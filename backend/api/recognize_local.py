"""Offline card recognition, with no vision provider involved.

`api/recognize.py` cannot run without a configured LLM: it calls the provider
before anything else, and raises 400 when no credential is present. It also
fails outright (422) when the model cannot read a card name, because the name is
what its TCGdex search is keyed on.

This endpoint takes the other route. It matches the photo's fingerprint against
the locally stored catalogue, needing no credential, no network, and no name.

The response is shaped like /recognize's so a client can reuse the same review
component. Two callers reach it: this endpoint, for a direct single-card scan,
and the background scan worker, which calls `recognize_sanitized_card_locally`
for users who have turned the "Scanner v2 (Beta)" toggle on.

Everything here is CPU work -- decode, card detection, hash, scan -- and the
production image runs a single uvicorn worker, so it is pushed onto the thread
pool rather than run on the event loop.
"""
from __future__ import annotations

import asyncio
import weakref
import logging
import os

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

import numpy as np

from api.auth import get_current_user
from database import get_db
from models import User
from services import card_embedding, fingerprint_index
from services.card_fingerprint import (
    MAX_DISTANCE,
    SHORTLIST_MAX_DISTANCE,
    distances,
    fuse_similarity,
    is_confident,
    match_leader_counts,
    match_probability,
    photo_hash_variants,
    rank_fused,
    search_variants,
)
from services.scan_storage import (
    MAX_FILE_BYTES,
    ScanUploadError,
    read_limited_upload,
    sanitize_image_bytes,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# How many candidates the review UI is offered. This is the number the
# accuracy figures in services/card_fingerprint are quoted at (91.62% of photos
# have the right artwork inside the top 12), so it is part of what the endpoint
# promises, not an arbitrary page size.
SHORTLIST = 12

# The shortlist holds twelve distinct ARTWORKS, not twelve rows. 5,313 rows
# share an artwork URL with another row and 16,897 sit in 8,305 identical-hash
# groups, so an ungrouped shortlist spent an average of 2.43 of its twelve
# slots showing one card twice -- 93.9% of shortlists had at least one
# duplicate. Collapsing them is worth +0.63 points of recall (90.96% -> 91.59%)
# for the same twelve tiles, because the freed slots go to real candidates.
#
# Rows are GROUPED, never dropped. The shortlist exists so the user picks the
# printing, and the language is part of the printing; a collapsed group carries
# its other printings in `_other_printings` so they stay one click away.
#
# So the scan has to rank more rows than it shows. Four times is comfortably
# enough: the largest identical-hash group in the real catalogue is 6, and a
# shortlist would have to be almost entirely duplicates to exhaust 48 rows
# before finding 12 distinct artworks.
GROUP_OVERSCAN = 4

# Distance at or below which a candidate is labelled "high". Two-thirds of
# MAX_DISTANCE (12): on the benchmark the correct row sits at a median distance
# of 4, so this separates "this is the card" from "this is in the running".
HIGH_CONFIDENCE_DISTANCE = 8


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# Any authenticated caller can reach this endpoint, and _match_photo (decode,
# crop, hash, scan the whole fingerprint index) runs in the shared AnyIO
# worker threadpool -- capped at 40 tasks by default -- alongside every other
# synchronous endpoint and dependency. The production image runs a single
# uvicorn worker, so unbounded concurrent recognitions can starve unrelated
# requests of that same pool. Deployment-configurable so a bigger box can
# raise it.
MAX_CONCURRENT_LOCAL_RECOGNITIONS = _positive_int("LOCAL_RECOGNITION_MAX_CONCURRENCY", 4)
# One semaphore per event loop rather than one for the module. An
# `asyncio.Semaphore` binds to the loop that first awaits it, so a module-level
# instance raises "bound to a different event loop" as soon as a second loop
# uses it -- which the test suite already does, one loop per TestClient. The
# map is weak so a finished loop does not keep its semaphore alive.
_recognition_semaphores: "weakref.WeakKeyDictionary[object, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def _recognition_semaphore() -> asyncio.Semaphore:
    """The concurrency limiter for the currently running loop."""
    loop = asyncio.get_running_loop()
    semaphore = _recognition_semaphores.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_LOCAL_RECOGNITIONS)
        _recognition_semaphores[loop] = semaphore
    return semaphore


def _confidence(distance: int) -> str:
    """Coarse label for the UI, so the list is not silently ordered."""
    if distance <= HIGH_CONFIDENCE_DISTANCE:
        return "high"
    if distance <= MAX_DISTANCE:
        return "medium"
    return "low"


def _artwork_key(row: dict) -> str:
    """What makes two catalogue rows the same picture.

    `tcg_card_id` is the TCGdex id with the language stripped, so the German and
    English rows of one card share it while two different cards never do. A row
    missing it -- which the catalogue should not contain, but a hand-imported
    one might -- keys on its own id and therefore forms a group of one, rather
    than silently merging with every other row missing it.
    """
    return str(row.get("tcg_card_id") or "") or f"id:{row['id']}"


def _group_by_artwork(
    ranked: list[tuple[int, int]], rows: tuple[dict, ...], limit: int
) -> list[list[tuple[int, int]]]:
    """Collapse rows of one artwork into one group, best group first.

    Groups keep the rank order they were found in, so a group's first entry is
    its closest row and the group list is still ordered by distance. Stops once
    `limit` distinct artworks have been collected; rows of an artwork already
    complete are still attached to it, but no new group starts.
    """
    groups: list[list[tuple[int, int]]] = []
    by_key: dict[str, list[tuple[int, int]]] = {}
    for row_index, distance in ranked:
        key = _artwork_key(rows[row_index])
        group = by_key.get(key)
        if group is None:
            if len(groups) >= limit:
                continue
            group = []
            by_key[key] = group
            groups.append(group)
        group.append((row_index, distance))
    return groups


def _candidate(row: dict, distance: int, percent: int | None) -> dict:
    """One shortlist tile, shaped like a /recognize match plus local-only fields.

    `percent` is None when this ranking has no calibrated curve behind it; the
    review UI already renders no badge for a candidate without one, which is
    what the provider scanner's candidates have always looked like.
    """
    return {
        "id": row["id"],
        "tcg_card_id": row["tcg_card_id"],
        "name": row["name"],
        "number": row["number"],
        "set_id": row["set_id"],
        "lang": row["lang"],
        "_lang": row["lang"],
        "rarity": row["rarity"],
        "image": row["image"],
        # Extra, local-only fields. The review UI ignores what it does not know.
        "_distance": distance,
        # Also a distance rule, so it goes quiet for the same reason the
        # percentage does: it would paint a correctly-found card "low".
        "_confidence": _confidence(distance) if percent is not None else None,
        # Measured, not derived from the distance arithmetically -- see
        # card_fingerprint.match_probability.
        "_match_percent": percent,
    }


def _match_photo(
    raw: bytes,
    snapshot: fingerprint_index.Snapshot,
    *,
    sanitized: bool = False,
):
    """Sanitize, fingerprint and rank one upload. Runs off the event loop.

    Returns `(ranked, fused)`, where `fused` says whether the accurate path was
    used -- the caller needs it, because the confidence gate is only calibrated
    for the other one.

    `sanitized` is for the queue worker, whose bytes were already put through
    `sanitize_image_bytes` at enqueue time. Re-encoding them would cost a second
    generation of JPEG loss on the very image the hash is taken from.
    """
    data = raw if sanitized else sanitize_image_bytes(raw).data
    queries = photo_hash_variants(data)
    if not queries:
        return None
    limit = SHORTLIST * GROUP_OVERSCAN

    if snapshot.embeddings is None:
        return search_variants(
            queries, snapshot.packed, limit=limit,
            max_distance=SHORTLIST_MAX_DISTANCE,
        ), False

    views = card_embedding.embed_photo_views(data)
    if not views:
        # The model is configured but this photo did not survive it. Ranking on
        # the hash alone is exactly what a row with no embedding already does,
        # so this degrades rather than fails.
        return search_variants(
            queries, snapshot.packed, limit=limit,
            max_distance=SHORTLIST_MAX_DISTANCE,
        ), False

    whitened = snapshot.whitening.apply(np.asarray(views, dtype=np.float32))
    # Best of the views, not their average: the crop and the whole frame are
    # two framings of one card, and whichever the detector got right should not
    # be diluted by the one it got wrong.
    similarity = (whitened @ snapshot.embeddings.T).max(axis=0)

    hamming = distances(queries[0], snapshot.packed)
    for query in queries[1:]:
        np.minimum(hamming, distances(query, snapshot.packed), out=hamming)

    scores = fuse_similarity(similarity, hamming, snapshot.embedded)
    return rank_fused(
        scores, hamming, limit=limit, max_distance=SHORTLIST_MAX_DISTANCE
    ), True


async def recognize_local_photo(
    db: Session,
    raw: bytes,
    *,
    sanitized: bool = False,
) -> dict:
    """Match one photo against the local index, shaped like /recognize's reply.

    Raises `HTTPException` the same way the endpoint does, because the scan
    worker maps those status codes onto its retry policy: 503 (index still
    building) is transient and comes back when the backfill has caught up, while
    400 (unreadable photo) is permanent.
    """
    # A rebuild reads the whole cards table; never do that on the loop.
    snapshot = await run_in_threadpool(fingerprint_index.get, db)
    # Not "is the index non-empty" but the same readiness rule /status reports.
    # A partly built index gives confident-looking answers off whatever fraction
    # of the catalogue happens to be hashed so far, and `is_confident`'s margin
    # test has nothing meaningful to be a margin against on a small one.
    if not snapshot.ready:
        raise HTTPException(
            status_code=503,
            detail=(
                "Card fingerprints are still being built. Offline recognition "
                "becomes available once enough of the catalogue is covered; see "
                "recognize/local/status for progress."
            ),
        )

    try:
        async with _recognition_semaphore():
            result = await run_in_threadpool(
                _match_photo, raw, snapshot, sanitized=sanitized
            )
    except ScanUploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if result is None:
        raise HTTPException(status_code=400, detail="Could not read the uploaded image.")
    ranked, fused = result

    # Judged on the ungrouped ranking, deliberately. Collapsing a German and an
    # English printing into one tile would turn a zero margin into a wide one
    # and manufacture confidence on exactly the case the hash cannot decide.
    # `is_confident` only reads the first two entries, so the wider pool the
    # grouping needs gives it the identical answer it had at limit 12.
    #
    # And NOT judged at all on the fused ranking. `MIN_MARGIN` is a number of
    # bits, measured on a shortlist ordered by those bits; on a fused ranking
    # the first two entries are not ordered by distance and the difference
    # between them means nothing. A margin on the fused score is the right
    # replacement and is not derived yet -- measured so far, a z-score margin
    # of 1.5 fires on 41.4% of photos at 99.76% artwork and 98.03% printing
    # precision, against the hash gate's 15.7% at 100% and 99.10%. That is more
    # coverage for less precision, and which of the two to want is a decision
    # nobody has made. Until it is made, the accurate path never claims
    # confidence: its shortlist finds the right artwork 99.90% of the time
    # against the hash's 90.67%, so the user is being asked to confirm a much
    # better list, not left without an answer.
    confident = False if fused else is_confident(ranked)
    rows = snapshot.rows

    groups = _group_by_artwork(ranked, rows, SHORTLIST)
    leader_counts = match_leader_counts([group[0] for group in groups])

    matches = []
    for group, leaders in zip(groups, leader_counts):
        # One percentage for the whole group. Every printing in it is the same
        # picture, so "this is the right artwork" is true of all of them or none
        # -- unlike two different cards at the same distance, which is what
        # `leaders` divides the odds between.
        #
        # None on the fused path, and the UI then renders no badge at all.
        # `match_probability` reads a table indexed by Hamming distance, and on
        # the fused path the tile was chosen by the embedding, so its distance
        # is whatever the hash happened to think -- frequently 18 to 22, deep in
        # the tail. Live, that printed "1%" beside cards the scanner had found
        # correctly at tile 1. That is the original defect inverted: a measured
        # curve applied to a population it was not measured on, understating
        # this time instead of overstating. A number that says 1% about a
        # correct answer teaches the user to ignore the number.
        percent = (
            None if fused
            else match_probability(group[0][1], leaders=leaders)
        )
        printings = [_candidate(rows[i], distance, percent)
                     for i, distance in group]
        match = printings[0]
        if len(printings) > 1:
            # Grouped, not discarded: the user still has to choose the printing,
            # and the language is part of it.
            match["_other_printings"] = printings[1:]
        matches.append(match)

    return {
        # No text was read, so nothing is claimed about the printed identity.
        "recognized": {
            "name": None, "name_en": None, "number": None,
            "number_local": None, "number_total": None, "set_code": None,
            "regulation_mark": None, "card_type": None, "hp": None,
            "language": None, "artist": None,
        },
        "matches": matches,
        "_number_match_count": 0,
        "_identity_confident": confident,
        "_identity_decision": "local_image" if confident else None,
        "_source": "local_fingerprint",
    }


async def recognize_sanitized_card_locally(db: Session, image_bytes: bytes) -> dict:
    """Offline recognition for bytes the scan queue already sanitized."""
    return await recognize_local_photo(db, image_bytes, sanitized=True)


@router.post("/recognize/local")
async def recognize_card_locally(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        raw = await read_limited_upload(file, remaining_job_bytes=MAX_FILE_BYTES)
    except ScanUploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return await recognize_local_photo(db, raw)


@router.get("/recognize/local/status")
def local_recognition_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Whether offline recognition is usable, and how complete it is."""
    return fingerprint_index.coverage(db)
