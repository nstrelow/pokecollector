"""In-memory index of card fingerprints, loaded from the cards table.

Two structures are held per process. The packed hashes are genuinely tiny --
8 bytes a card, 338KB at 42,254 cards -- but the row metadata needed to render
a match is not: measured at 13.9MB for the same 42,254 rows, so roughly 19MB
across a full 58k catalogue, and a rebuild transiently holds the SQLAlchemy
result set on top of that. "The whole index is ~330KB" was only ever true of the
half that is not the cost. This is why the index is rebuilt in place rather than
kept per user or per request.

Reads take a single immutable snapshot, so a rebuild can never hand a caller new
hashes with stale metadata. Rebuilds are triggered by a process-local
generation counter, by a database version marker (so another process -- the
backfill script, or a second worker -- can invalidate this one), and by an age
ceiling as a backstop.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import numpy as np
from sqlalchemy import func
from sqlalchemy.orm import Session

from models import Card, Setting
from services.card_fingerprint import HASH_BYTES
from services.card_visibility import indexable_card_filter

logger = logging.getLogger(__name__)

# Rebuild at LEAST this often: a stale index is discarded once it reaches this
# age even if nothing signalled a change, so a sync that forgets to invalidate
# cannot leave the index wrong forever.
MAX_AGE_SECONDS = 15 * 60

# Key of the cross-process invalidation marker in the settings table. A separate
# process (scripts/backfill_fingerprints.py, or another uvicorn worker) bumps
# this; every process notices on its next read.
VERSION_SETTING_KEY = "fingerprint_index_version"

# "Ready" has to mean something. One fingerprinted card out of 58,634 is not a
# usable index, so report readiness as a coverage fraction over the cards that
# actually have artwork to hash.
READY_COVERAGE = 0.5
READY_MIN_CARDS = 500


@dataclass(frozen=True)
class Snapshot:
    """One consistent view of the index. Never mutated after construction."""

    packed: np.ndarray
    rows: tuple[dict, ...]
    built_at: float
    generation: int
    version: str

    def __len__(self) -> int:
        return len(self.rows)


_EMPTY = Snapshot(
    packed=np.empty((0, HASH_BYTES), dtype=np.uint8),
    rows=(),
    built_at=0.0,
    generation=-1,
    version="",
)

_lock = threading.Lock()
_generation = 0
_snapshot: Snapshot | None = None


def invalidate() -> None:
    """Mark the index stale for this process.

    Call after any change to the *indexed* catalogue: a card inserted, its
    artwork or language or digital flag changed, or a catalogue card deleted.
    Custom cards are never indexed (see `indexable_card_filter`), so custom-card
    creation, editing and deletion need no invalidation.

    This only affects the calling process. Use `bump_version` for a change made
    outside the running server.
    """
    global _generation
    _generation += 1


def read_version(db: Session) -> str:
    """Current cross-process invalidation marker, or "" when never set."""
    row = db.query(Setting.value).filter(Setting.key == VERSION_SETTING_KEY).first()
    return row[0] if row else ""


def bump_version(db: Session) -> str:
    """Advance the cross-process marker and commit it.

    Used by out-of-process writers -- the backfill script and the scheduled
    fingerprint job -- so a running server picks the new hashes up on its next
    scan instead of serving the old ones until MAX_AGE_SECONDS elapses.
    """
    row = db.query(Setting).filter(Setting.key == VERSION_SETTING_KEY).first()
    try:
        current = int(row.value) if row else 0
    except (TypeError, ValueError):
        current = 0
    value = str(current + 1)
    if row:
        row.value = value
    else:
        db.add(Setting(key=VERSION_SETTING_KEY, value=value))
    db.commit()
    invalidate()
    return value


def _load(db: Session) -> Snapshot:
    """Build a fresh snapshot. Caller must hold `_lock`."""
    # Snapshot the generation and the DB marker BEFORE reading the rows. An
    # invalidate() that lands while this query runs must not be swallowed:
    # card_upsert calls invalidate() before its commit, so clearing the flag
    # after the read would leave the index stale for a full MAX_AGE_SECONDS.
    generation = _generation
    version = read_version(db)

    rows = (
        db.query(
            Card.id, Card.tcg_card_id, Card.name, Card.number, Card.set_id,
            Card.lang, Card.rarity, Card.images_small, Card.image_phash,
        )
        .filter(Card.image_phash.isnot(None))
        .filter(indexable_card_filter(db))
        # Postgres row order is not stable across rebuilds, and search breaks
        # ties by row index. Without an explicit order, which of two
        # identical-artwork cards wins rank 1 would flip between rebuilds.
        .order_by(Card.id)
        .all()
    )
    usable = [r for r in rows if r.image_phash and len(r.image_phash) == HASH_BYTES]
    if len(usable) != len(rows):
        logger.warning(
            "fingerprint index: skipped %d cards with malformed hashes",
            len(rows) - len(usable),
        )

    if usable:
        packed = np.frombuffer(
            b"".join(r.image_phash for r in usable), dtype=np.uint8
        ).reshape(len(usable), HASH_BYTES)
    else:
        packed = np.empty((0, HASH_BYTES), dtype=np.uint8)

    snapshot = Snapshot(
        packed=packed,
        rows=tuple(
            {
                "id": r.id,
                "tcg_card_id": r.tcg_card_id,
                "name": r.name,
                "number": r.number,
                "set_id": r.set_id,
                "lang": r.lang,
                "rarity": r.rarity,
                "image": r.images_small,
            }
            for r in usable
        ),
        built_at=time.time(),
        generation=generation,
        version=version,
    )
    logger.info("fingerprint index: loaded %d cards", len(usable))
    return snapshot


def _is_stale(snapshot: Snapshot | None, db: Session) -> bool:
    if snapshot is None:
        return True
    if snapshot.generation != _generation:
        return True
    # An empty result is a legitimate, cacheable answer. Treating "no rows" as
    # stale made every request on an un-backfilled catalogue run a full table
    # scan under the global lock before returning 503 -- a free amplification
    # for any logged-in user.
    if (time.time() - snapshot.built_at) > MAX_AGE_SECONDS:
        return True
    return snapshot.version != read_version(db)


def get(db: Session) -> Snapshot:
    """Return one consistent snapshot of the index, rebuilding if stale.

    Blocking: a rebuild reads the whole cards table and takes 1-3s at catalogue
    scale, so callers must not run this on the event loop.
    """
    global _snapshot
    snapshot = _snapshot
    if not _is_stale(snapshot, db):
        return snapshot or _EMPTY
    with _lock:
        # Re-check inside the lock; another request may have just rebuilt.
        if _is_stale(_snapshot, db):
            _snapshot = _load(db)
        return _snapshot or _EMPTY


def reset() -> None:
    """Drop the cached snapshot entirely. For tests and process teardown."""
    global _snapshot, _generation
    with _lock:
        _snapshot = None
        _generation += 1


def coverage(db: Session) -> dict:
    """How much of the catalogue can currently be recognised offline."""
    indexable = indexable_card_filter(db)
    total, with_image, with_hash = (
        db.query(
            func.count(Card.id),
            func.count(Card.images_small),
            func.count(Card.image_phash),
        )
        .filter(indexable)
        .one()
    )
    total = int(total or 0)
    with_image = int(with_image or 0)
    with_hash = int(with_hash or 0)
    fraction = (with_hash / with_image) if with_image else 0.0
    return {
        "cards_total": total,
        "cards_with_image": with_image,
        "cards_fingerprinted": with_hash,
        "coverage": round(fraction, 4),
        "ready": with_hash >= READY_MIN_CARDS and fraction >= READY_COVERAGE,
    }
