"""`/api/lorcana/*`: the hub's Lorcana collection (E3, PicaLens #62). Server side only, off by default.

Mounted by main.py only when `LORCANA_ENABLED=1` (services/games.py); the deployed app answers 404 for all of it
until G2 (PicaLens #72) turns it on. Every route works on a session scoped to the Lorcana game (models.py default
scope), so a Pokemon row can never appear here and a Lorcana row can never appear in a Pokemon endpoint.

  GET    /status                       what is switched on, price file age
  GET    /collection/                  the user's Lorcana items, each with its current price
  POST   /collection/                  add {print_id, quantity, foil, condition, purchase_price}
  PUT    /collection/{id}              change quantity / foil / condition / purchase_price
  DELETE /collection/{id}
  POST   /collection/import-dreamborn  dreamborn.ink CSV; preview unless commit=true; unmapped rows come back, never guessed
  GET    /collection/export-dreamborn  the collection as a dreamborn.ink CSV
  GET    /prices/{print_id}?foil=      the current price of one print

Foil is the user's choice at save time, never a scanner decision (plan section 0). It is stored as the existing
`collection.variant` value "Foil" or "Normal" (owner decision 2026-10-16; Pokemon keeps its four variants and never
accepts "Foil"); the Pokemon pricing is untouched.
"""
from __future__ import annotations

import csv
import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.auth import get_current_user
from database import SessionLocal
from models import Card, CollectionItem, User
from services import lorcana_dreamborn as dream
from services.games import LORCANA, lorcana_enabled, scope_session
from services.lorcana_catalog import (UnknownPrint, card_id_for, ensure_lorcana_card, is_foil_kind,
                                      print_id_of)
from services.lorcana_prices import CURRENCY, price_for, prices_status

FOIL, NORMAL = "Foil", "Normal"
CONDITIONS = ("Mint", "NM", "LP", "MP", "HP")
LANGS = ("en", "de", "fr", "it")
IMPORT_MAX_BYTES = 1024 * 1024
IMPORT_ENVELOPE_BYTES = 64 * 1024          # multipart framing allowed on top of the file itself
MAX_QUANTITY = 999                         # the same cap as LorcanaAdd.quantity
PRINT_PATTERN = r"^[a-z]{2}:[^\s:]+$"


class _ImportRoute(APIRoute):
    """Refuses an oversized upload on its Content-Length header before FastAPI reads (spools) the body. A client that
    sends no Content-Length (chunked) is still stopped by the read limit in the handler."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def guarded(request: Request):
            length = request.headers.get("content-length", "")
            if length.isascii() and length.isdigit() and int(length) > IMPORT_MAX_BYTES + IMPORT_ENVELOPE_BYTES:
                raise HTTPException(status_code=413, detail="CSV file is too large")
            return await handler(request)

        return guarded


router = APIRouter()
import_router = APIRouter(route_class=_ImportRoute)


def get_lorcana_db():
    """A DB session scoped to the Lorcana game (the default scope everywhere else is Pokemon)."""
    db = scope_session(SessionLocal(), LORCANA)
    try:
        yield db
    finally:
        db.close()


def require_lorcana():
    if not lorcana_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


class LorcanaAdd(BaseModel):
    print_id: str = Field(pattern=PRINT_PATTERN, max_length=80)
    quantity: int = Field(default=1, ge=1, le=MAX_QUANTITY)
    foil: Optional[bool] = None            # None: foil for foil-only cards (Enchanted, Epic, Iconic), else normal
    condition: str = "NM"
    purchase_price: Optional[float] = Field(default=None, ge=0)


class LorcanaUpdate(BaseModel):
    quantity: Optional[int] = Field(default=None, ge=1, le=MAX_QUANTITY)
    foil: Optional[bool] = None
    condition: Optional[str] = None
    purchase_price: Optional[float] = Field(default=None, ge=0)


def _condition(value: str) -> str:
    if value not in CONDITIONS:
        raise HTTPException(status_code=422, detail=f"condition must be one of: {', '.join(CONDITIONS)}")
    return value


def _resolve(print_id: str):
    try:
        return card_id_for(print_id)
    except UnknownPrint as exc:
        raise HTTPException(status_code=422, detail=f"unknown Lorcana print: {print_id}") from exc


def _is_foil_only(row) -> bool:
    return is_foil_kind(row.variant_kind)


def _group_query(db: Session, user_id: int, card_id: str, variant: str, condition: str, purchase_price):
    return db.query(CollectionItem).filter(
        CollectionItem.user_id == user_id, CollectionItem.card_id == card_id, CollectionItem.variant == variant,
        CollectionItem.condition == condition, CollectionItem.purchase_price == purchase_price)


def _add(db: Session, user_id: int, print_id: str, quantity: int, foil: Optional[bool], condition: str,
         purchase_price) -> tuple[str, CollectionItem, dict]:
    card_id, lang, row = _resolve(print_id)
    foil_only = _is_foil_only(row)
    if foil is None:
        foil = foil_only
    notes = {"foil_only_card": True} if foil_only and not foil else {}
    ensure_lorcana_card(db, print_id)
    variant = FOIL if foil else NORMAL
    existing = _group_query(db, user_id, card_id, variant, condition, purchase_price).first()
    if existing:
        existing.quantity = (existing.quantity or 0) + quantity
        return "updated", existing, notes
    item = CollectionItem(card_id=card_id, user_id=user_id, quantity=quantity, condition=condition, variant=variant,
                          purchase_price=purchase_price, lang=lang, game=LORCANA,
                          added_at=datetime.datetime.utcnow())
    db.add(item)
    db.flush()
    return "added", item, notes


def _item_dict(db: Session, item: CollectionItem, prices: bool = True) -> dict:
    card = item.card
    print_id = print_id_of(item.card_id)           # None when the card row is gone (card_id was set NULL)
    foil = item.variant == FOIL
    price = price_for(db, print_id, foil) if prices and print_id else None
    return {
        "id": item.id, "print_id": print_id, "card_id": item.card_id, "name": card.name if card else None,
        "set_code": card.set_id if card else None, "number": card.number if card else None,
        "rarity": card.rarity if card else None, "ink": card.types if card else None, "lang": item.lang,
        "foil": foil, "quantity": item.quantity, "condition": item.condition,
        "purchase_price": item.purchase_price,
        "added_at": item.added_at.isoformat() if item.added_at else None,
        "price": price.as_dict() if price else None,
        "currency": CURRENCY,                      # prices are always USD, also when there is none yet
    }


@router.get("/status")
def status(db: Session = Depends(get_lorcana_db), current_user: User = Depends(get_current_user),
           _on: None = Depends(require_lorcana)):
    dmap = dream.load_map()
    return {"game": LORCANA, "enabled": True, "catalog": {"prints": dmap.meta.get("n_rows"),
                                                          "snapshot": dmap.meta.get("catalog_snapshot_dir")},
            "prices": prices_status(db), "finish_storage": {"foil": FOIL, "normal": NORMAL}}


@router.get("/collection/")
def list_collection(db: Session = Depends(get_lorcana_db), current_user: User = Depends(get_current_user),
                    _on: None = Depends(require_lorcana)):
    items = db.query(CollectionItem).filter(CollectionItem.user_id == current_user.id).order_by(
        CollectionItem.added_at.desc(), CollectionItem.id.desc()).all()
    return [_item_dict(db, i) for i in items]


@router.post("/collection/")
def add_to_collection(body: LorcanaAdd, db: Session = Depends(get_lorcana_db),
                      current_user: User = Depends(get_current_user), _on: None = Depends(require_lorcana)):
    action, item, notes = _add(db, current_user.id, body.print_id, body.quantity, body.foil,
                               _condition(body.condition), body.purchase_price)
    db.commit()
    db.refresh(item)
    return {"action": action, "notes": notes, "item": _item_dict(db, item)}


@router.put("/collection/{item_id}")
def update_item(item_id: int, body: LorcanaUpdate, db: Session = Depends(get_lorcana_db),
                current_user: User = Depends(get_current_user), _on: None = Depends(require_lorcana)):
    item = db.query(CollectionItem).filter(CollectionItem.id == item_id,
                                           CollectionItem.user_id == current_user.id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="Collection item not found")
    fields = body.model_fields_set
    variant = (FOIL if body.foil else NORMAL) if "foil" in fields and body.foil is not None else item.variant
    condition = _condition(body.condition) if body.condition is not None else item.condition
    price = body.purchase_price if "purchase_price" in fields else item.purchase_price
    quantity = body.quantity if body.quantity is not None else item.quantity
    other = _group_query(db, current_user.id, item.card_id, variant, condition, price).filter(
        CollectionItem.id != item.id).first()
    if other is not None:            # the change makes this row identical to another one: merge, never duplicate
        other.quantity = (other.quantity or 0) + quantity
        db.delete(item)
        item = other
    else:
        item.variant, item.condition, item.purchase_price, item.quantity = variant, condition, price, quantity
    db.commit()
    db.refresh(item)
    return _item_dict(db, item)


@router.delete("/collection/{item_id}")
def delete_item(item_id: int, db: Session = Depends(get_lorcana_db),
                current_user: User = Depends(get_current_user), _on: None = Depends(require_lorcana)):
    item = db.query(CollectionItem).filter(CollectionItem.id == item_id,
                                           CollectionItem.user_id == current_user.id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="Collection item not found")
    db.delete(item)
    db.commit()
    return {"ok": True}


def _owned_by_finish(db: Session, user_id: int) -> dict[tuple[str, bool], int]:
    owned: dict[tuple[str, bool], int] = {}
    for i in db.query(CollectionItem).filter(CollectionItem.user_id == user_id):
        pid = print_id_of(i.card_id)
        if pid is None:                            # card row deleted: nothing to key it by, left out of the export
            continue
        key = (pid, i.variant == FOIL)
        owned[key] = owned.get(key, 0) + (i.quantity or 0)
    return owned


@import_router.post("/collection/import-dreamborn")
async def import_dreamborn(
    file: UploadFile = File(...),
    lang: str = Query("en"),
    commit: bool = Query(False, description="false = preview only, nothing is written"),
    mode: str = Query("fill", pattern="^(fill|add)$",
                      description="fill: raise each print+finish up to the file's count, never lower; add: add the file's counts"),
    db: Session = Depends(get_lorcana_db), current_user: User = Depends(get_current_user),
    _on: None = Depends(require_lorcana),
):
    if lang not in LANGS:
        raise HTTPException(status_code=422, detail=f"lang must be one of: {', '.join(LANGS)}")
    raw = await file.read(IMPORT_MAX_BYTES + 1)
    if len(raw) > IMPORT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="CSV file is too large")
    try:
        parsed = dream.parse_csv(raw.decode("utf-8-sig"), lang=lang)
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="CSV file must be UTF-8 encoded") from exc
    except dream.DreambornFormatError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (csv.Error, ValueError) as exc:         # e.g. a field larger than the csv module's limit
        raise HTTPException(status_code=422, detail=f"CSV file could not be read: {exc}") from exc

    owned = _owned_by_finish(db, current_user.id)
    plan, skipped_zero = [], 0
    for (pid, foil), want in sorted(dream.totals(parsed.entries).items()):
        if want == 0:
            skipped_zero += 1
            continue
        have = owned.get((pid, foil), 0)
        add = want if mode == "add" else max(want - have, 0)
        entry = {"print_id": pid, "foil": foil, "file_count": want, "have": have, "add": add,
                 "note": "file count is lower than the collection; left as is" if mode == "fill" and want < have
                 else None}
        if not foil and is_foil_kind(card_id_for(pid)[2].variant_kind):
            # kept as the file says (normal), never silently turned into foil; the client can show this
            entry["foil_only_card"] = True
        if want > MAX_QUANTITY or add > MAX_QUANTITY:
            raise HTTPException(status_code=422, detail=f"{pid}: count {max(want, add)} is above {MAX_QUANTITY}")
        plan.append(entry)
    result = {"mode": mode, "committed": False, "report": parsed.report(), "plan": plan, "skipped_zero": skipped_zero,
              "would_add_copies": sum(p["add"] for p in plan),
              "foil_only_as_normal": sum(1 for p in plan if p.get("foil_only_card"))}
    if commit:
        for p in plan:
            if p["add"]:
                _add(db, current_user.id, p["print_id"], p["add"], p["foil"], "NM", None)
        db.commit()
        result["committed"] = True
    return result


@router.get("/collection/export-dreamborn")
def export_dreamborn(db: Session = Depends(get_lorcana_db), current_user: User = Depends(get_current_user),
                     _on: None = Depends(require_lorcana)):
    entries = [dream.make_entry(pid, n, foil) for (pid, foil), n in sorted(_owned_by_finish(db, current_user.id).items())
               if n > 0]
    return Response(dream.export_csv(entries), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="lorcana-dreamborn.csv"'})


@router.get("/prices/{print_id}")
def get_price(print_id: str, foil: bool = Query(False), db: Session = Depends(get_lorcana_db),
              current_user: User = Depends(get_current_user), _on: None = Depends(require_lorcana)):
    _resolve(print_id)
    price = price_for(db, print_id, foil)
    return {"print_id": print_id, "foil": foil, "currency": CURRENCY, "price": price.as_dict() if price else None}


router.include_router(import_router)
