from sqlalchemy import (
    Column, String, Integer, Float, DateTime, Date, Boolean,
    CheckConstraint, ForeignKey, Text, JSON, UniqueConstraint, LargeBinary, Index, Table
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func, text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from database import Base

POKEDEX_JSON = JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")
PORTFOLIO_CALCULATION_VERSION = 2
COLLECTION_VARIANT_CHECK = "variant IN ('Normal', 'Holo', 'Reverse Holo', 'First Edition')"
# The `collection` table alone also takes 'Foil' (Lorcana, E3 / PicaLens #62); product_cards, ledger and trade items
# keep the four-value check above.
COLLECTION_ITEM_VARIANT_CHECK = "variant IN ('Normal', 'Holo', 'Reverse Holo', 'First Edition', 'Foil')"


class Set(Base):
    __tablename__ = "sets"

    id = Column(String, primary_key=True)    # Composite DB key: "sv1_en" / "sv1_zh-tw"
    tcg_set_id = Column(String)              # Original TCGdex set ID: "sv1"
    name = Column(String, nullable=False)
    series = Column(String)
    release_date = Column(String)
    total = Column(Integer, default=0)
    printed_total = Column(Integer, default=0)
    images_symbol = Column(String)
    images_logo = Column(String)
    abbreviation = Column(String, nullable=True)
    is_new = Column(Boolean, default=False)
    is_digital = Column(Boolean, default=False)
    lang = Column(String, default="en")      # TCGdex language code, NEVER "both"
    game = Column(String, nullable=False, default="pokemon", server_default=sql_text("'pokemon'"))  # game axis (E3, PicaLens #62)
    updated_at = Column(DateTime, default=func.now())

    # Relationship to cards via tcg_set_id (no DB-level FK, joined in Python)
    # Use explicit primaryjoin so SQLAlchemy can resolve the join
    cards = relationship(
        "Card",
        primaryjoin="Set.tcg_set_id == foreign(Card.set_id)",
        foreign_keys="[Card.set_id]",
        lazy="dynamic",
        viewonly=True,
        overlaps="set_ref",
    )


class Card(Base):
    __tablename__ = "cards"

    id = Column(String, primary_key=True)          # Composite DB key: "sv1-1_en" / "sv1-1_zh-tw"
    tcg_card_id = Column(String, nullable=True)    # Original TCGdex ID "sv1-1"; NULL for custom cards
    name = Column(String, nullable=False)
    set_id = Column(String, nullable=True)   # Original TCGdex set ID (no FK constraint)
    number = Column(String)
    rarity = Column(String)
    types = Column(JSON)
    supertype = Column(String)
    subtypes = Column(JSON)
    hp = Column(String)
    artist = Column(String)
    stage = Column(String)
    evolve_from = Column(String)
    suffix = Column(String)
    trainer_type = Column(String)
    energy_type = Column(String)
    card_effect = Column(String)
    regulation_mark = Column(String)
    attacks = Column(JSON)
    abilities = Column(JSON)
    weaknesses = Column(JSON)
    resistances = Column(JSON)
    dex_ids = Column(POKEDEX_JSON)
    pokedex_entry_ids = Column(POKEDEX_JSON)
    cardmarket_products = Column(POKEDEX_JSON)
    retreat = Column(Integer)
    playable_fingerprint = Column(String)
    images_small = Column(String)
    images_large = Column(String)
    image_source_lang = Column(String, nullable=True)  # Set when images are copied from another TCGdex language
    data_source_lang = Column(String, nullable=True)   # Set when metadata is copied from another TCGdex language
    custom_image_url = Column(String, nullable=True)   # Manual temporary fallback while TCGdex has no image
    is_custom = Column(Boolean, default=False)
    custom_owner_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    is_shared_template = Column(Boolean, default=False, nullable=False)
    custom_source_card_id = Column(String, nullable=True, index=True)
    is_digital = Column(Boolean, default=False)
    lang = Column(String, default="en")      # TCGdex language code
    game = Column(String, nullable=False, default="pokemon", server_default=sql_text("'pokemon'"))  # game axis (E3, PicaLens #62)
    # Cardmarket EUR prices
    price_market = Column(Float)
    price_low = Column(Float)
    price_mid = Column(Float)
    price_high = Column(Float)
    price_trend = Column(Float)
    price_avg1 = Column(Float)
    price_avg7 = Column(Float)
    price_avg30 = Column(Float)
    # Cardmarket EUR holo prices
    price_market_holo = Column(Float)
    price_low_holo = Column(Float)
    price_trend_holo = Column(Float)
    price_avg1_holo = Column(Float)
    price_avg7_holo = Column(Float)
    price_avg30_holo = Column(Float)
    # TCGPlayer USD prices — normal variant
    price_tcg_normal_low = Column(Float)
    price_tcg_normal_mid = Column(Float)
    price_tcg_normal_high = Column(Float)
    price_tcg_normal_market = Column(Float)
    # TCGPlayer USD prices — reverse holofoil variant
    price_tcg_reverse_low = Column(Float)
    price_tcg_reverse_mid = Column(Float)
    price_tcg_reverse_market = Column(Float)
    # TCGPlayer USD prices — holofoil variant
    price_tcg_holo_low = Column(Float)
    price_tcg_holo_mid = Column(Float)
    price_tcg_holo_market = Column(Float)
    price_source_lang = Column(String, nullable=True)  # Set when prices are copied from another TCGdex language
    last_price_sync_attempt_at = Column(DateTime, nullable=True)
    last_price_sync_success_at = Column(DateTime, nullable=True)
    last_metadata_enrichment_attempt_at = Column(DateTime, nullable=True, index=True)
    # Card variants from TCGdex
    variants_normal = Column(Boolean)
    variants_reverse = Column(Boolean)
    variants_holo = Column(Boolean)
    variants_first_edition = Column(Boolean)
    updated_at = Column(DateTime, default=func.now())

    # Relationship to Set via tcg_set_id (viewonly, no DB FK)
    set_ref = relationship(
        "Set",
        primaryjoin="and_(Set.tcg_set_id == foreign(Card.set_id), Set.lang == foreign(Card.lang))",
        foreign_keys="[Card.set_id, Card.lang]",
        uselist=False,
        viewonly=True,
        overlaps="cards",
    )
    collection_items = relationship("CollectionItem", back_populates="card", lazy="dynamic")
    wishlist_items = relationship("WishlistItem", back_populates="card", lazy="dynamic")
    price_history = relationship("PriceHistory", back_populates="card", lazy="dynamic")
    binder_cards = relationship("BinderCard", back_populates="card", lazy="dynamic")
    custom_owner = relationship("User", foreign_keys=[custom_owner_id])


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_public_handle", "public_handle", unique=True),
        CheckConstraint(
            "wishlist_visibility IN ('private', 'trade_matches', 'public')",
            name="ck_users_wishlist_visibility",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String, unique=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    role = Column(String, default="trainer")  # "admin" or "trainer"
    is_active = Column(Boolean, default=True)
    avatar_id = Column(Integer, nullable=True)  # Pokemon number (1-151) for avatar sprite
    public_handle = Column(String, nullable=True)
    is_profile_public = Column(Boolean, default=False, nullable=False)
    public_show_values = Column(Boolean, default=False, nullable=False)
    wishlist_visibility = Column(String, default="private", nullable=False)
    must_change_password = Column(Boolean, default=False)
    created_at = Column(DateTime, default=func.now())


collection_printing_detail_tags = Table(
    "collection_printing_detail_tags",
    Base.metadata,
    Column(
        "collection_item_id",
        Integer,
        ForeignKey("collection.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "printing_detail_tag_id",
        Integer,
        ForeignKey("printing_detail_tags.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Index("ix_collection_printing_detail_tag", "printing_detail_tag_id"),
)

product_card_printing_detail_tags = Table(
    "product_card_printing_detail_tags",
    Base.metadata,
    Column("product_card_id", Integer, ForeignKey("product_cards.id", ondelete="CASCADE"), primary_key=True),
    Column("printing_detail_tag_id", Integer, ForeignKey("printing_detail_tags.id", ondelete="CASCADE"), primary_key=True),
    Index("ix_product_card_printing_detail_tag", "printing_detail_tag_id"),
)

product_ledger_printing_detail_tags = Table(
    "product_ledger_printing_detail_tags",
    Base.metadata,
    Column("product_ledger_entry_id", Integer, ForeignKey("product_ledger_entries.id", ondelete="CASCADE"), primary_key=True),
    Column("printing_detail_tag_id", Integer, ForeignKey("printing_detail_tags.id", ondelete="CASCADE"), primary_key=True),
    Index("ix_product_ledger_printing_detail_tag", "printing_detail_tag_id"),
)

trade_item_printing_detail_tags = Table(
    "trade_item_printing_detail_tags",
    Base.metadata,
    Column("trade_item_id", Integer, ForeignKey("trade_items.id", ondelete="CASCADE"), primary_key=True),
    Column("printing_detail_tag_id", Integer, ForeignKey("printing_detail_tags.id", ondelete="CASCADE"), primary_key=True),
    Index("ix_trade_item_printing_detail_tag", "printing_detail_tag_id"),
)


class PrintingDetailTag(Base):
    """Reusable, user-scoped descriptive metadata for a physical card copy."""

    __tablename__ = "printing_detail_tags"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(80), nullable=False)
    # NFKD/case-fold normalization can expand an 80-character display name
    # (for example, ligatures), so it is not indexed directly. The fixed-size
    # digest is the user-scoped uniqueness key and avoids PostgreSQL index-size
    # failures for valid Unicode names.
    normalized_name = Column(String(1440), nullable=False)
    normalized_key = Column(String(64), nullable=False)
    created_at = Column(DateTime, default=func.now(), nullable=False)

    collection_items = relationship(
        "CollectionItem",
        secondary=collection_printing_detail_tags,
        back_populates="printing_detail_tags",
    )
    product_cards = relationship("ProductCard", secondary=product_card_printing_detail_tags, back_populates="printing_detail_tags")
    product_ledger_entries = relationship("ProductLedgerEntry", secondary=product_ledger_printing_detail_tags, back_populates="printing_detail_tags")
    trade_items = relationship("TradeItem", secondary=trade_item_printing_detail_tags, back_populates="printing_detail_tags")

    __table_args__ = (
        UniqueConstraint("user_id", "normalized_key", name="uq_printing_detail_tag_user_key"),
        CheckConstraint("length(name) BETWEEN 1 AND 80", name="ck_printing_detail_tag_name_length"),
        CheckConstraint("length(normalized_name) BETWEEN 1 AND 1440", name="ck_printing_detail_tag_normalized_length"),
        CheckConstraint("length(normalized_key) = 64", name="ck_printing_detail_tag_key_length"),
    )


class CollectionItem(Base):
    __tablename__ = "collection"

    id = Column(Integer, primary_key=True, autoincrement=True)
    card_id = Column(String, ForeignKey("cards.id", ondelete="SET NULL"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    quantity = Column(Integer, default=1)
    condition = Column(String, default="NM")  # Mint/NM/LP/MP/HP
    variant = Column(String, nullable=False, default="Normal")  # Normal/Holo/Reverse Holo/First Edition; Foil = Lorcana only
    purchase_price = Column(Float)
    lang = Column(String, default="en")  # fixed TCGdex card language
    game = Column(String, nullable=False, default="pokemon", server_default=sql_text("'pokemon'"))  # game axis (E3, PicaLens #62)
    added_at = Column(DateTime, default=func.now())

    card = relationship("Card", back_populates="collection_items")
    printing_detail_tags = relationship(
        "PrintingDetailTag",
        secondary=collection_printing_detail_tags,
        back_populates="collection_items",
        lazy="selectin",
        order_by="PrintingDetailTag.name",
    )

    __table_args__ = (
        CheckConstraint(COLLECTION_ITEM_VARIANT_CHECK, name="ck_collection_variant"),
    )

    @property
    def printing_details(self):
        return self.printing_detail_tags


class CollectionCardPhoto(Base):
    """One private photograph per owner and catalogue card.

    Roughly one card in fourteen — trainer kits, Japanese printings, the newest
    energy subsets — has no TCGdex image, and the collection shows a card back
    for it. The scanner already took a photograph of the physical card, so this
    keeps that photo instead of discarding it on resolve.

    Deliberately keyed by owner and card rather than stored on the shared card:

      * `cards` is a catalogue shared by every user of the instance, and
        `/api/images` is mounted without authentication (see api/images.py,
        which gates on set/language visibility rather than on a user). A photo
        stored there would be served to anyone who can reach the instance.
      * A photograph of a card is also a photograph of whatever it was resting
        on. That is the owner's, not the catalogue's.

    All grouped collection rows for the same card share this photo. That matches
    catalogue artwork: condition, variant, price and quantity do not create a
    different printing. The bytes remain in a separate authenticated table so
    ordinary collection queries only ever load a yes/no flag.
    """
    __tablename__ = "collection_card_photos"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    card_id = Column(String, ForeignKey("cards.id", ondelete="CASCADE"), nullable=False, index=True)
    data = Column(LargeBinary, nullable=False)
    content_type = Column(String, default="image/jpeg")
    created_at = Column(DateTime, default=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "card_id", name="uq_collection_card_photo_user_card"),
    )


class WishlistItem(Base):
    __tablename__ = "wishlist"

    id = Column(Integer, primary_key=True, autoincrement=True)
    card_id = Column(String, ForeignKey("cards.id", ondelete="SET NULL"), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    quantity = Column(Integer, default=1, nullable=False)
    price_alert_above = Column(Float)
    price_alert_below = Column(Float)
    notified_at = Column(DateTime)
    created_at = Column(DateTime, default=func.now())

    card = relationship("Card", back_populates="wishlist_items")

    __table_args__ = (
        CheckConstraint("quantity >= 1 AND quantity <= 99", name="ck_wishlist_quantity_range"),
        UniqueConstraint("user_id", "card_id", name="uq_wishlist_user_card"),
    )


class PriceHistory(Base):
    __tablename__ = "price_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    card_id = Column(String, ForeignKey("cards.id"), nullable=False)
    date = Column(Date, nullable=False)
    price_low = Column(Float)
    price_mid = Column(Float)
    price_high = Column(Float)
    price_market = Column(Float)
    price_trend = Column(Float)

    card = relationship("Card", back_populates="price_history")

    __table_args__ = (UniqueConstraint("card_id", "date", name="uq_price_history_card_date"),)


class GamePrice(Base):
    """One price observation for a print of a non-Pokemon game (E3, PicaLens #62). Append-only.

    Pokemon keeps its prices in the `cards.price_*` columns and `price_history` (Cardmarket EUR, TCGplayer USD),
    unchanged. Every other game writes here: one row per (game, print, source, finish, day), never updated; the
    newest `price_date` is the current price. Daily full rows are kept on purpose (owner 2026-10-16): about 3,000 English
    prints x 2 finishes x 365 days = roughly 2.5 million rows a year. Keyed by print id, not by `cards.id`, because a price exists before
    anybody owns the card. A row is the source's number as published: `amount` NULL means the source has no price
    for that finish (never 0).
    """
    __tablename__ = "game_prices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    game = Column(String, nullable=False)
    print_id = Column(String, nullable=False)       # "en:1-1" (lorcascan scheme); the source's own print, e.g. English
    source = Column(String, nullable=False)         # "lorcast" (TCGplayer market price, passed through by Lorcast)
    currency = Column(String, nullable=False)       # "USD"
    finish = Column(String, nullable=False)         # "normal" | "foil"
    amount = Column(Float, nullable=True)
    price_date = Column(Date, nullable=False)       # the day the source file is for (UTC)
    fetched_at = Column(String, nullable=True)      # ISO time the source file says it was fetched
    source_file = Column(String, nullable=True)     # file name it was read from (provenance)

    __table_args__ = (
        UniqueConstraint("game", "print_id", "source", "finish", "price_date", name="uq_game_prices_obs"),
        Index("ix_game_prices_lookup", "game", "print_id", "finish", "price_date"),
        # the per-day questions: "which rows of this day exist" (idempotent import) and the status line (newest day)
        Index("ix_game_prices_day", "game", "source", "price_date"),
    )


class Binder(Base):
    __tablename__ = "binders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    description = Column(Text)
    color = Column(String, default="#EE1515")
    binder_type = Column(String, default="collection")  # "collection", "wishlist", "deck" (planned), or "physical_deck"
    format = Column(String, nullable=True)  # "Standard", "Expanded", "Unlimited", "Casual"
    target_size = Column(Integer, nullable=True)
    icon_pokemon_id = Column(Integer, nullable=True)
    is_public = Column(Boolean, default=False, nullable=False)
    auto_owned_set_id = Column(String, nullable=True)
    # Set only when importing Decks created by the original standalone PR #386
    # schema. Keeping the source id makes that data migration idempotent.
    legacy_deck_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    binder_cards = relationship("BinderCard", back_populates="binder", cascade="all, delete-orphan")

    @property
    def entries(self):
        """Planned deck entries stored in the shared binder-card table."""
        if (self.binder_type or "collection") in {"deck", "physical_deck"}:
            return [entry for entry in self.binder_cards if entry.collection_item_id is None]
        return list(self.binder_cards)

    @property
    def allocations(self):
        """Exact owned copies assigned to this card list."""
        return [entry for entry in self.binder_cards if entry.collection_item_id is not None]

    __table_args__ = (
        CheckConstraint(
            "target_size IS NULL OR target_size IN (20, 40, 60)",
            name="ck_binders_target_size",
        ),
        Index("ux_binders_legacy_deck_id", "legacy_deck_id", unique=True),
    )


class BinderCard(Base):
    __tablename__ = "binder_cards"

    id = Column(Integer, primary_key=True, autoincrement=True)
    binder_id = Column(Integer, ForeignKey("binders.id"), nullable=False)
    card_id = Column(String, ForeignKey("cards.id"), nullable=False)
    collection_item_id = Column(Integer, ForeignKey("collection.id"), nullable=True)
    required_quantity = Column(Integer, default=1, nullable=False)
    added_at = Column(DateTime, default=func.now())

    binder = relationship("Binder", back_populates="binder_cards")
    card = relationship("Card", back_populates="binder_cards")
    collection_item = relationship("CollectionItem")

    __table_args__ = (
        CheckConstraint("required_quantity >= 1 AND required_quantity <= 99", name="ck_binder_card_quantity_range"),
        UniqueConstraint("binder_id", "collection_item_id", name="uq_binder_collection_item"),
        Index("ix_binder_cards_collection_item_id", "collection_item_id"),
        Index(
            "ix_binder_cards_plan_lookup",
            "binder_id",
            "card_id",
            postgresql_where=collection_item_id.is_(None),
            sqlite_where=collection_item_id.is_(None),
        ),
    )


class ProductPurchase(Base):
    __tablename__ = "product_purchases"

    id = Column(Integer, primary_key=True, autoincrement=True)
    product_name = Column(String, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    product_type = Column(String)  # Booster, Display, ETB, Tin, Bundle, etc.
    purchase_price = Column(Float, nullable=False)
    current_value = Column(Float)
    sold_price = Column(Float)
    purchase_date = Column(Date, nullable=False)
    sold_date = Column(Date)
    lifecycle_status = Column(String, nullable=False, default="sealed", server_default="sealed")
    batch_id = Column(String)
    image_url = Column(String)
    cardmarket_url = Column(String)
    notes = Column(Text)
    created_at = Column(DateTime, default=func.now())

    __table_args__ = (
        Index("ix_product_purchases_user_id", "user_id"),
        Index("ix_product_purchases_user_batch", "user_id", "batch_id"),
        CheckConstraint(
            "lifecycle_status IN ('sealed', 'opened', 'sold', 'review')",
            name="ck_product_purchases_lifecycle_status",
        ),
    )


class ProductCard(Base):
    __tablename__ = "product_cards"

    id = Column(Integer, primary_key=True, autoincrement=True)
    product_id = Column(Integer, ForeignKey("product_purchases.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    card_id = Column(String, ForeignKey("cards.id", ondelete="SET NULL"), nullable=True)
    # Historical source row only. Intentionally not a FK so sold-card history
    # survives when the active collection row is reduced/deleted after sale.
    collection_item_id = Column(Integer, nullable=True)
    initial_quantity = Column(Integer, default=1, nullable=False)
    active_quantity = Column(Integer, default=1, nullable=False)
    sold_quantity = Column(Integer, default=0, nullable=False)
    condition = Column(String, default="NM")
    variant = Column(String, nullable=False, default="Normal")
    lang = Column(String, default="en")
    purchase_price = Column(Float)
    linked_at = Column(DateTime, default=func.now())

    product = relationship("ProductPurchase")
    card = relationship("Card")
    ledger_entries = relationship(
        "ProductLedgerEntry",
        back_populates="product_card",
        order_by="ProductLedgerEntry.event_date.asc(), ProductLedgerEntry.id.asc()",
    )
    printing_detail_tags = relationship(
        "PrintingDetailTag",
        secondary=product_card_printing_detail_tags,
        back_populates="product_cards",
        lazy="selectin",
        order_by="PrintingDetailTag.name",
    )

    @property
    def printing_details(self):
        return [tag.name for tag in self.printing_detail_tags]

    __table_args__ = (
        CheckConstraint("initial_quantity >= 1", name="ck_product_cards_initial_quantity_positive"),
        CheckConstraint("active_quantity >= 0", name="ck_product_cards_active_quantity_non_negative"),
        CheckConstraint("sold_quantity >= 0", name="ck_product_cards_sold_quantity_non_negative"),
        CheckConstraint("active_quantity + sold_quantity <= initial_quantity", name="ck_product_cards_quantities_within_initial"),
        CheckConstraint(COLLECTION_VARIANT_CHECK, name="ck_product_cards_variant"),
    )


class ProductLedgerEntry(Base):
    __tablename__ = "product_ledger_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    product_card_id = Column(Integer, ForeignKey("product_cards.id", ondelete="SET NULL"), nullable=True)
    product_id = Column(Integer, ForeignKey("product_purchases.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    entry_type = Column(String, nullable=False, default="card_sale")  # card_sale / flat_gain / adjustment
    card_id = Column(String, ForeignKey("cards.id", ondelete="SET NULL"), nullable=True)
    original_collection_item_id = Column(Integer, nullable=True)
    trade_item_id = Column(Integer, ForeignKey("trade_items.id", ondelete="SET NULL"), nullable=True)
    quantity = Column(Integer, default=1, nullable=False)
    amount = Column(Float, nullable=False)  # Flat total for this ledger event
    event_date = Column(Date, nullable=False)
    product_name = Column(String, nullable=True)
    card_name = Column(String, nullable=True)
    set_id = Column(String, nullable=True)
    card_number = Column(String, nullable=True)
    variant = Column(String, nullable=True)
    condition = Column(String, nullable=True)
    lang = Column(String, nullable=True)
    notes = Column(Text)
    created_at = Column(DateTime, default=func.now())

    product_card = relationship("ProductCard", back_populates="ledger_entries")
    product = relationship("ProductPurchase")
    card = relationship("Card")
    printing_detail_tags = relationship(
        "PrintingDetailTag",
        secondary=product_ledger_printing_detail_tags,
        back_populates="product_ledger_entries",
        lazy="selectin",
        order_by="PrintingDetailTag.name",
    )

    @property
    def printing_details(self):
        return [tag.name for tag in self.printing_detail_tags]

    __table_args__ = (
        CheckConstraint("quantity >= 1", name="ck_product_ledger_quantity_positive"),
        CheckConstraint("amount >= 0", name="ck_product_ledger_amount_non_negative"),
        CheckConstraint("entry_type IN ('card_sale', 'flat_gain', 'adjustment', 'trade_out')", name="ck_product_ledger_entry_type"),
        CheckConstraint(
            "variant IS NULL OR " + COLLECTION_VARIANT_CHECK,
            name="ck_product_ledger_variant",
        ),
    )


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    partner_name = Column(String, nullable=True)
    trade_date = Column(Date, nullable=False)
    notes = Column(Text)
    outgoing_value = Column(Float, default=0, nullable=False)
    incoming_value = Column(Float, default=0, nullable=False)
    value_delta = Column(Float, default=0, nullable=False)
    created_at = Column(DateTime, default=func.now())

    items = relationship(
        "TradeItem",
        back_populates="trade",
        cascade="all, delete-orphan",
        order_by="TradeItem.id.asc()",
    )

    __table_args__ = (
        CheckConstraint("outgoing_value >= 0", name="ck_trades_outgoing_value_non_negative"),
        CheckConstraint("incoming_value >= 0", name="ck_trades_incoming_value_non_negative"),
    )


class TradeItem(Base):
    __tablename__ = "trade_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_id = Column(Integer, ForeignKey("trades.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    direction = Column(String, nullable=False)
    card_id = Column(String, ForeignKey("cards.id", ondelete="SET NULL"), nullable=True)
    original_collection_item_id = Column(Integer, nullable=True)
    created_collection_item_id = Column(Integer, nullable=True)
    product_card_id = Column(Integer, nullable=True)
    # Inventory provenance added for editable trades. Legacy rows keep
    # snapshot_version=0 so unsafe reversals can be rejected explicitly.
    purchase_price = Column(Float, nullable=True)
    snapshot_version = Column(Integer, default=1, nullable=False)
    quantity = Column(Integer, default=1, nullable=False)
    value_per_card = Column(Float, default=0, nullable=False)
    value_total = Column(Float, default=0, nullable=False)
    card_name = Column(String, nullable=True)
    set_id = Column(String, nullable=True)
    card_number = Column(String, nullable=True)
    variant = Column(String, nullable=True)
    condition = Column(String, nullable=True)
    lang = Column(String, nullable=True)
    notes = Column(Text)
    created_at = Column(DateTime, default=func.now())

    trade = relationship("Trade", back_populates="items")
    card = relationship("Card")
    printing_detail_tags = relationship(
        "PrintingDetailTag",
        secondary=trade_item_printing_detail_tags,
        back_populates="trade_items",
        lazy="selectin",
        order_by="PrintingDetailTag.name",
    )

    @property
    def printing_details(self):
        return [tag.name for tag in self.printing_detail_tags]

    __table_args__ = (
        CheckConstraint("direction IN ('outgoing', 'incoming')", name="ck_trade_items_direction"),
        CheckConstraint("quantity >= 1", name="ck_trade_items_quantity_positive"),
        CheckConstraint("value_per_card >= 0", name="ck_trade_items_value_per_card_non_negative"),
        CheckConstraint("value_total >= 0", name="ck_trade_items_value_total_non_negative"),
        CheckConstraint("purchase_price IS NULL OR purchase_price >= 0", name="ck_trade_items_purchase_price_non_negative"),
        CheckConstraint("snapshot_version >= 0", name="ck_trade_items_snapshot_version_non_negative"),
        CheckConstraint(
            "variant IS NULL OR " + COLLECTION_VARIANT_CHECK,
            name="ck_trade_items_variant",
        ),
    )


class SyncLog(Base):
    __tablename__ = "sync_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    started_at = Column(DateTime, default=func.now())
    finished_at = Column(DateTime)
    cards_updated = Column(Integer, default=0)
    sets_updated = Column(Integer, default=0)
    status = Column(String, default="running")  # running/success/error
    error_message = Column(Text)
    sync_type = Column(String, nullable=True, default="full")  # "full" or "price"


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(DateTime, nullable=False)  # full UTC timestamp, no unique constraint
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    total_value = Column(Float, default=0)
    total_cards = Column(Integer, default=0)
    total_cost = Column(Float, default=0)
    calculation_version = Column(Integer, default=PORTFOLIO_CALCULATION_VERSION, nullable=False)
    cards_value = Column(Float, nullable=True)
    products_value = Column(Float, nullable=True)
    cards_cost = Column(Float, nullable=True)
    products_cost = Column(Float, nullable=True)
    performance_cost_basis = Column(Float, nullable=True)
    realized_value = Column(Float, nullable=True)
    unrealized_pnl = Column(Float, nullable=True)
    realized_pnl = Column(Float, nullable=True)
    total_pnl = Column(Float, nullable=True)
    product_value_fallback_count = Column(Integer, nullable=True)

    __table_args__ = (
        Index("ix_portfolio_snapshots_user_date", "user_id", "date"),
    )


class Setting(Base):
    __tablename__ = "settings"

    key = Column(String, primary_key=True)
    value = Column(Text, nullable=False)


class UserSetting(Base):
    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    key = Column(String, nullable=False)
    value = Column(Text, nullable=False)

    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_user_setting"),)


class CustomCardMatch(Base):
    """Tracks custom cards that now have an equivalent API card on TCGdex."""
    __tablename__ = "custom_card_matches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    custom_card_id = Column(String, ForeignKey("cards.id"), nullable=False)
    api_card_id = Column(String, nullable=False)
    matched_at = Column(DateTime, default=func.now())
    status = Column(String, default="pending")  # pending / migrated / dismissed

    custom_card = relationship("Card", foreign_keys=[custom_card_id])


class ImageCache(Base):
    __tablename__ = "image_cache"

    id = Column(Integer, primary_key=True, autoincrement=True)
    image_key = Column(String, unique=True, nullable=False, index=True)
    data = Column(LargeBinary, nullable=False)
    content_type = Column(String, default="image/webp")
    cached_at = Column(DateTime, default=func.now())


class ScanJob(Base):
    """A persistent background-recognition job owned by one user."""

    __tablename__ = "scan_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(String, default="pending", nullable=False, index=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), nullable=False)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)
    expires_at = Column(DateTime, nullable=False, index=True)
    error_message = Column(Text)
    # en/de hint forwarded to the external card matcher; NULL for LLM jobs.
    session_lang = Column(String, nullable=True)
    game = Column(String, nullable=False, default="pokemon", server_default=sql_text("'pokemon'"))  # game axis (E3, PicaLens #62)

    items = relationship(
        "ScanJobItem",
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="ScanJobItem.position",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'done', 'failed')",
            name="ck_scan_jobs_status",
        ),
        Index("ix_scan_jobs_user_status_created", "user_id", "status", "created_at"),
    )


class ScanJobItem(Base):
    """One sanitized photo and its eventual recognition result."""

    __tablename__ = "scan_job_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("scan_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    position = Column(Integer, nullable=False)
    image_path = Column(String, nullable=True)
    content_type = Column(String, default="image/jpeg", nullable=False)
    byte_size = Column(Integer, nullable=False)
    batch_mode = Column(Boolean, default=False, nullable=False)
    status = Column(String, default="pending", nullable=False, index=True)
    resolved = Column(Boolean, default=False, nullable=False, index=True)
    attempts = Column(Integer, default=0, nullable=False)
    transient_failures = Column(Integer, default=0, nullable=False)
    next_attempt_at = Column(DateTime, nullable=True, index=True)
    retry_reason = Column(String, nullable=True)
    lease_token = Column(String, nullable=True, index=True)
    lease_expires_at = Column(DateTime, nullable=True, index=True)
    recognized = Column(JSON)
    matches = Column(JSON)
    # External matcher debug blob (its response minus candidates); NULL otherwise.
    matcher_result = Column(JSON)
    error = Column(Text)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), nullable=False)

    job = relationship("ScanJob", back_populates="items")

    __table_args__ = (
        UniqueConstraint("job_id", "position", name="uq_scan_job_item_position"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'retrying', 'done', 'failed')",
            name="ck_scan_job_items_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_scan_job_items_attempts_non_negative"),
        CheckConstraint(
            "transient_failures >= 0",
            name="ck_scan_job_items_transient_failures_non_negative",
        ),
        Index(
            "ix_scan_job_items_dispatch",
            "status",
            "next_attempt_at",
            "lease_expires_at",
            "user_id",
        ),
    )


class ScanQueueUserState(Base):
    """Persistent round-robin cursor used to share queue work fairly."""

    __tablename__ = "scan_queue_user_state"

    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    last_dispatched_at = Column(DateTime, nullable=True, index=True)


class GeminiQuotaState(Base):
    """Cross-worker pacing state keyed by a non-reversible API-key fingerprint."""

    __tablename__ = "gemini_quota_state"

    key_fingerprint = Column(String, primary_key=True)
    tokens = Column(Float, nullable=True)
    last_refill_at = Column(DateTime, nullable=True)
    next_request_at = Column(DateTime, nullable=True, index=True)
    blocked_until = Column(DateTime, nullable=True)
    blocked_reason = Column(String, nullable=True)
    consecutive_daily_failures = Column(Integer, default=0, nullable=False)
    interactive_pending_until = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=func.now(), nullable=False)


class ScannerProviderLimitState(Base):
    """Persisted non-Gemini provider blocks without storing credentials or URLs."""

    __tablename__ = "scanner_provider_limit_state"

    scope_fingerprint = Column(String, primary_key=True)
    provider = Column(String, nullable=False)
    blocked_until = Column(DateTime, nullable=True, index=True)
    blocked_reason = Column(String, nullable=True)
    updated_at = Column(DateTime, default=func.now(), nullable=False)


# --- game axis: default scope (E3, PicaLens #62) --------------------------------------------------------------------
# Every ORM SELECT of a game-scoped model is limited to one game. The default is "pokemon", so each existing endpoint,
# job and test keeps seeing exactly the rows it saw before the `game` column existed, even once rows of another game
# are in the same tables. A session opts into another game with `session.info["game"] = "lorcana"` (see
# services/games.py `game_session`); "*" lifts the scope (maintenance only). Not covered on purpose: raw `text()`
# SQL, Core statements on `__table__`, and ORM bulk UPDATE/DELETE; those are keyed by id or written for Pokemon only.
from sqlalchemy import event as _sa_event
from sqlalchemy.orm import Session as _OrmSession, with_loader_criteria as _with_loader_criteria

DEFAULT_GAME = "pokemon"
GAME_SCOPED_MODELS = (Set, Card, CollectionItem, ScanJob)


def _game_criteria(model, game):
    # the closure variable `game` is tracked by SQLAlchemy's lambda cache as a bound parameter
    return _with_loader_criteria(model, lambda cls: cls.game == game, include_aliases=True)


@_sa_event.listens_for(_OrmSession, "do_orm_execute")
def _apply_game_scope(execute_state):
    if (
        not execute_state.is_select
        or execute_state.is_column_load
        or execute_state.is_relationship_load
    ):
        return
    game = execute_state.execution_options.get("game") or execute_state.session.info.get("game", DEFAULT_GAME)
    if game == "*":
        return
    execute_state.statement = execute_state.statement.options(
        *[_game_criteria(model, game) for model in GAME_SCOPED_MODELS]
    )
