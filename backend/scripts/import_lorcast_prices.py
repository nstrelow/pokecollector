"""Import lorcascan's daily Lorcast price files into the hub's `game_prices` table (E3, PicaLens #62).

    docker exec pokemon-backend python -m scripts.import_lorcast_prices                  # newest file in LORCANA_PRICES_DIR
    docker exec pokemon-backend python -m scripts.import_lorcast_prices FILE [FILE ...]  # these files, in order
    docker exec pokemon-backend python -m scripts.import_lorcast_prices --all            # every file in the directory

LORCANA_PRICES_DIR defaults to /app/data/lorcana-prices (docker-compose.yml mounts ./data/lorcana-prices there).
Append-only and idempotent: importing a day twice adds nothing, old rows are never touched. Exit 0 = ok, 1 = a file
was refused (nothing of that file is written), 2 = nothing to import.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from database import SessionLocal
from services.games import LORCANA, game_session
from services.lorcana_prices import FILE_RE, PriceFileError, import_price_file, latest_file

DEFAULT_DIR = "/app/data/lorcana-prices"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", type=Path)
    ap.add_argument("--dir", type=Path, default=Path(os.getenv("LORCANA_PRICES_DIR", DEFAULT_DIR)))
    ap.add_argument("--all", action="store_true", help="import every lorcast_<date>.json in --dir, oldest first")
    a = ap.parse_args(argv)
    if a.files:
        files = a.files
    elif a.all:
        files = sorted((p for p in a.dir.glob("lorcast_*.json") if FILE_RE.match(p.name)), key=lambda p: p.name)
    else:
        newest = latest_file(a.dir) if a.dir.is_dir() else None
        files = [newest] if newest else []
    if not files:
        print(f"nothing to import in {a.dir}", file=sys.stderr)
        return 2
    rc = 0
    with game_session(SessionLocal, LORCANA) as db:
        for f in files:
            try:
                print(import_price_file(db, f))
            except (PriceFileError, OSError, ValueError) as exc:
                db.rollback()
                print(f"refused {f}: {exc}", file=sys.stderr)
                rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
