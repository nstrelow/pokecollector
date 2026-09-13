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
from services import card_embedding
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


# How many dimensions the whitened embedding index keeps. Measured over the
# 42,254-card index: no whitening scores 99.07% / 86.01%, 128 dimensions is
# WORSE than none at 99.04% / 84.81%, 256 reaches 99.65% / 86.18%, and 384
# gains a further 0.07 points for 10MB more resident memory. 256 is the rare
# change that improves accuracy and cuts cost at the same time.
WHITENING_DIMS = 256

# Ridge on the eigenvalues, so a near-zero direction is damped rather than
# amplified into the dominant one.
_WHITENING_EPSILON = 1e-4

# Below this many embedded rows, the whitening covariance is fitted on too
# little to mean anything and the accurate path is not used at all.
MIN_EMBEDDED_CARDS = 256


@dataclass(frozen=True)
class Whitening:
    """The PCA whitening fitted to one snapshot's embeddings.

    Carried on the snapshot rather than stored in the database, because it is a
    property of the index as loaded: fitting it at load time costs one 768x768
    eigendecomposition (milliseconds) and guarantees the query is projected
    through exactly the transform the stored vectors were, which a persisted
    matrix could drift from the moment one row is re-embedded.
    """

    mean: np.ndarray
    projection: np.ndarray

    def apply(self, vectors: np.ndarray) -> np.ndarray:
        projected = (vectors - self.mean) @ self.projection
        norms = np.linalg.norm(projected, axis=1, keepdims=True)
        return (projected / (norms + 1e-9)).astype(np.float32)


@dataclass(frozen=True)
class Snapshot:
    """One consistent view of the index. Never mutated after construction."""

    packed: np.ndarray
    rows: tuple[dict, ...]
    built_at: float
    generation: int
    version: str
    # The accurate path, present only when this installation opted into a model
    # AND enough of the catalogue has been embedded. `embeddings` is whitened
    # and row-aligned with `packed`; `embedded` marks which rows really have
    # one, because a row that does not must compete on its hash alone rather
    # than on a zero vector that would rank it against everything.
    embeddings: np.ndarray | None = None
    embedded: np.ndarray | None = None
    whitening: Whitening | None = None
    # Whether this snapshot is complete enough to answer with, by the same rule
    # `coverage()` reports to /status. Carried on the snapshot so the scan
    # endpoint can check it without a second aggregate query per request.
    ready: bool = False

    # Deliberately no __len__. A dataclass with one falls back to it for
    # truthiness, and a legitimately-rebuilt zero-row snapshot -- an
    # un-backfilled catalogue, or every fingerprint filtered out as malformed
    # -- has len() 0 and so is falsy despite being exactly what `_load` just
    # built. `snapshot or _EMPTY` would then discard that fresh empty
    # snapshot in favour of the stale `_EMPTY` sentinel (generation -1,
    # built_at 0.0). Callers that mean "is there a cached snapshot" must
    # check identity against None, not boolean-ness.


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
            Card.image_embedding,
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

    embeddings, embedded, whitening = _build_embeddings(usable)

    snapshot = Snapshot(
        packed=packed,
        embeddings=embeddings,
        embedded=embedded,
        whitening=whitening,
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
        "fingerprint index: loaded %d cards of %d with artwork (ready=%s, "
        "embedded=%s)",
        len(usable), with_image, snapshot.ready,
        int(embedded.sum()) if embedded is not None else "off",
    )
    return snapshot


def _fit_whitening(vectors: np.ndarray) -> Whitening:
    """PCA whitening fitted to the embedded rows, unsupervised.

    Decorrelating the axes and equalising their variance is worth +0.58 points
    of shortlist recall on its own, and projecting to WHITENING_DIMS cuts the
    resident index by a third at the same time.
    """
    mean = vectors.mean(axis=0)
    centred = vectors - mean
    covariance = (centred.T @ centred) / max(len(vectors) - 1, 1)
    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    order = np.argsort(-eigenvalues)[:min(WHITENING_DIMS, vectors.shape[1])]
    projection = eigenvectors[:, order] / np.sqrt(
        eigenvalues[order] + _WHITENING_EPSILON
    )
    return Whitening(
        mean=mean.astype(np.float32), projection=projection.astype(np.float32)
    )


def _build_embeddings(usable):
    """The whitened embedding index for `usable`, or (None, None, None).

    Returns nothing at all unless this installation has a model configured and
    enough rows carry a vector of one consistent length: a half-populated
    embedding index would rank the embedded rows against each other and bury
    every row the backfill has not reached yet, which is worse than not using
    embeddings at all.
    """
    if not usable or not card_embedding.available():
        return None, None, None

    vectors: list[np.ndarray | None] = []
    dims: int | None = None
    mismatched = 0
    for row in usable:
        vector = card_embedding.unpack(row.image_embedding, dims)
        if vector is not None and dims is None:
            dims = len(vector)
        elif vector is None and row.image_embedding:
            mismatched += 1
        vectors.append(vector)
    if mismatched:
        logger.warning(
            "fingerprint index: skipped %d embeddings of a different length "
            "(a changed model re-queues them; see card_embedding.source_marker)",
            mismatched,
        )
    if dims is None:
        return None, None, None

    embedded = np.array([v is not None for v in vectors], dtype=bool)
    present = int(embedded.sum())
    if present < MIN_EMBEDDED_CARDS:
        logger.info(
            "fingerprint index: %d embedded cards is below the %d needed to "
            "fit whitening; using the perceptual hash alone",
            present, MIN_EMBEDDED_CARDS,
        )
        return None, None, None

    raw = np.zeros((len(vectors), dims), dtype=np.float32)
    for position, vector in enumerate(vectors):
        if vector is not None:
            raw[position] = vector
    whitening = _fit_whitening(raw[embedded])
    # Whitened in place of the raw matrix rather than alongside it: at 42k rows
    # the raw 768-d copy is 130MB and is not needed again once fitted.
    whitened = whitening.apply(raw)
    whitened[~embedded] = 0.0
    return whitened, embedded, whitening


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
        return snapshot if snapshot is not None else _EMPTY

    blocking = snapshot is None
    if not _lock.acquire(blocking=blocking):
        # Someone else is rebuilding and we have something to answer with.
        for candidate in (_snapshot, snapshot):
            if candidate is not None:
                return candidate
        return _EMPTY
    try:
        # Re-check inside the lock; another request may have just rebuilt.
        if _is_stale(_snapshot, db):
            _snapshot = _load(db)
        return _snapshot if _snapshot is not None else _EMPTY
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
    hash, which is the numerator both fractions need. A row holding a hash but
    no artwork URL is a data anomaly rather than usable coverage; it would
    still be matchable, so this slightly understates the index size in exchange
    for fractions that cannot exceed 1.0.

    TWO fractions, because they answer different questions and only one of them
    is what a user means by "how much of my catalogue can this recognise":

    * `coverage` is over cards that HAVE artwork. It is the backfill's progress
      bar -- how much work is left to do -- and it reaches 1.0 while a fifth of
      the catalogue is still unmatchable.
    * `catalogue_coverage` is over every indexable card. On the reference
      catalogue these read 97.2% and 75.8%: 12,893 of 58,630 cards have a name
      and a number and no artwork at all, so no image method can ever match
      them and TCGdex mostly does not have the pictures. Showing the first one
      under a label like "catalogue fingerprinted" overstates what the scanner
      can do by twenty-one points.

    Readiness stays on `coverage`: it asks whether the backfill has done enough
    of the work available to it, which is not a question about catalogue gaps.
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
    catalogue_fraction = (with_hash / total) if total else 0.0
    return {
        "cards_total": total,
        "cards_with_image": with_image,
        "cards_fingerprinted": with_hash,
        "coverage": round(fraction, 4),
        "catalogue_coverage": round(catalogue_fraction, 4),
        "ready": _is_ready(with_hash, with_image),
    }
