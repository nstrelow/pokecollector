"""Downloading catalogue artwork and storing its perceptual fingerprint.

Shared by the one-off `scripts/backfill_fingerprints.py` and the recurring
scheduler job, so both pace TCGdex the same way and apply the same rules about
what may be stored.

TCGdex is a free, community-run service, so requests are paced globally rather
than issued as fast as the CDN will allow.
"""
from __future__ import annotations

import hashlib
import logging
import random
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import httpx
from sqlalchemy.orm import Session

from models import Card
from services import fingerprint_index
from services.card_fingerprint import fingerprint_reference
from services.card_visibility import indexable_card_filter

logger = logging.getLogger(__name__)

DEFAULT_RPS = 5.0
DEFAULT_WORKERS = 4
COMMIT_BATCH = 200

# Statuses that mean "this image will not appear later", as opposed to a
# transient failure. Only these are allowed to clear an existing fingerprint.
ABSENT_STATUSES = {400, 401, 403, 404, 410}

# A CDN that answers with a generic "image unavailable" render returns the very
# same bytes for every card that hits it. Those cards would all get one
# identical hash and collide at distance 0 at the top of every shortlist. Real
# distinct card renders are never byte-identical, so repeated identical
# downloads within one run are treated as a placeholder and discarded.
PLACEHOLDER_REPEATS = 8


class Pacer:
    """Global request pacing shared across worker threads."""

    def __init__(self, rps: float) -> None:
        self.interval = 1.0 / max(rps, 0.1)
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def wait(self) -> None:
        with self._lock:
            slot = max(time.monotonic(), self._next_slot)
            self._next_slot = slot + self.interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)


@dataclass(frozen=True)
class Download:
    content: bytes | None
    absent: bool = False


def download_image(
    client: httpx.Client, url: str, pacer: Pacer, attempts: int = 4
) -> Download:
    """Fetch one image, distinguishing "gone" from "could not reach it"."""
    delay = 1.0
    for attempt in range(attempts):
        try:
            pacer.wait()
            response = client.get(url, timeout=30)
            if response.status_code == 200 and response.content:
                return Download(response.content)
            if response.status_code in ABSENT_STATUSES:
                return Download(None, absent=True)
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = max(delay, min(float(retry_after), 60.0))
                except ValueError:
                    pass
        except Exception:
            pass
        if attempt < attempts - 1:
            time.sleep(delay + random.random() * 0.5)
            delay = min(delay * 2, 20.0)
    return Download(None)


def pending_cards(
    db: Session,
    *,
    langs: list[str] | None = None,
    refresh: bool = False,
    limit: int = 0,
):
    """Cards eligible for fingerprinting, in a stable order, at most `limit`.

    Only indexable cards are considered: custom cards are never fingerprinted,
    and neither are cards the catalogue does not show. The order is by id so a
    bounded run resumed later starts where the previous one stopped instead of
    re-drawing an arbitrary sample.
    """
    query = (
        db.query(Card.id, Card.images_small)
        .filter(Card.images_small.isnot(None))
        .filter(indexable_card_filter(db))
    )
    if not refresh:
        query = query.filter(Card.image_phash.is_(None))
    if langs:
        query = query.filter(Card.lang.in_(langs))
    query = query.order_by(Card.id)
    if limit:
        # Push the limit into SQL. Fetching 45k rows to keep 10 was pointless.
        query = query.limit(limit)
    return query.all()


def _store(db: Session, updates: list[tuple[str, bytes | None]]) -> None:
    for card_id, value in updates:
        db.query(Card).filter(Card.id == card_id).update(
            {"image_phash": value}, synchronize_session=False
        )
    db.commit()


def fingerprint_cards(
    db: Session,
    rows,
    *,
    rps: float = DEFAULT_RPS,
    workers: int = DEFAULT_WORKERS,
    refresh: bool = False,
    on_progress=None,
) -> dict:
    """Download, hash and store fingerprints for `rows`.

    Returns counts. `cleared` are cards whose artwork is definitively gone; in
    refresh mode their stale fingerprint is removed rather than left pointing at
    a picture that no longer exists. A merely unreachable CDN never clears
    anything.
    """
    pacer = Pacer(rps)
    stored = skipped = cleared = 0
    pending: list[tuple[str, bytes | None]] = []
    digest_counts: Counter = Counter()
    stored_by_digest: dict[str, list[str]] = defaultdict(list)

    def flush() -> None:
        nonlocal stored, pending
        keep = []
        for card_id, value, content_digest in pending:
            if (
                content_digest is not None
                and digest_counts[content_digest] > PLACEHOLDER_REPEATS
            ):
                continue
            keep.append((card_id, value))
            if value is not None and content_digest is not None:
                # Only what actually reached the database, so it can be taken
                # back out if this image turns out to be a placeholder.
                stored_by_digest[content_digest].append(card_id)
        if keep:
            _store(db, keep)
            stored += sum(1 for _, value in keep if value is not None)
        pending = []

    with httpx.Client(
        headers={"User-Agent": "pokecollector/backfill-fingerprints"},
        limits=httpx.Limits(max_connections=max(1, workers)),
    ) as client:

        def work(row):
            result = download_image(client, row.images_small, pacer)
            if result.content is None:
                return row.id, None, None, result.absent
            content_digest = hashlib.sha256(result.content).hexdigest()
            return row.id, fingerprint_reference(result.content), content_digest, False

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            # pool.map yields into this thread only, so nothing below needs a
            # lock; only `pacer` is touched by the workers.
            for index, (card_id, digest, content_digest, absent) in enumerate(
                pool.map(work, rows), start=1
            ):
                if digest is None:
                    if refresh and absent:
                        pending.append((card_id, None, None))
                        cleared += 1
                    else:
                        skipped += 1
                else:
                    digest_counts[content_digest] += 1
                    pending.append((card_id, digest, content_digest))
                if len(pending) >= COMMIT_BATCH:
                    flush()
                    if on_progress:
                        on_progress(index, len(rows), stored, skipped, cleared)

    flush()

    # A placeholder can only be recognised once it has repeated, so the first
    # few copies may already have been committed. Take them back out.
    poisoned = [d for d, count in digest_counts.items() if count > PLACEHOLDER_REPEATS]
    placeholders = sum(digest_counts[d] for d in poisoned)
    if poisoned:
        already_written = [cid for d in poisoned for cid in stored_by_digest[d]]
        if already_written:
            _store(db, [(cid, None) for cid in already_written])
            stored -= len(already_written)
        logger.warning(
            "fingerprint backfill: discarded %d cards sharing %d byte-identical "
            "images (looks like a CDN placeholder)",
            placeholders,
            len(poisoned),
        )

    if stored or cleared or placeholders:
        # Another process holds the live index; tell it to reload.
        fingerprint_index.bump_version(db)

    return {
        "considered": len(rows),
        "stored": stored,
        "skipped": skipped,
        "cleared": cleared,
        "placeholders": placeholders,
    }


def run_backfill(
    db: Session,
    *,
    langs: list[str] | None = None,
    refresh: bool = False,
    limit: int = 0,
    rps: float = DEFAULT_RPS,
    workers: int = DEFAULT_WORKERS,
    on_progress=None,
) -> dict:
    """Select the outstanding work and fingerprint it."""
    rows = pending_cards(db, langs=langs, refresh=refresh, limit=limit)
    if not rows:
        return {"considered": 0, "stored": 0, "skipped": 0, "cleared": 0,
                "placeholders": 0}
    return fingerprint_cards(
        db, rows, rps=rps, workers=workers, refresh=refresh, on_progress=on_progress
    )
