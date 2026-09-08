"""In-memory index of card fingerprints, loaded from the cards table.

The whole index for a 42k-card catalogue is ~330KB, so it is held in process
rather than queried per scan: a Hamming scan over all of it costs about 3ms,
which is faster than the round trip to Postgres would be.

It is rebuilt lazily, and invalidated whenever the catalogue changes.
"""
from __future__ import annotations

import logging
import threading
import time

import numpy as np
from sqlalchemy import func
from sqlalchemy.orm import Session

from models import Card
from services.card_fingerprint import HASH_BYTES

logger = logging.getLogger(__name__)

# Rebuild at most this often even if nothing signalled a change, so a sync that
# forgets to invalidate cannot leave the index permanently stale.
MAX_AGE_SECONDS = 15 * 60


class _Index:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.packed: np.ndarray = np.empty((0, HASH_BYTES), dtype=np.uint8)
        self.rows: list[dict] = []
        self.built_at: float = 0.0
        self.dirty: bool = True

    def invalidate(self) -> None:
        self.dirty = True


_index = _Index()


def invalidate() -> None:
    """Mark the index stale. Call after any card insert/update/delete."""
    _index.invalidate()


def _load(db: Session) -> None:
    rows = (
        db.query(
            Card.id, Card.tcg_card_id, Card.name, Card.number, Card.set_id,
            Card.lang, Card.rarity, Card.images_small, Card.image_phash,
        )
        .filter(Card.image_phash.isnot(None))
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

    _index.packed = packed
    _index.rows = [
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
    ]
    _index.built_at = time.time()
    _index.dirty = False
    logger.info("fingerprint index: loaded %d cards", len(usable))


def get(db: Session) -> tuple[np.ndarray, list[dict]]:
    """Return (packed hashes, row metadata), rebuilding if stale."""
    stale = (
        _index.dirty
        or not _index.rows
        or (time.time() - _index.built_at) > MAX_AGE_SECONDS
    )
    if stale:
        with _index.lock:
            # Re-check inside the lock; another request may have just rebuilt.
            if (
                _index.dirty
                or not _index.rows
                or (time.time() - _index.built_at) > MAX_AGE_SECONDS
            ):
                _load(db)
    return _index.packed, _index.rows


def coverage(db: Session) -> dict:
    """How much of the catalogue can currently be recognised offline."""
    total, with_image, with_hash = db.query(
        func.count(Card.id),
        func.count(Card.images_small),
        func.count(Card.image_phash),
    ).one()
    return {
        "cards_total": int(total or 0),
        "cards_with_image": int(with_image or 0),
        "cards_fingerprinted": int(with_hash or 0),
        "ready": bool(with_hash),
    }
