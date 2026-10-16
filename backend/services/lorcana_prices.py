"""Lorcana prices in the hub (E3, PicaLens #62): import lorcascan's daily Lorcast files, read the current price.

Source: `lorcast_<YYYY-MM-DD>.json` written by lorcascan `scripts/prices_lorcast.py` (B6, PicaLens #46), format
`lorcascan-lorcast-prices` version 1 (lorcascan docs/PRICES.md): TCGplayer's market price in USD, passed through by
Lorcast, per ENGLISH print, `usd` / `usd_foil` as numbers or null (null = no price for that finish, never 0).

Storage: `game_prices` (models.GamePrice), append-only, one row per (game, print, source, finish, day). Importing the
same file twice adds nothing. The hub never calls Lorcast; a file has to arrive in LORCANA_PRICES_DIR (see
docs/DREAMBORN-CSV.md "Hub" and the E3 plan).

Reading (`price_for`): the newest day on file for the print and finish.
  * a de / fr / it print inherits its English twin's price (same rest id) and says so (`inherited: true`): TCGplayer
    has no localised products, so that is an approximation and clients must label it ("~ English price");
  * foil-only prints (the catalog's variant_kind enchanted / iconic / epic) have `usd` null; only for those a NORMAL
    lookup returns the foil price with `finish_fallback: "foil"`. Any other print with a null normal price stays null
    (a normal card with no TCGplayer normal price must not be valued at its foil price).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session

from models import GamePrice
from services.lorcana_catalog import is_foil_only_print

GAME = "lorcana"
SOURCE = "lorcast"
CURRENCY = "USD"
FORMAT = "lorcascan-lorcast-prices"
FILE_RE = re.compile(r"^lorcast_(\d{4}-\d{2}-\d{2})\.json$")
PRINT_RE = re.compile(r"^[a-z]{2}(-[a-z]{2,4})?:[^\s:]+$")


class PriceFileError(ValueError):
    pass


def latest_file(directory: Path | str) -> Path | None:
    """The newest `lorcast_<date>.json` in a directory (by the date in the name), or None."""
    files = sorted((m.group(1), p) for p in Path(directory).glob("lorcast_*.json") if (m := FILE_RE.match(p.name)))
    return files[-1][1] if files else None


def read_price_file(path: Path | str) -> tuple[dt.date, str | None, dict]:
    """(price day, fetched_at, {print_id: {"usd": x|None, "usd_foil": y|None}}) of one file; raises PriceFileError."""
    path = Path(path)
    m = FILE_RE.match(path.name)
    if not m:
        raise PriceFileError(f"{path.name}: not a lorcast_<date>.json file name")
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("format") != FORMAT or doc.get("version") != 1:
        raise PriceFileError(f"{path.name}: format {doc.get('format')!r} version {doc.get('version')!r} not supported")
    meta = doc.get("meta") or {}
    day = dt.date.fromisoformat(m.group(1))
    if meta.get("date") and meta["date"] != m.group(1):
        raise PriceFileError(f"{path.name}: meta.date {meta['date']!r} disagrees with the file name")
    prices = doc.get("prices")
    if not isinstance(prices, dict) or not prices:
        raise PriceFileError(f"{path.name}: no prices")
    out = {}
    for pid, row in prices.items():
        if not PRINT_RE.match(pid):
            raise PriceFileError(f"{path.name}: bad print id {pid!r}")
        vals = {}
        for key in ("usd", "usd_foil"):
            v = row.get(key)
            if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                                  or v < 0):
                raise PriceFileError(f"{path.name}: {pid} {key}={v!r}")
            vals[key] = None if v is None else float(v)
        out[pid] = vals
    return day, meta.get("fetched_at"), out


def import_price_file(db: Session, path: Path | str) -> dict:
    """Append one day's rows; rows that are already there are left alone. Returns counts."""
    path = Path(path)
    day, fetched_at, prices = read_price_file(path)
    have = {
        (pid, fin)
        for pid, fin in db.query(GamePrice.print_id, GamePrice.finish).filter(
            GamePrice.game == GAME, GamePrice.source == SOURCE, GamePrice.price_date == day)
    }
    new = []
    for pid, row in sorted(prices.items()):
        for fin, key in (("normal", "usd"), ("foil", "usd_foil")):
            if (pid, fin) not in have:
                new.append(GamePrice(game=GAME, print_id=pid, source=SOURCE, currency=CURRENCY, finish=fin,
                                     amount=row[key], price_date=day, fetched_at=fetched_at, source_file=path.name))
    db.add_all(new)
    db.commit()
    return {"file": path.name, "date": day.isoformat(), "prints": len(prices), "added": len(new),
            "already_there": len(prices) * 2 - len(new)}


@dataclass(frozen=True)
class PriceInfo:
    amount: float | None
    currency: str
    finish: str                 # the finish the number is for
    date: str                   # the day on file
    print_id: str               # the print the number belongs to (the English twin when inherited)
    inherited: bool
    finish_fallback: str | None = None

    def as_dict(self) -> dict:
        return {"amount": self.amount, "currency": self.currency, "finish": self.finish, "date": self.date,
                "source": SOURCE, "print_id": self.print_id, "inherited": self.inherited,
                "finish_fallback": self.finish_fallback}


def _latest(db: Session, print_id: str, finish: str) -> GamePrice | None:
    return (db.query(GamePrice)
            .filter(GamePrice.game == GAME, GamePrice.source == SOURCE, GamePrice.print_id == print_id,
                    GamePrice.finish == finish)
            .order_by(GamePrice.price_date.desc(), GamePrice.id.desc()).first())


def price_for(db: Session, print_id: str, foil: bool) -> PriceInfo | None:
    """The current price of a print and finish, or None when no file has a row for the print (or its English twin)."""
    lang, _, rest = print_id.partition(":")
    english = f"en:{rest}"
    inherited = lang != "en"
    for pid in (print_id, english) if inherited else (print_id,):
        row = _latest(db, pid, "foil" if foil else "normal")
        if row is None:
            continue
        if row.amount is None and not foil and is_foil_only_print(pid):
            alt = _latest(db, pid, "foil")
            if alt is not None and alt.amount is not None:
                return PriceInfo(alt.amount, CURRENCY, "foil", alt.price_date.isoformat(), pid, pid != print_id,
                                 finish_fallback="foil")
        return PriceInfo(row.amount, CURRENCY, row.finish, row.price_date.isoformat(), pid, pid != print_id)
    return None


def prices_status(db: Session) -> dict:
    """What the hub holds: newest day, how many days, rows."""
    q = db.query(func.max(GamePrice.price_date), func.count(func.distinct(GamePrice.price_date)), func.count(GamePrice.id)
                 ).filter(GamePrice.game == GAME, GamePrice.source == SOURCE).one()
    newest = q[0]
    if isinstance(newest, str):
        newest = dt.date.fromisoformat(newest)
    return {"newest": newest.isoformat() if newest else None, "days": q[1], "rows": q[2],
            "age_days": (dt.datetime.now(dt.timezone.utc).date() - newest).days if newest else None}
