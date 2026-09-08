"""Offline card recognition, with no vision provider involved.

`api/recognize.py` cannot run without a configured LLM: it calls the provider
before anything else, and raises 400 when no credential is present. It also
fails outright (422) when the model cannot read a card name, because the name is
what its TCGdex search is keyed on.

This endpoint takes the other route. It matches the photo's fingerprint against
the locally stored catalogue, needing no credential, no network, and no name.

The response is shaped like /recognize's so a client can reuse the same review
component, but nothing calls it yet: no frontend code references
`recognize/local`. Wiring up the UI is a separate change.

Everything here is CPU work -- decode, card detection, hash, scan -- and the
production image runs a single uvicorn worker, so it is pushed onto the thread
pool rather than run on the event loop.
"""
from __future__ import annotations

import logging

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
    fingerprint_photo,
    is_confident,
    search,
)
from services.scan_storage import (
    MAX_FILE_BYTES,
    ScanUploadError,
    read_limited_upload,
    sanitize_image_bytes,
)

logger = logging.getLogger(__name__)

router = APIRouter()

SHORTLIST = 12


def _confidence(distance: int) -> str:
    """Coarse label for the UI, so the list is not silently ordered."""
    if distance <= 8:
        return "high"
    if distance <= MAX_DISTANCE:
        return "medium"
    return "low"


def _match_photo(raw: bytes, snapshot: fingerprint_index.Snapshot):
    """Sanitize, fingerprint and rank one upload. Runs off the event loop."""
    sanitized = sanitize_image_bytes(raw)
    query = fingerprint_photo(sanitized.data)
    if query is None:
        return None
    return search(
        query,
        snapshot.packed,
        limit=SHORTLIST,
        max_distance=SHORTLIST_MAX_DISTANCE,
    )


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

    # A rebuild reads the whole cards table; never do that on the loop.
    snapshot = await run_in_threadpool(fingerprint_index.get, db)
    if not snapshot.rows:
        raise HTTPException(
            status_code=503,
            detail=(
                "No card fingerprints are stored yet. Offline recognition "
                "becomes available once the fingerprint job has run."
            ),
        )

    try:
        ranked = await run_in_threadpool(_match_photo, raw, snapshot)
    except ScanUploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if ranked is None:
        raise HTTPException(status_code=400, detail="Could not read the uploaded image.")

    confident = is_confident(ranked)
    rows = snapshot.rows

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


@router.get("/recognize/local/status")
def local_recognition_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Whether offline recognition is usable, and how complete it is."""
    return fingerprint_index.coverage(db)
