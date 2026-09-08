"""Shared card upsert helper."""

from __future__ import annotations

import datetime

from sqlalchemy import event
from sqlalchemy.orm import Session

from models import Card, ImageCache, Set
from services import fingerprint_index
from services.price_utils import preserve_existing_prices_for_invalid_update


def invalidate_fingerprint_index_after_commit(db: Session) -> None:
    """Drop the in-memory fingerprint index once this session's work lands.

    Invalidating immediately is not enough: a rebuild triggered by a concurrent
    request can read the cards table between the invalidation and the commit,
    clear the flag, and cache a snapshot that is already out of date. Deferring
    to after_commit means the index is only marked stale once the new rows are
    actually visible to other sessions.
    """
    db.info["fingerprint_index_dirty"] = True
    if db.info.get("fingerprint_index_hooked"):
        return
    db.info["fingerprint_index_hooked"] = True

    @event.listens_for(db, "after_commit")
    def _flush(session):  # pragma: no cover - exercised via upsert_card tests
        if session.info.pop("fingerprint_index_dirty", False):
            fingerprint_index.invalidate()

    @event.listens_for(db, "after_rollback")
    def _drop(session):  # pragma: no cover - exercised via upsert_card tests
        session.info.pop("fingerprint_index_dirty", None)


def _apply_set_digital_flag(db: Session, card_data: dict) -> None:
    if card_data.get("is_digital") or not card_data.get("set_id"):
        return
    set_lang = card_data.get("lang") or "en"
    set_row = db.query(Set.is_digital).filter(
        Set.tcg_set_id == card_data["set_id"],
        Set.lang == set_lang,
    ).first()
    if set_row and set_row[0]:
        card_data["is_digital"] = True


def upsert_card(db: Session, card_data: dict) -> Card:
    """Insert or update a card row consistently across sync and API flows."""
    existing = db.query(Card).filter(Card.id == card_data["id"]).first()
    card_data["updated_at"] = datetime.datetime.utcnow()
    _apply_set_digital_flag(db, card_data)
    preserve_existing_prices_for_invalid_update(card_data, existing)
    has_api_image = bool(card_data.get("images_small") or card_data.get("images_large"))
    if existing:
        # A changed artwork URL invalidates the stored fingerprint. card_data
        # never carries image_phash, so without this the old hash would silently
        # survive and point at the previous picture.
        new_image = card_data.get("images_small")
        if existing.image_phash is not None and new_image != existing.images_small:
            existing.image_phash = None
        for key, value in card_data.items():
            if key != "id":
                setattr(existing, key, value)
        if has_api_image:
            existing.custom_image_url = None
            db.query(ImageCache).filter(ImageCache.image_key.in_([
                f"card:{existing.id}:small:custom",
                f"card:{existing.id}:large:custom",
            ])).delete(synchronize_session=False)
    else:
        existing = Card(**card_data)
        db.add(existing)
    # The in-memory fingerprint index no longer reflects the catalogue.
    invalidate_fingerprint_index_after_commit(db)
    return existing
