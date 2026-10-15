"""CLI for the Lorcana dreamborn.ink collection CSV (no DB, no network).

    cd backend
    python scripts/dreamborn_csv.py check  export.csv [--lang en]        # counts + every unmapped row
    python scripts/dreamborn_csv.py roundtrip export.csv [--out out.csv] # import -> export, verifies every row kept
    python scripts/dreamborn_csv.py json export.csv [--lang en]          # normalized rows {print_id,count,foil,raw}
    python scripts/dreamborn_csv.py export rows.json [--out out.csv]     # [{print_id,count,foil}] -> dreamborn CSV

Exit code 0 = ok, 1 = unmapped rows present (check) or round trip differs, 2 = unreadable file.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.lorcana_dreamborn import (  # noqa: E402
    DreambornFormatError, export_csv, export_parsed, make_entry, parse_csv,
)


def _rows(text):
    return list(csv.reader(io.StringIO(text, newline="")))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["check", "roundtrip", "json", "export"])
    ap.add_argument("file", type=Path)
    ap.add_argument("--lang", default="en")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    text = a.file.read_text(encoding="utf-8")
    try:
        if a.cmd == "export":
            out = export_csv([make_entry(r["print_id"], int(r["count"]), bool(r["foil"])) for r in json.loads(text)])
            (a.out.write_text(out, encoding="utf-8") if a.out else sys.stdout.write(out))
            return 0
        parsed = parse_csv(text, lang=a.lang)
    except (DreambornFormatError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if a.cmd == "check":
        rep = parsed.report()
        print(f"{rep['format']} format: {rep['rows']} rows, {rep['mapped']} mapped ({rep['copies_mapped']} copies), "
              f"{rep['unmapped']} unmapped")
        for u in rep["unmapped_rows"]:
            print(f"  line {u['line']}: {u['reason']}  {list(u['raw'].values())}")
        return 1 if rep["unmapped"] else 0
    if a.cmd == "json":
        json.dump([e.as_dict() for e in parsed.entries], sys.stdout, ensure_ascii=False, indent=1)
        print()
        return 0
    out = export_parsed(parsed)
    same_rows = _rows(out) == _rows(text.lstrip("﻿"))
    print(f"round trip: {len(parsed.entries)} rows, rows equal={same_rows}, bytes equal={out == text}", file=sys.stderr)
    if a.out:
        a.out.write_text(out, encoding="utf-8")
    return 0 if same_rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
