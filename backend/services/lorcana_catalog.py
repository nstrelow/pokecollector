"""Lorcana prints as `cards` rows (game = "lorcana"), from the catalog map that ships with the app.

Identity is the lorcascan print id `<lang>:<rest>` (`en:1-207`, `de:3-4a`); the map is `data/lorcana_dreamborn_map_v1.json`
(services/lorcana_dreamborn.py, docs/DREAMBORN-CSV.md). A print that is not in the map is refused, never guessed.
The `cards.id` of a Lorcana print is `lorcana:<rest>_<lang>`: the prefix keeps it apart from every TCGdex id, the
`_<lang>` suffix is the same composite form the rest of the app expects.

Names are the English full name (the map carries English only); images are not set yet (G3, per-game card detail).
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from models import Card
from services.lorcana_dreamborn import DreambornMap, MapRow, load_map

GAME = "lorcana"
ID_PREFIX = "lorcana:"


class UnknownPrint(KeyError):
    """The print id is not in the catalog map (or not in that language)."""


def card_id_for(print_id: str, dmap: DreambornMap | None = None) -> tuple[str, str, MapRow]:
    """(cards.id, lang, map row) for a print id; raises UnknownPrint."""
    dmap = dmap or load_map()
    try:
        lang, row = dmap.row_for_print_id(print_id)
    except KeyError as exc:
        raise UnknownPrint(str(exc)) from exc
    return f"{ID_PREFIX}{row.rest_id}_{lang}", lang, row


def print_id_of(card_id: str) -> str:
    """`lorcana:1-1_de` -> `de:1-1`."""
    if not card_id.startswith(ID_PREFIX) or "_" not in card_id:
        raise ValueError(f"not a Lorcana card id: {card_id!r}")
    rest, _, lang = card_id[len(ID_PREFIX):].rpartition("_")
    return f"{lang}:{rest}"


def card_values(card_id: str, lang: str, row: MapRow) -> dict:
    return dict(
        id=card_id, tcg_card_id=row.rest_id, name=row.full_name, set_id=row.set_code,
        number=f"{row.number}{row.variant}", rarity=row.rarity_en,
        types=[ink for ink in row.ink.split("-") if ink] or None,
        supertype="Lorcana", subtypes=[row.variant_kind] if row.variant_kind != "normal" else None,
        lang=lang, game=GAME, is_custom=False, is_digital=False,
        variants_normal=row.variant_kind not in ("enchanted", "iconic", "epic"), variants_holo=True,
        variants_reverse=False, variants_first_edition=False,
    )


def ensure_lorcana_card(db: Session, print_id: str, dmap: DreambornMap | None = None) -> Card:
    """The `cards` row of a Lorcana print, created on first use. `db` must be scoped to Lorcana."""
    card_id, lang, row = card_id_for(print_id, dmap)
    # unscoped on purpose: a duplicate insert would fail on the primary key, a foreign game's row must be an error
    card = db.query(Card).filter(Card.id == card_id).execution_options(game="*").first()
    if card is not None and card.game != GAME:
        raise ValueError(f"{card_id!r} exists as a {card.game!r} card")
    if card is None:
        card = Card(**card_values(card_id, lang, row))
        db.add(card)
        db.flush()
    return card
