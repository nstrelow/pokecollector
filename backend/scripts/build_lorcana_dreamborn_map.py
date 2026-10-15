"""Build `data/lorcana_dreamborn_map_v1.json` from the lorcascan catalog (sqlite, read-only).

    python scripts/build_lorcana_dreamborn_map.py --catalog /path/to/lorcascan/data/catalog_v1.sqlite \
        --out data/lorcana_dreamborn_map_v1.json

The hub does not read the sqlite at runtime; it reads this small, deterministic JSON (one print per line).
Append-only: refuses to overwrite an existing file (a new catalog makes `..._v2.json`).
Columns per print: rest_id (print_id without the `<lang>:` prefix), set_code, promo_group, number, variant,
full_name (English), ink (English, `Amber-Steel`), rarity_en, variant_kind, langs (comma separated language codes
that have this print, e.g. "en,de,fr"). Prints without a number (the Q3 token) are listed in meta, not mapped.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

LANG_ORDER = ("en", "de", "fr", "it")


def build(catalog: Path) -> dict:
    con = sqlite3.connect(f"file:{catalog}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    by_rest: dict[str, dict] = {}
    skipped = []
    for r in con.execute(
        "SELECT print_id, lang, set_code, promo_group, number, variant, full_name, ink, rarity_en, variant_kind "
        "FROM prints ORDER BY rowid"
    ):
        lang, rest = r["print_id"].split(":", 1)
        if r["number"] is None:
            skipped.append(r["print_id"])
            continue
        ident = (r["set_code"], r["promo_group"], r["number"], r["variant"], r["variant_kind"])
        e = by_rest.get(rest)
        if e is None:
            e = by_rest[rest] = {"ident": ident, "langs": set(), "en": None, "any": r}
        elif e["ident"] != ident:
            raise SystemExit(f"identity differs between languages for {rest}: {e['ident']} vs {ident}")
        e["langs"].add(lang)
        if lang == "en":
            e["en"] = r
    prints = []
    for rest in sorted(by_rest):
        e = by_rest[rest]
        r = e["en"] or e["any"]
        prints.append([
            rest, r["set_code"], r["promo_group"], r["number"], r["variant"], r["full_name"], r["ink"],
            r["rarity_en"], r["variant_kind"], ",".join(l for l in LANG_ORDER if l in e["langs"]),
        ])
    return {
        "format": "picalens-dreamborn-map",
        "version": 1,
        "columns": ["rest_id", "set_code", "promo_group", "number", "variant", "full_name", "ink", "rarity_en",
                    "variant_kind", "langs"],
        "meta": {
            "source": "lorcascan " + catalog.name,
            "catalog_snapshot_sha256": meta.get("snapshot_sha256"),
            "catalog_source": meta.get("source"),
            "catalog_snapshot_dir": meta.get("snapshot_dir"),
            "catalog_n_prints": meta.get("n_prints"),
            "licence": meta.get("licence"),
            "n_rows": len(prints),
            "skipped_without_number": skipped,
            "note": "print_id = '<lang>:' + rest_id; identity is language independent (checked while building)",
        },
        "prints": prints,
    }


def dumps(doc: dict) -> str:
    head = {k: v for k, v in doc.items() if k != "prints"}
    lines = [json.dumps(p, ensure_ascii=False, separators=(",", ":")) for p in doc["prints"]]
    head_s = json.dumps(head, ensure_ascii=False, indent=1, sort_keys=True)
    return head_s[:-2] + ',\n "prints": [\n' + ",\n".join(lines) + "\n ]\n}\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    if a.out.exists():
        print(f"refusing to overwrite {a.out} (append-only; write a new version)", file=sys.stderr)
        return 2
    doc = build(a.catalog)
    a.out.write_text(dumps(doc), encoding="utf-8")
    json.loads(a.out.read_text(encoding="utf-8"))  # sanity
    print(f"wrote {a.out}: {doc['meta']['n_rows']} prints")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
