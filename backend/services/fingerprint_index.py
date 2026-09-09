"""In-memory index of card fingerprints, loaded from the cards table.

Two structures are held per process. The packed hashes are genuinely tiny --
8 bytes a card, 338KB at 42,254 cards -- but the row metadata needed to render
a match is not.

Measured with tracemalloc over the real catalogue's own values (58,630 rows
dumped from a production database), building exactly the tuple-of-dicts that
`_load` builds, from FRESH string objects: 674 bytes a row, so 28.5MB at 42,254
rows and 39.5MB at 58,634. An earlier comment here claimed 13.9MB for 42,254
rows; that measurement reused the strings it was measuring, which is not what
psycopg2 does -- every fetched row arrives as new str objects -- so it
undercounted by about half. A rebuild transiently holds the SQLAlchemy result
set on top of the new snapshot, so peak is roughly double. "The whole index is
~330KB" was only ever true of the half that is not the cost. This is why the
index is rebuilt in place rather than kept per user or per request.

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
import uuid
from dataclasses import dataclass

import numpy as np
from sqlalchemy import and_, case, func
from sqlalchemy.exc import IntegrityError
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


def _is_ready(with_hash: int, with_image: int) -> bool:
    """The one definition of "ready", shared by /status and the scan endpoint.

    These two used to disagree: /status applied this rule while the scan
    endpoint only checked that the index was non-empty, so a freshly seeded
    install could return `_identity_confident: true` off a couple of thousand
    cards while /status still said it was not ready. `is_confident`'s margin
    test assumes a catalogue-sized index to be a margin against; on a small one
    a lone survivor at distance 12 is not evidence of anything.
    """
    fraction = (with_hash / with_image) if with_image else 0.0
    return with_hash >= READY_MIN_CARDS and fraction >= READY_COVERAGE


@dataclass(frozen=True)
class Snapshot:
    """One consistent view of the index. Never mutated after construction."""

    packed: np.ndarray
    rows: tuple[dict, ...]
    built_at: float
    generation: int
    version: str
    # Whether this snapshot is complete enough to answer with, by the same rule
    # `coverage()` reports to /status. Carried on the snapshot so the scan
    # endpoint can check it without a second aggregate query per request.
    ready: bool = False

    def __len__(self) -> int:
        return len(self.rows)


_EMPTY = Snapshot(
    packed=np.empty((0, HASH_BYTES), dtype=np.uint8),
    rows=(),
    built_at=0.0,
    generation=-1,
    version="",
    ready=False,
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

    Known gap, deliberately not covered here. `indexable_card_filter` also
    depends on `get_pinned_set_language_pairs`, which every collection,
    wishlist and binder write can change: adding the first card of a set in a
    language that is not in `tcgdex_sync_languages` makes that whole set
    indexable, and removing the last one makes it un-indexable again. Those
    writes are frequent and per-user, and calling invalidate() from each would
    make a normal collecting session rebuild the index continuously, so they do
    not. The consequence is bounded by MAX_AGE_SECONDS: for at most 15 minutes
    a newly pinned language is not yet matchable, or an unpinned one still is.
    Callers that need this immediately should invalidate explicitly.

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

    The marker is a fresh random token, not an incremented integer. Readers only
    ever ask "is this different from what my snapshot was built with", so the
    value carries no meaning, and read-modify-writing a counter from two
    processes at once loses one of the two updates -- exactly the invalidation
    you cannot afford to lose, and it raised a duplicate-key IntegrityError when
    both raced to create the row. A blind UPDATE to a new token is atomic.

    Also invalidates locally: a process that just wrote hashes must not go on
    serving its own stale snapshot either.
    """
    value = uuid.uuid4().hex
    updated = (
        db.query(Setting)
        .filter(Setting.key == VERSION_SETTING_KEY)
        .update({"value": value}, synchronize_session=False)
    )
    if updated:
        db.commit()
    else:
        # First bump on this install. Another process may be inserting the same
        # primary key at the same moment; its row is an equally good "something
        # changed" signal, but overwrite it so this call's writes are covered.
        try:
            db.add(Setting(key=VERSION_SETTING_KEY, value=value))
            db.commit()
        except IntegrityError:
            db.rollback()
            db.query(Setting).filter(Setting.key == VERSION_SETTING_KEY).update(
                {"value": value}, synchronize_session=False
            )
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
    indexable = indexable_card_filter(db)
    # Same numerator `coverage()` reports to /status: rows that have BOTH
    # artwork and a hash. `len(usable)` below is not it -- `usable` also
    # admits rows with a hash but no `images_small` (a data anomaly: a writer
    # that clears artwork after it was fingerprinted, without also clearing
    # the hash), and counting those in readiness let /recognize/local accept
    # requests while /status reported not ready off the identical snapshot.
    with_image, with_hash = (
        db.query(
            func.count(Card.images_small),
            func.count(case(
                (and_(Card.images_small.isnot(None), Card.image_phash.isnot(None)), 1)
            )),
        )
        .filter(indexable)
        .one()
    )
    with_image = int(with_image or 0)
    with_hash = int(with_hash or 0)

    rows = (
        db.query(
            Card.id, Card.tcg_card_id, Card.name, Card.number, Card.set_id,
            Card.lang, Card.rarity, Card.images_small, Card.image_phash,
        )
        .filter(Card.image_phash.isnot(None))
        .filter(indexable)
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
        ready=_is_ready(with_hash, with_image),
    )
    logger.info(
        "fingerprint index: loaded %d cards of %d with artwork (ready=%s)",
        len(usable), with_image, snapshot.ready,
    )
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

    At most one thread rebuilds. The others do NOT queue behind it: if a
    snapshot already exists they take the slightly stale one and answer now.
    Waiting for the lock was a latency cliff -- every settings change and every
    sync invalidates, and each waiter occupies both an anyio worker thread and a
    pooled database connection for the whole 1-3s rebuild, so a handful of
    concurrent scans after a sync could exhaust the pool. The extra staleness is
    bounded by one rebuild, against a snapshot that is allowed to be
    MAX_AGE_SECONDS old anyway. The very first load has nothing to serve, so
    that one does block.
    """
    global _snapshot
    snapshot = _snapshot
    if not _is_stale(snapshot, db):
        return snapshot or _EMPTY

    blocking = snapshot is None
    if not _lock.acquire(blocking=blocking):
        # Someone else is rebuilding and we have something to answer with.
        return _snapshot or snapshot or _EMPTY
    try:
        # Re-check inside the lock; another request may have just rebuilt.
        if _is_stale(_snapshot, db):
            _snapshot = _load(db)
        return _snapshot or _EMPTY
    finally:
        _lock.release()


def reset() -> None:
    """Drop the cached snapshot entirely. For tests and process teardown."""
    global _snapshot, _generation
    with _lock:
        _snapshot = None
        _generation += 1


def coverage(db: Session) -> dict:
    """How much of the catalogue can currently be recognised offline.

    `cards_fingerprinted` counts indexable cards that have BOTH artwork and a
    hash, which is the numerator the `coverage` fraction needs. A row holding a
    hash but no artwork URL is a data anomaly rather than usable coverage; it
    would still be matchable, so this slightly understates the index size in
    exchange for a fraction that cannot exceed 1.0.
    """
    indexable = indexable_card_filter(db)
    total, with_image, with_hash = (
        db.query(
            func.count(Card.id),
            func.count(Card.images_small),
            # Only hashes on rows that also have artwork, so the fraction below
            # cannot exceed 1.0. A card whose images_small was cleared after it
            # was fingerprinted counted in the numerator but not the
            # denominator, and coverage went over 100%.
            func.count(case(
                (and_(Card.images_small.isnot(None), Card.image_phash.isnot(None)), 1)
            )),
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
        "ready": _is_ready(with_hash, with_image),
    }
