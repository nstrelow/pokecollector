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

from api.auth import get_current_user
from database import get_db
from models import User
from services import fingerprint_index
from services.card_fingerprint import (
    MAX_DISTANCE,
    SHORTLIST_MAX_DISTANCE,
    is_confident,
    match_probability,
    photo_hash_variants,
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


def _match_photo(
    raw: bytes,
    snapshot: fingerprint_index.Snapshot,
    *,
    sanitized: bool = False,
):
    """Sanitize, fingerprint and rank one upload. Runs off the event loop.

    `sanitized` is for the queue worker, whose bytes were already put through
    `sanitize_image_bytes` at enqueue time. Re-encoding them would cost a second
    generation of JPEG loss on the very image the hash is taken from.
    """
    data = raw if sanitized else sanitize_image_bytes(raw).data
    queries = photo_hash_variants(data)
    if not queries:
        return None
    return search_variants(
        queries,
        snapshot.packed,
        limit=SHORTLIST,
        max_distance=SHORTLIST_MAX_DISTANCE,
    )


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
            ranked = await run_in_threadpool(
                _match_photo, raw, snapshot, sanitized=sanitized
            )
    except ScanUploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if ranked is None:
        raise HTTPException(status_code=400, detail="Could not read the uploaded image.")

    confident = is_confident(ranked)
    rows = snapshot.rows

    # "Leader" means strictly closest, not merely first in the list. Two rows at
    # the same distance are two rows the hash cannot tell apart -- typically the
    # German and English printings of one artwork -- and which of them lands at
    # rank 1 is decided by row index, which carries no evidence at all. Scoring
    # one as the leader and the other as a tail entry turned that coin flip into
    # "43% versus 4%" on a real scan, which is not a small overstatement of a
    # weak signal but an invented one.
    best_distance = ranked[0][1] if ranked else None

    matches = []
    for row_index, distance in ranked:
        row = rows[row_index]
        matches.append({
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
            "_confidence": _confidence(distance),
            # Measured, not derived from the distance arithmetically -- see
            # card_fingerprint.match_probability.
            "_match_percent": match_probability(
                distance, leader=distance == best_distance
            ),
        })

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
