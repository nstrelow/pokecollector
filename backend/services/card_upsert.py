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

    The flag is cleared only by a rollback that ends the whole transaction. A
    SAVEPOINT rollback fires `after_rollback` too -- api/cards.py uses
    `begin_nested()` around parts of the custom-card migration -- and clearing
    it there dropped an invalidation for rows the enclosing transaction went on
    to commit. Over-invalidating costs one rebuild; under-invalidating serves
    the wrong card until MAX_AGE_SECONDS, so the nesting depth is tracked and
    only depth 0 may clear.
    """
    db.info["fingerprint_index_dirty"] = True
    if db.info.get("fingerprint_index_hooked"):
        return
    db.info["fingerprint_index_hooked"] = True

    @event.listens_for(db, "after_commit")
    def _flush(session):  # pragma: no cover - exercised via upsert_card tests
        # `after_commit` also fires when a SAVEPOINT commits (`begin_nested()`
        # exiting normally), and `in_nested_transaction()` is still true at
        # that point -- measured against the installed SQLAlchemy. Popping the
        # flag there invalidates before the enclosing transaction's writes are
        # actually visible to other sessions, and the real outer commit is
        # then left with no signal at all. Only depth 0 may flush, mirroring
        # the same guard `_drop` already needs for `after_rollback`.
        if session.in_nested_transaction():
            return
        if session.info.pop("fingerprint_index_dirty", False):
            fingerprint_index.invalidate()

    @event.listens_for(db, "after_rollback")
    def _drop(session):  # pragma: no cover - exercised via upsert_card tests
        # `after_rollback` also fires for `ROLLBACK TO SAVEPOINT`, where the
        # outer transaction -- and the write that set the flag -- survives.
        if session.in_nested_transaction():
            return
        session.info.pop("fingerprint_index_dirty", None)


def clear_stale_fingerprint(existing: Card, new_image: str | None) -> None:
    """Drop `existing`'s fingerprint if `new_image` is not what it was made from.

    Any writer that assigns a new `images_small` must call this, or it leaves a
    hash of the previous picture in place. That is not a recoverable mistake on
    its own: the backfill only looked at rows with a NULL hash, so a survivor
    was never revisited and went on matching photos of the old artwork at
    distance 0. `image_phash_source` is what makes it recoverable -- see
    `fingerprint_backfill.pending_cards`, which re-queues any row whose stored
    provenance no longer matches its URL even if this call is forgotten.
    """
    if existing.images_small == new_image:
        return
    existing.image_phash = None
    existing.image_phash_source = None


def apply_catalogue_fields(db: Session, existing: Card, parsed: dict) -> Card:
    """Copy parsed TCGdex fields onto an existing catalogue row, safely.

    Several call sites do their own field-by-field assignment instead of going
    through `upsert_card` (which also does price preservation and custom-image
    cleanup they do not want). They still change artwork URLs and still change
    what the index should hold, so they share this instead of each remembering
    two separate rules.
    """
    clear_stale_fingerprint(existing, parsed.get("images_small"))
    for key, value in parsed.items():
        if key != "id":
            setattr(existing, key, value)
    invalidate_fingerprint_index_after_commit(db)
    return existing


def add_catalogue_card(db: Session, card: Card) -> Card:
    """Insert a new catalogue row and tell the index it exists.

    A new row carries no stale hash, but the index will not contain it until it
    is rebuilt, so an insert is an invalidation just as much as an update is.
    """
    db.add(card)
    invalidate_fingerprint_index_after_commit(db)
    return card


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
        clear_stale_fingerprint(existing, card_data.get("images_small"))
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
