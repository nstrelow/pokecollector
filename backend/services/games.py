"""The hub's game axis (E3, PicaLens #62): which games exist, which one a session is scoped to, what is switched on.

`models.py` limits every ORM SELECT of Set / Card / CollectionItem / ScanJob to one game (default "pokemon"). This
module is the small public surface around that: the registry, `game_session` for code that works on another game,
and the switch that keeps the Lorcana API off until the owner turns it on (G2, PicaLens #72).
"""
from __future__ import annotations

import os
from contextlib import contextmanager

from sqlalchemy.orm import Session

from models import DEFAULT_GAME

POKEMON = "pokemon"
LORCANA = "lorcana"
GAMES = (POKEMON, LORCANA)


def validate_game(game: str | None) -> str:
    game = (game or DEFAULT_GAME).strip().lower()
    if game not in GAMES:
        raise ValueError(f"game must be one of: {', '.join(GAMES)}")
    return game


def lorcana_enabled() -> bool:
    """LORCANA_ENABLED=1 turns the /api/lorcana routes on. Default off: the deployed app has no Lorcana surface."""
    return os.getenv("LORCANA_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def scope_session(db: Session, game: str) -> Session:
    """Scope an existing session to `game` (every later ORM SELECT on it sees that game's rows only)."""
    db.info["game"] = validate_game(game)
    return db


@contextmanager
def game_session(session_factory, game: str):
    """A new session scoped to `game`, closed on exit: `with game_session(SessionLocal, "lorcana") as db: ...`."""
    db = scope_session(session_factory(), game)
    try:
        yield db
    finally:
        db.close()
