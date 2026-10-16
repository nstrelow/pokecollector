"""Snapshot the Pokemon-facing API of one backend tree on a seeded SQLite test DB (E3, PicaLens #62).

    python scripts/api_snapshot.py --tree /path/to/backend --db /tmp/x.sqlite --out snap.json [--lorcana]

Run it once per tree (the baseline branch and game-axis) and diff the two JSON files: they must be byte-identical.
`--lorcana` (game-axis only) adds Lorcana cards, items and prices to the same tables first; the Pokemon answers must
still not change. Seeding uses fixed timestamps; the network is blocked; nothing leaves the machine.

Every parameterless GET under /api is called, plus a fixed list of GETs with ids and a few writes (add, change,
remove a Pokemon collection item) followed by GETs again. Each record is (method, path, status, body); the body is
the parsed JSON (or text), keys sorted.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import socket
import sys

ap = argparse.ArgumentParser()
ap.add_argument("--tree", required=True)
ap.add_argument("--db", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--lorcana", action="store_true")
args = ap.parse_args()

os.environ["DATABASE_URL"] = "postgresql://u:p@127.0.0.1:1/x"      # never connected; replaced by the SQLite override
os.environ.setdefault("JWT_SECRET_KEY", "snapshot-not-a-secret")
os.environ["PRE_UPGRADE_BACKUP_ENABLED"] = "false"
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.abspath(args.tree))
os.chdir(args.tree)


def _no_network(*a, **k):
    raise OSError("network blocked by api_snapshot.py")


socket.socket.connect = _no_network
socket.create_connection = _no_network

from fastapi.testclient import TestClient            # noqa: E402
from sqlalchemy import create_engine                 # noqa: E402
from sqlalchemy.orm import sessionmaker              # noqa: E402

import main                                          # noqa: E402
from api.auth import get_current_user, get_optional_user   # noqa: E402
from database import Base, get_db                    # noqa: E402
import models as M                                   # noqa: E402

if os.path.exists(args.db):
    os.remove(args.db)
engine = create_engine(f"sqlite:///{args.db}", connect_args={"check_same_thread": False})
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
T0 = dt.datetime(2026, 1, 2, 3, 4, 5)


def seed():
    db = Session()
    admin = M.User(id=1, username="admin", hashed_password="x", role="admin", is_active=True, created_at=T0)
    friend = M.User(id=2, username="misty", hashed_password="x", role="trainer", is_active=True, created_at=T0)
    db.add_all([admin, friend])
    db.add_all([M.Set(id=f"sv{n}_en", tcg_set_id=f"sv{n}", name=f"Set {n}", series="Scarlet & Violet", total=10 + n,
                      printed_total=10 + n, lang="en", release_date=f"2023-0{n}-01", updated_at=T0) for n in (1, 2)])
    db.add_all([M.Set(id="sv1_de", tcg_set_id="sv1", name="Satz 1", series="Karmesin & Purpur", total=11,
                      printed_total=11, lang="de", release_date="2023-01-01", updated_at=T0)])
    cards = []
    for n, (sid, lang) in enumerate([("sv1", "en"), ("sv1", "en"), ("sv2", "en"), ("sv2", "en"), ("sv1", "de")], 1):
        cards.append(M.Card(
            id=f"{sid}-{n}_{lang}", tcg_card_id=f"{sid}-{n}", name=f"Card {n}", set_id=sid, number=str(n),
            rarity=["Common", "Rare", "Uncommon", "Rare Holo", "Common"][n - 1], lang=lang, supertype="Pokemon",
            types=["Grass"], hp="60", artist="Ken Sugimori", price_market=0.5 * n, price_trend=0.6 * n,
            price_avg1=0.4 * n, price_avg7=0.45 * n, price_avg30=0.5 * n, price_low=0.1 * n,
            price_market_holo=1.5 * n, price_trend_holo=1.6 * n, price_tcg_normal_market=0.7 * n,
            price_tcg_holo_market=2.0 * n, images_small=f"https://assets.example/{n}/low.webp",
            images_large=f"https://assets.example/{n}/high.webp", variants_normal=True, variants_holo=n % 2 == 0,
            variants_reverse=True, updated_at=T0))
    db.add_all(cards)
    db.flush()
    db.add_all([
        M.CollectionItem(card_id="sv1-1_en", user_id=1, quantity=2, condition="NM", variant="Normal", lang="en", added_at=T0),
        M.CollectionItem(card_id="sv1-2_en", user_id=1, quantity=1, condition="LP", variant="Holo", lang="en",
                         purchase_price=3.5, added_at=T0 + dt.timedelta(days=1)),
        M.CollectionItem(card_id="sv2-3_en", user_id=1, quantity=4, condition="NM", variant="Reverse Holo", lang="en",
                         added_at=T0 + dt.timedelta(days=2)),
        M.CollectionItem(card_id="sv1-5_de", user_id=1, quantity=1, condition="NM", variant="Normal", lang="de",
                         added_at=T0 + dt.timedelta(days=3)),
        M.CollectionItem(card_id="sv2-4_en", user_id=2, quantity=1, condition="NM", variant="Normal", lang="en", added_at=T0),
    ])
    db.add_all([M.WishlistItem(card_id="sv2-4_en", user_id=1, quantity=1, created_at=T0),
                M.PriceHistory(card_id="sv1-1_en", date=dt.date(2026, 1, 1), price_market=0.4, price_trend=0.5),
                M.PriceHistory(card_id="sv1-1_en", date=dt.date(2026, 1, 2), price_market=0.5, price_trend=0.6)])
    b = M.Binder(id=1, name="Main", user_id=1, color="#ee1515", binder_type="collection", created_at=T0)
    db.add(b)
    db.flush()
    db.add(M.BinderCard(binder_id=1, card_id="sv1-1_en", added_at=T0))
    db.add(M.Setting(key="language", value="en")) if hasattr(M, "Setting") else None
    db.commit()
    if args.lorcana:
        import services.lorcana_prices as P
        import tempfile, pathlib
        lc = Session()
        lc.info["game"] = "lorcana"
        from api.lorcana import _add
        _add(lc, 1, "en:1-1", 4, None, "NM", None)
        _add(lc, 1, "en:1-207", 1, None, "NM", None)
        _add(lc, 2, "de:1-1", 2, True, "NM", None)
        lc.add(M.ScanJob(user_id=1, status="done", created_at=T0, updated_at=T0, expires_at=T0, game="lorcana"))
        lc.commit()
        d = pathlib.Path(tempfile.mkdtemp())
        (d / "lorcast_2026-10-16.json").write_text(json.dumps({
            "format": "lorcascan-lorcast-prices", "version": 1, "meta": {"date": "2026-10-16"},
            "prices": {"en:1-1": {"usd": 0.13, "usd_foil": 0.75}, "en:1-207": {"usd": None, "usd_foil": 952.84}}}))
        P.import_price_file(lc, d / "lorcast_2026-10-16.json")
        lc.close()
    db.close()


seed()


def override_db():
    db = Session()
    try:
        yield db
    finally:
        db.close()


def user():
    db = Session()
    try:
        u = db.query(M.User).filter(M.User.id == 1).one()
        db.expunge(u)
        return u
    finally:
        db.close()


app = main.app
app.dependency_overrides[get_db] = override_db
app.dependency_overrides[get_current_user] = user
app.dependency_overrides[get_optional_user] = user
client = TestClient(app, raise_server_exceptions=False)

SKIP = ("/api/lorcana",)                       # the new surface is not part of the comparison
gets = sorted({r.path for r in app.routes if getattr(r, "methods", None) and "GET" in r.methods
               and r.path.startswith("/api") and "{" not in r.path and not r.path.startswith(SKIP)})
extra = [
    "/api/cards/sv1-1_en", "/api/cards/sv1-5_de", "/api/cards/search?q=Card", "/api/cards/search?q=Card&lang=de",
    "/api/cards/search?rarity=Rare", "/api/sets/sv1", "/api/sets/sv1_en", "/api/sets/sv1/cards", "/api/sets/sv2/cards",
    "/api/collection/?sort_by=quantity&order=asc", "/api/collection/user/2", "/api/collection/1/photo",
    "/api/binders/1", "/api/pokedex/?status=all", "/api/images/cards/sv1-1_en", "/api/wishlist/",
    "/api/sets/?lang=en", "/api/sets/sv1_en/cards", "/api/cards/search?set_id=sv1", "/api/export/csv?lang=en",
]
records = []


def call(method, path, **kw):
    r = client.request(method, path, **kw)
    try:
        body = r.json()
    except ValueError:
        body = r.text if "text" in r.headers.get("content-type", "") or r.headers.get("content-type", "").startswith("application/json") else f"<{len(r.content)} bytes {r.headers.get('content-type')}>"
    records.append({"method": method, "path": path, "status": r.status_code, "body": body})


for p in gets + extra:
    call("GET", p)
call("POST", "/api/collection/", json={"card_id": "sv2-3_en", "quantity": 2, "variant": "Normal"})
call("POST", "/api/collection/", json={"card_id": "sv1-1_en", "quantity": 1})
call("POST", "/api/collection/bulk-add", json={"items": [{"card_id": "sv1-2_en", "quantity": 1, "variant": "Holo"}]})
call("PUT", "/api/collection/1", json={"quantity": 5})
call("DELETE", "/api/collection/3")
call("GET", "/api/collection/")
call("GET", "/api/collection/stats/summary")
text = json.dumps(records, indent=1, sort_keys=True, ensure_ascii=False, default=str)
# "now" stamps written by the code under test (added_at, updated_at, ...) differ per run: mask today's date-times only
text = re.sub(r"%s[T ]\d\d:\d\d:\d\d(\.\d+)?" % dt.date.today().isoformat(), "<now>", text)
with open(args.out, "w", encoding="utf-8") as f:
    f.write(text)
ok = sum(1 for r in records if r["status"] < 400)
print(f"{len(records)} calls, {ok} below 400, written to {args.out}")
