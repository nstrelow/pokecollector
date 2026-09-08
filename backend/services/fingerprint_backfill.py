"""Downloading catalogue artwork and storing its perceptual fingerprint.

Shared by the one-off `scripts/backfill_fingerprints.py` and the recurring
scheduler job, so both pace TCGdex the same way and apply the same rules about
what may be stored.

TCGdex is a free, community-run service, so requests are paced globally rather
than issued as fast as the CDN will allow.

Two rules keep a run bounded and keep the queue moving:

* A run has a wall-clock budget and a failure-rate circuit breaker. Without
  them one "hourly" batch could take most of a day -- `download_image` alone is
  ~127s worst case per card (4 attempts at a 30s timeout plus 1+2+4s of
  backoff), so 2,000 cards over 4 workers is 17.6 hours -- while the scheduler
  silently coalesced away the seventeen runs it overlapped.
* Every outcome that is a property of the URL rather than of the network is
  recorded in `cards.image_phash_source`. That column is the fingerprint's
  provenance ("this hash is of this picture") and simultaneously its negative
  cache ("this picture was tried and yields nothing usable"). Without it,
  cards that can never be fingerprinted -- a render that does not decode, a CDN
  placeholder -- stayed NULL, sorted to the head of the id-ordered window every
  hour, and were re-downloaded forever while coverage never advanced.

A genuinely transient failure -- timeout, 5xx, unreachable host -- writes
nothing at all and is retried on the next run. `--refresh` ignores the negative
cache entirely, which is the escape hatch if a CDN outage ever poisons it.
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
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from models import Card
from services import fingerprint_index
from services.card_fingerprint import fingerprint_reference
from services.card_visibility import indexable_card_filter

logger = logging.getLogger(__name__)

DEFAULT_RPS = 5.0
DEFAULT_WORKERS = 4
COMMIT_BATCH = 200

# Never hold an open transaction longer than this between commits. The batch
# used to flush only every COMMIT_BATCH *pending* rows, and rows that failed
# never became pending, so a run that mostly failed kept the transaction opened
# by the initial SELECT alive for its entire length: idle-in-transaction for
# hours, pinning the xmin horizon and blocking autovacuum on `cards`.
MAX_FLUSH_SECONDS = 60.0

# Statuses that mean "this image will not appear later", as opposed to a
# transient failure. Only these are allowed to clear an existing fingerprint.
ABSENT_STATUSES = {400, 401, 403, 404, 410}

# A CDN that answers with a generic "image unavailable" render returns the very
# same bytes for every card that hits it. Those cards would all get one
# identical hash and collide at distance 0 at the top of every shortlist. Real
# distinct card renders are never byte-identical, so repeated identical
# downloads within one run are treated as a placeholder and discarded.
PLACEHOLDER_REPEATS = 8

# Circuit breaker. Once this many cards have been attempted, a run whose
# failure rate is above the threshold stops instead of spending its whole
# budget hammering a CDN that is plainly not answering. Only failures that mean
# "we got nothing usable" count; a 404 is a definite answer, not a failure.
BREAKER_MIN_ATTEMPTS = 50
BREAKER_FAILURE_RATE = 0.5

# Default wall-clock budget for one run, in seconds. The scheduler passes its
# own; this is the ceiling for anything that does not.
DEFAULT_TIME_BUDGET = 30 * 60.0


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


def stale_fingerprint_filter():
    """Rows whose stored hash is not a hash of their current artwork.

    This is the self-detecting half of the staleness guard. Three catalogue
    writers used to rotate `images_small` without clearing `image_phash`, and
    because the backfill only ever selected `image_phash IS NULL` the survivor
    was never recomputed: photographs of the OLD artwork kept matching it at
    distance 0, with a wide margin, and nothing repaired it. Comparing stored
    provenance against the current URL makes that a fact about the row, so a
    future writer that forgets cannot corrupt the index permanently -- the next
    run picks the row back up.

    A NULL `image_phash_source` also matches: it means "never attempted", which
    is every row on an install that predates the column.
    """
    return or_(
        Card.image_phash_source.is_(None),
        Card.image_phash_source != Card.images_small,
    )


def pending_cards(
    db: Session,
    *,
    langs: list[str] | None = None,
    refresh: bool = False,
    limit: int = 0,
    shuffle: bool = True,
):
    """Cards eligible for fingerprinting, at most `limit` of them.

    Only indexable cards are considered: custom cards are never fingerprinted,
    and neither are cards the catalogue does not show.

    "Eligible" is "we have not already tried this exact URL": a row is pending
    while its `image_phash_source` differs from its `images_small`, whether
    that is because it has never been attempted, because a writer rotated the
    artwork behind a stored hash, or because the URL changed after a failed
    attempt. A row whose provenance already matches is done -- successfully if
    it has a hash, permanently unusably if it does not.

    `shuffle` orders the window randomly. Ordering by id looks tidier and lets a
    bounded run resume where the last stopped, but it is also how the job
    starved: any block of low-id rows that fails transiently every time -- a
    dead CDN edge, a set of URLs that always time out -- sits at the head of the
    same 2,000-row window every hour and nothing behind it is ever reached.
    Random order gives every pending row the same chance, so coverage advances
    whatever is broken. Pass `shuffle=False` for a deterministic sweep.
    """
    query = (
        db.query(Card.id, Card.images_small)
        .filter(Card.images_small.isnot(None))
        .filter(indexable_card_filter(db))
    )
    if not refresh:
        query = query.filter(stale_fingerprint_filter())
    if langs:
        query = query.filter(Card.lang.in_(langs))
    # Postgres row order is not stable, so an order is always specified: either
    # random (the default, see above) or by id for a reproducible sweep.
    query = query.order_by(func.random() if shuffle else Card.id)
    if limit:
        # Push the limit into SQL. Fetching 45k rows to keep 10 was pointless.
        query = query.limit(limit)
    return query.all()


def _store(db: Session, updates: list[tuple[str, dict]]) -> None:
    """Apply one batch of per-card column updates in as few round trips as possible.

    Was one UPDATE statement per card -- 2,000 round trips for one scheduled
    run. Cards sharing the same set of values (all the successes with the same
    provenance shape, all the negative-cache writes) are grouped, so a batch
    costs a handful of statements instead of one per row.
    """
    if not updates:
        return
    per_card: list[dict] = []
    grouped: dict[tuple, list[str]] = defaultdict(list)
    for card_id, values in updates:
        if "image_phash" in values:
            # A distinct hash per card cannot be grouped by value, but it can
            # still go out as one executemany instead of one statement each.
            per_card.append({"id": card_id, **values})
        else:
            grouped[tuple(sorted(values.items()))].append(card_id)
    if per_card:
        db.bulk_update_mappings(Card, per_card)
    for key, card_ids in grouped.items():
        db.query(Card).filter(Card.id.in_(card_ids)).update(
            dict(key), synchronize_session=False
        )
    db.commit()


class _Budget:
    """Wall-clock deadline plus failure-rate circuit breaker for one run.

    Updated from the worker threads, because that is where outcomes become
    known. `ThreadPoolExecutor.map` submits every task up front, so the workers
    run ahead of whatever consumes their results: a breaker fed from the
    consuming loop would only trip long after the whole batch had been
    downloaded, which is exactly the cost it exists to avoid.
    """

    def __init__(self, deadline: float) -> None:
        self.deadline = deadline
        self.attempted = 0
        self.failed = 0
        self.stopped: str | None = None
        self._lock = threading.Lock()

    def record(self, *, failure: bool) -> None:
        with self._lock:
            self.attempted += 1
            if failure:
                self.failed += 1

    def check(self) -> str | None:
        with self._lock:
            if self.stopped:
                return self.stopped
            if time.monotonic() >= self.deadline:
                self.stopped = "time budget exhausted"
            elif (
                self.attempted >= BREAKER_MIN_ATTEMPTS
                and self.failed > self.attempted * BREAKER_FAILURE_RATE
            ):
                self.stopped = (
                    f"failure rate {self.failed}/{self.attempted} above "
                    f"{BREAKER_FAILURE_RATE:.0%}"
                )
            return self.stopped


def fingerprint_cards(
    db: Session,
    rows,
    *,
    rps: float = DEFAULT_RPS,
    workers: int = DEFAULT_WORKERS,
    refresh: bool = False,
    time_budget: float = DEFAULT_TIME_BUDGET,
    on_progress=None,
) -> dict:
    """Download, hash and store fingerprints for `rows`.

    Returns counts. `cleared` are cards whose artwork is definitively gone; in
    refresh mode their stale fingerprint is removed rather than left pointing at
    a picture that no longer exists. A merely unreachable CDN never clears
    anything, and never writes to the negative cache either.

    Stops early when `time_budget` seconds have elapsed or the circuit breaker
    trips; `stopped_early` says which. Everything already downloaded is stored
    before returning, and whatever was not attempted is simply still pending.
    """
    pacer = Pacer(rps)
    budget = _Budget(deadline=time.monotonic() + max(1.0, time_budget))
    stored = skipped = cleared = 0
    # (card_id, column values, content digest or None)
    pending: list[tuple[str, dict, str | None]] = []
    digest_counts: Counter = Counter()
    stored_by_digest: dict[str, list[str]] = defaultdict(list)
    last_flush = time.monotonic()

    cards_by_digest: dict[str, list[str]] = defaultdict(list)
    url_of = {row.id: row.images_small for row in rows}

    def flush() -> None:
        nonlocal stored, pending, last_flush
        keep: list[tuple[str, dict]] = []
        for card_id, values, content_digest in pending:
            if (
                content_digest is not None
                and digest_counts[content_digest] > PLACEHOLDER_REPEATS
            ):
                continue
            keep.append((card_id, values))
            if values.get("image_phash") is not None and content_digest is not None:
                # Only what actually reached the database, so it can be taken
                # back out if this image turns out to be a placeholder.
                stored_by_digest[content_digest].append(card_id)
        if keep:
            _store(db, keep)
            stored += sum(
                1 for _, values in keep if values.get("image_phash") is not None
            )
        pending = []
        last_flush = time.monotonic()

    with httpx.Client(
        headers={"User-Agent": "pokecollector/backfill-fingerprints"},
        limits=httpx.Limits(max_connections=max(1, workers)),
    ) as client:

        def work(row):
            """Download and classify one card. Runs on a worker thread.

            Outcomes are recorded against the budget here rather than in the
            consuming loop below, because `pool.map` queues everything up front
            and the workers run far ahead of the consumer.
            """
            if budget.check():
                return "aborted", row.id, None, None
            result = download_image(client, row.images_small, pacer)
            if result.content is None:
                if result.absent:
                    # A definite "this URL has no image", not a failure to reach
                    # it, so it does not count against the circuit breaker.
                    budget.record(failure=False)
                    return "absent", row.id, None, None
                budget.record(failure=True)
                return "unreachable", row.id, None, None
            content_digest = hashlib.sha256(result.content).hexdigest()
            digest = fingerprint_reference(result.content)
            if digest is None:
                budget.record(failure=True)
                return "undecodable", row.id, None, content_digest
            budget.record(failure=False)
            return "ok", row.id, digest, content_digest

        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            # pool.map yields into this thread only, so everything below is
            # single threaded. Tasks that start after the budget runs out return
            # immediately without a request, which is how a deadline is honoured
            # without cancelling work already in flight.
            for index, (outcome, card_id, digest, content_digest) in enumerate(
                pool.map(work, rows), start=1
            ):
                url = url_of.get(card_id)
                if outcome == "aborted":
                    continue
                if outcome == "unreachable":
                    # Says nothing about the URL. Write nothing; retry next run.
                    skipped += 1
                elif outcome == "absent":
                    if refresh:
                        pending.append(
                            (card_id,
                             {"image_phash": None, "image_phash_source": url},
                             None)
                        )
                        cleared += 1
                    else:
                        # Not a refresh, so an existing hash is left alone; only
                        # the provenance is recorded, so it is not re-fetched.
                        pending.append((card_id, {"image_phash_source": url}, None))
                        skipped += 1
                elif outcome == "undecodable":
                    # Downloaded, but the render does not decode. That is a
                    # property of the image, not of the network: cache it, or it
                    # heads the queue again every single run.
                    pending.append((card_id, {"image_phash_source": url}, None))
                    skipped += 1
                else:
                    digest_counts[content_digest] += 1
                    cards_by_digest[content_digest].append(card_id)
                    pending.append(
                        (card_id,
                         {"image_phash": digest, "image_phash_source": url},
                         content_digest)
                    )
                if (
                    len(pending) >= COMMIT_BATCH
                    or (pending and time.monotonic() - last_flush >= MAX_FLUSH_SECONDS)
                ):
                    flush()
                    if on_progress:
                        on_progress(index, len(rows), stored, skipped, cleared)

    flush()

    # A placeholder can only be recognised once it has repeated, so the first
    # few copies may already have been committed. Take them back out -- and
    # negative-cache every card that hit it, withheld or not, because serving a
    # placeholder is what this URL does and re-downloading it hourly achieves
    # nothing. `--refresh` is the way back if a CDN outage ever poisons this.
    poisoned = [d for d, count in digest_counts.items() if count > PLACEHOLDER_REPEATS]
    withheld = sum(digest_counts[d] for d in poisoned)
    undone = 0
    if poisoned:
        affected = [cid for d in poisoned for cid in cards_by_digest[d]]
        already_written = {cid for d in poisoned for cid in stored_by_digest[d]}
        _store(db, [
            (cid, {"image_phash": None, "image_phash_source": url_of.get(cid)})
            for cid in affected
        ])
        undone = len(already_written)
        stored -= undone
        logger.warning(
            "fingerprint backfill: discarded %d cards sharing %d byte-identical "
            "images (looks like a CDN placeholder); %d of them had already been "
            "committed and were taken back out",
            withheld, len(poisoned), undone,
        )

    if stored or cleared or withheld:
        # Another process holds the live index; tell it to reload.
        fingerprint_index.bump_version(db)

    if budget.stopped:
        logger.warning(
            "fingerprint backfill stopped early: %s (attempted %d of %d)",
            budget.stopped, budget.attempted, len(rows),
        )

    return {
        "considered": len(rows),
        "attempted": budget.attempted,
        "stored": stored,
        "skipped": skipped,
        "cleared": cleared,
        # Cards whose hash was withheld or taken back out as a placeholder.
        "placeholders": withheld,
        "placeholders_undone": undone,
        "stopped_early": budget.stopped,
    }


def run_backfill(
    db: Session,
    *,
    langs: list[str] | None = None,
    refresh: bool = False,
    limit: int = 0,
    rps: float = DEFAULT_RPS,
    workers: int = DEFAULT_WORKERS,
    time_budget: float = DEFAULT_TIME_BUDGET,
    shuffle: bool = True,
    on_progress=None,
) -> dict:
    """Select the outstanding work and fingerprint it."""
    rows = pending_cards(
        db, langs=langs, refresh=refresh, limit=limit, shuffle=shuffle
    )
    # End the read transaction before spending minutes on the network. The
    # SELECT above opens one, and nothing else touched this session until the
    # first flush, so it stayed open -- idle in transaction -- for the whole run.
    db.commit()
    if not rows:
        return {"considered": 0, "attempted": 0, "stored": 0, "skipped": 0,
                "cleared": 0, "placeholders": 0, "placeholders_undone": 0,
                "stopped_early": None}
    return fingerprint_cards(
        db, rows, rps=rps, workers=workers, refresh=refresh,
        time_budget=time_budget, on_progress=on_progress,
    )
