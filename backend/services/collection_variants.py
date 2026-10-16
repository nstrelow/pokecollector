from enum import Enum


class PokemonVariant(str, Enum):
    """The Pokemon contract: the four physical variants it always had. Every Pokemon request/response schema
    (schemas.py) uses this class, so a Pokemon endpoint refuses 'Foil' with the same 422 text as any other bad value."""
    NORMAL = "Normal"
    HOLO = "Holo"
    REVERSE_HOLO = "Reverse Holo"
    FIRST_EDITION = "First Edition"


class CollectionVariant(str, Enum):
    """Everything the `collection` table may hold: the Pokemon four plus Foil (Lorcana, E3 / PicaLens #62, owner
    2026-10-16). Foil is never accepted by a Pokemon code path."""
    NORMAL = "Normal"
    HOLO = "Holo"
    REVERSE_HOLO = "Reverse Holo"
    FIRST_EDITION = "First Edition"
    FOIL = "Foil"


CANONICAL_VARIANTS = tuple(variant.value for variant in PokemonVariant)
CANONICAL_VARIANT_SET = frozenset(CANONICAL_VARIANTS)
# What the `collection` table may hold: the Pokemon four plus Foil (Lorcana). Other tables keep the four.
COLLECTION_TABLE_VARIANTS = tuple(variant.value for variant in CollectionVariant)
LORCANA_VARIANTS = (CollectionVariant.NORMAL.value, CollectionVariant.FOIL.value)


def normalize_collection_variant(variant: str | Enum | None) -> str:
    """Return a canonical collection variant or reject the value.

    Blank legacy inputs remain equivalent to Normal, but non-blank values must
    exactly match the public enum. This keeps pricing, effects, imports, and
    inventory identity on the same four-value contract.
    """
    value = str(variant.value if isinstance(variant, Enum) else (variant or "")).strip()
    value = value or PokemonVariant.NORMAL.value
    if value not in CANONICAL_VARIANT_SET:
        raise ValueError(
            "variant must be blank or one of: "
            + ", ".join(CANONICAL_VARIANTS)
            + ". Put specific physical printing descriptions in printing_details."
        )
    return value
