"""Lorcana collection <-> dreamborn.ink CSV (game-scoped, pure, no DB, no network).

Format and sources: docs/DREAMBORN-CSV.md.  Long format, one row per (card, finish):

    Set Number,Card Number,Variant,Count,Name,Color,Rarity

Rules (see the standing rule "the only expensive failure is a card silently recorded wrong"):
  * a row is mapped only on an exact (set, promo group, number, letter) match in the catalog map; nothing is guessed;
  * a row that cannot be mapped (unknown card, ambiguous, bad count, unknown finish ...) is KEPT as an entry with
    ``print_id=None`` and a ``reason``, never dropped, and is written back verbatim on export;
  * unchanged entries are exported from their original cells, so import -> export keeps every row (and is
    byte-equal when the source uses minimal CSV quoting and one line terminator).

``print_id`` follows the lorcascan scheme (``en:1-207``, ``en:1-P1-1``, ``en:3-4a``; docs/CATALOG.md in lorcascan).
Dreamborn has no language column, so import takes ``lang`` (default ``en``); export folds languages together
(a ``de:`` and an ``en:`` copy of the same card become one row, counts summed).
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

GAME = "lorcana"
DEFAULT_MAP_PATH = Path(__file__).resolve().parent.parent / "data" / "lorcana_dreamborn_map_v1.json"

LONG_HEADER = ["Set Number", "Card Number", "Variant", "Count", "Name", "Color", "Rarity"]
_REQUIRED_LONG = ("set number", "card number", "variant", "count")
_REQUIRED_WIDE = ("name", "normal", "foil", "set", "card number")
# Promo groups that dreamborn lists as their own "set" (Set Number C1, Card Number 1) -- seen in the mapping of
# Brausepaul/lorcana-app-importer. Other groups are written as "<n>/<group>" under the set they belong to.
EXPORT_GROUP_AS_SET = frozenset({"C1"})

_CARD_RE = re.compile(r"^(\d{1,9})([A-Za-z]*)(?:/([A-Za-z0-9]+))?$", re.ASCII)
_SET_RE = re.compile(r"^[A-Za-z]+\d*$", re.ASCII)


def _uint(s: str) -> int | None:
    """A plain ASCII non-negative integer of at most 9 digits, else None. (`str.isdigit()` is True for '\u00b2' and
    other superscripts and `int()` then raises; a 1 MB run of digits would also raise, so both are refused here.)"""
    return int(s) if s.isascii() and s.isdigit() and len(s) <= 9 else None


class DreambornFormatError(ValueError):
    """The file is not a dreamborn collection CSV we understand (nothing is imported)."""


# --------------------------------------------------------------------------- catalog map

@dataclass(frozen=True, slots=True)
class MapRow:
    rest_id: str
    set_code: str
    promo_group: str | None
    number: int
    variant: str
    full_name: str
    ink: str
    rarity_en: str
    variant_kind: str
    langs: tuple[str, ...]


class DreambornMap:
    """Index over ``data/lorcana_dreamborn_map_v1.json`` (built by scripts/build_lorcana_dreamborn_map.py)."""

    def __init__(self, doc: dict):
        if doc.get("format") != "picalens-dreamborn-map":
            raise ValueError("not a dreamborn map file")
        self.meta = doc["meta"]
        self.rows: dict[str, MapRow] = {}
        self._by_key: dict[tuple, list[MapRow]] = {}
        self._by_group: dict[tuple, list[MapRow]] = {}
        self.set_codes: set[str] = set()
        self.groups: set[str] = set()
        for p in doc["prints"]:
            row = MapRow(*p[:9], tuple(p[9].split(",")))
            self.rows[row.rest_id] = row
            self.set_codes.add(row.set_code)
            lv = row.variant.lower()
            self._by_key.setdefault((row.set_code, row.promo_group, row.number, lv), []).append(row)
            if row.promo_group:
                self.groups.add(row.promo_group)
                self._by_group.setdefault((row.promo_group, row.number, lv), []).append(row)

    def resolve(self, set_token: str, card_token: str, name: str | None = None) -> tuple[MapRow | None, str | None]:
        """(row, None) on an exact unique match, else (None, reason)."""
        set_token, card_token = (set_token or "").strip(), (card_token or "").strip()
        if _uint(set_token) is not None:
            set_code = str(_uint(set_token))
        elif _SET_RE.match(set_token):
            set_code = set_token.upper()
        else:
            return None, f"unreadable set {set_token!r}"
        m = _CARD_RE.match(card_token)
        if not m:
            return None, f"unreadable card number {card_token!r}"
        number, letter, group = int(m.group(1)), m.group(2).lower(), (m.group(3) or "").upper() or None
        if set_code in self.set_codes:
            cands = self._by_key.get((set_code, group, number, letter), [])
        elif set_code in self.groups and group in (None, set_code):
            cands = self._by_group.get((set_code, number, letter), [])
        else:
            return None, f"unknown set {set_token!r}"
        if not cands:
            return None, f"not in catalog: set {set_token!r} card {card_token!r}"
        if len(cands) > 1 and name:
            wanted = _fold(name)
            hit = [c for c in cands if wanted in (_fold(c.full_name), _fold(c.full_name.split(" - ")[0]))]
            if len(hit) == 1:
                cands = hit
        if len(cands) > 1:
            return None, "ambiguous: " + ", ".join(sorted(c.rest_id for c in cands))
        return cands[0], None

    def row_for_print_id(self, print_id: str) -> tuple[str, MapRow]:
        lang, _, rest = print_id.partition(":")
        row = self.rows.get(rest)
        if not rest or row is None or lang not in row.langs:
            raise KeyError(f"print_id not in the dreamborn map: {print_id!r}")
        return lang, row


def _fold(s: str) -> str:
    return " ".join(s.casefold().split())


_MAP_CACHE: dict[str, DreambornMap] = {}


def load_map(path: Path | str | None = None) -> DreambornMap:
    key = str(path or DEFAULT_MAP_PATH)
    if key not in _MAP_CACHE:
        _MAP_CACHE[key] = DreambornMap(json.loads(Path(key).read_text(encoding="utf-8")))
    return _MAP_CACHE[key]


# --------------------------------------------------------------------------- entries

@dataclass(frozen=True, slots=True)
class DreambornEntry:
    """One normalized row. ``print_id is None`` means unmapped (see ``reason``); it is still exported."""

    print_id: str | None
    count: int | None
    foil: bool | None
    raw: dict
    line: int = 0
    reason: str | None = None
    cells: tuple[str, ...] | None = field(default=None, repr=False)  # original cells, exported verbatim

    @property
    def mapped(self) -> bool:
        return self.print_id is not None

    def as_dict(self) -> dict:
        return {"print_id": self.print_id, "count": self.count, "foil": self.foil, "raw": dict(self.raw),
                "line": self.line, "reason": self.reason}


def make_entry(print_id: str, count: int, foil: bool) -> DreambornEntry:
    """A hub-side row without a source line; exported with generated Name/Color/Rarity."""
    if count < 0:
        raise ValueError("count must be >= 0")
    return DreambornEntry(print_id, int(count), bool(foil), {})


@dataclass
class ParsedCsv:
    entries: list[DreambornEntry]
    header: list[str] | None          # original header cells; None when the file had none
    newline: str = "\n"
    bom: bool = False
    final_newline: bool = True
    fmt: str = "long"                  # "long" (round-trips) or "wide" (import only)

    @property
    def mapped(self) -> list[DreambornEntry]:
        return [e for e in self.entries if e.mapped]

    @property
    def unmapped(self) -> list[DreambornEntry]:
        return [e for e in self.entries if not e.mapped]

    def report(self) -> dict:
        return {
            "format": self.fmt, "rows": len(self.entries), "mapped": len(self.mapped), "unmapped": len(self.unmapped),
            "copies_mapped": sum(e.count or 0 for e in self.mapped),
            "unmapped_rows": [{"line": e.line, "reason": e.reason, "raw": e.raw} for e in self.unmapped],
        }


def totals(entries) -> dict[tuple[str, bool], int]:
    """Sum counts per (print_id, foil) over mapped entries (a file may repeat a card)."""
    out: dict[tuple[str, bool], int] = {}
    for e in entries:
        if e.mapped:
            out[(e.print_id, e.foil)] = out.get((e.print_id, e.foil), 0) + e.count
    return out


# --------------------------------------------------------------------------- parse

def parse_csv(text: str, *, lang: str = "en", dmap: DreambornMap | None = None) -> ParsedCsv:
    dmap = dmap or load_map()
    bom = text.startswith("﻿")
    if bom:
        text = text[1:]
    if not text.strip():
        raise DreambornFormatError("empty file")
    newline = "\r\n" if "\r\n" in text else "\n"
    final_newline = text.endswith("\n")
    rows = list(csv.reader(io.StringIO(text, newline="")))
    # csv.reader line numbers: use reader.line_num-like counting by physical rows; good enough for reports
    first = rows[0]
    low = [c.strip().lower() for c in first]
    header: list[str] | None
    if all(k in low for k in _REQUIRED_LONG):
        fmt, header, body, start = "long", first, rows[1:], 2
    elif all(k in low for k in _REQUIRED_WIDE) and "variant" not in low:
        fmt, header, body, start = "wide", first, rows[1:], 2
    elif len(first) >= 4 and low[2] in ("normal", "foil") and _uint(low[3]) is not None:
        fmt, header, body, start = "long", None, rows, 1       # header-less paste
    else:
        raise DreambornFormatError(
            "unrecognised header; expected 'Set Number,Card Number,Variant,Count[,Name,Color,Rarity]' "
            f"(or the wide 'Name,Normal,Foil,...,Set,Card Number'), got {first!r}")
    cols = header if header is not None else LONG_HEADER[:len(first)]
    index = {c.strip().lower(): i for i, c in enumerate(cols)}
    entries: list[DreambornEntry] = []
    for n, cells in enumerate(body, start=start):
        if not cells or all(not c.strip() for c in cells):
            continue                                            # blank line: nothing to keep
        raw = dict(zip(cols, cells))
        if len(cells) != len(cols):
            entries.append(DreambornEntry(None, None, None, raw, n, f"expected {len(cols)} columns, got {len(cells)}",
                                          tuple(cells)))
            continue
        get = lambda k: cells[index[k]].strip() if k in index else ""
        if fmt == "long":
            entries.append(_long_entry(dmap, lang, get, raw, cells, n))
        else:
            entries.extend(_wide_entries(dmap, lang, get, raw, n))
    return ParsedCsv(entries, header, newline, bom, final_newline, fmt)


def _lang_print_id(row: MapRow | None, reason: str | None, lang: str) -> tuple[str | None, str | None]:
    if row is None:
        return None, reason
    if lang not in row.langs:
        return None, f"no {lang} print of {row.rest_id} in the catalog"
    return f"{lang}:{row.rest_id}", None


def _long_entry(dmap, lang, get, raw, cells, n) -> DreambornEntry:
    cells = tuple(cells)
    variant = get("variant").lower()
    foil = {"normal": False, "foil": True}.get(variant)
    cnt = get("count")
    count = _uint(cnt)
    problem = None
    if foil is None:
        problem = f"unknown variant {get('variant')!r}"
    elif count is None:
        problem = f"bad count {cnt!r}"
    row, reason = dmap.resolve(get("set number"), get("card number"), get("name") or None)
    pid, reason = _lang_print_id(row, reason, lang)
    problem = problem or reason
    if problem:
        return DreambornEntry(None, count, foil, raw, n, problem, cells)
    return DreambornEntry(pid, count, foil, raw, n, None, cells)


def _wide_entries(dmap, lang, get, raw, n) -> list[DreambornEntry]:
    row, reason = dmap.resolve(get("set"), get("card number"), get("name") or None)
    pid, reason = _lang_print_id(row, reason, lang)
    out = []
    for key, foil in (("normal", False), ("foil", True)):
        v = get(key)
        count = _uint(v) if v else 0
        if count is None:
            out.append(DreambornEntry(None, None, foil, raw, n, f"bad {key} count {v!r}"))
        elif count > 0:
            out.append(DreambornEntry(pid, count, foil, raw, n, reason))
    if not out:                                                 # nothing owned: keep the row as a zero entry
        out.append(DreambornEntry(pid, 0, False, raw, n, reason))
    return out


# --------------------------------------------------------------------------- export

def _generated_cells(e: DreambornEntry, dmap: DreambornMap, header: list[str]) -> list[str]:
    if e.print_id is None:
        raise ValueError(f"line {e.line}: unmapped entry has no source cells to write")
    _, row = dmap.row_for_print_id(e.print_id)
    if row.promo_group in EXPORT_GROUP_AS_SET:
        set_no, card = row.promo_group, f"{row.number:03d}{row.variant}"
    else:
        set_no = f"{int(row.set_code):03d}" if row.set_code.isdigit() else row.set_code
        card = f"{row.number:03d}{row.variant}" + (f"/{row.promo_group}" if row.promo_group else "")
    values = {
        "set number": set_no, "card number": card, "variant": "foil" if e.foil else "normal", "count": str(e.count),
        "name": row.full_name, "color": row.ink.replace("-", " "), "rarity": row.rarity_en,
    }
    return [values.get(h.strip().lower(), "") for h in header]


def _fold_languages(entries):
    """Entries from the hub: merge ``de:`` / ``en:`` copies of one card+finish (dreamborn has no language)."""
    merged: dict[tuple[str, bool], DreambornEntry] = {}
    order: list = []
    for e in entries:
        if e.cells is not None or e.print_id is None:
            order.append(e)
            continue
        key = (e.print_id.partition(":")[2], e.foil)
        if key in merged:
            merged[key] = replace(merged[key], count=merged[key].count + e.count)
        else:
            merged[key] = e
            order.append(key)
    return [merged[o] if isinstance(o, tuple) else o for o in order]


def export_csv(entries, *, header: list[str] | None = None, newline: str = "\n", bom: bool = False,
               final_newline: bool = True, write_header: bool = True, dmap: DreambornMap | None = None) -> str:
    """Write dreamborn long-format CSV.

    ``entries`` is a ``ParsedCsv.entries`` list (unchanged ones are written from their original cells, unmapped ones
    verbatim) and/or ``make_entry`` rows from the hub (generated cells; languages folded, counts summed).
    ``header=None`` writes the standard header; pass ``ParsedCsv.header`` to keep the source's own.
    ``write_header=False`` omits the header row.
    """
    dmap = dmap or load_map()
    head = list(header) if header is not None else list(LONG_HEADER)
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator=newline)
    if write_header:
        w.writerow(head)
    for e in _fold_languages(list(entries)):
        w.writerow(list(e.cells) if e.cells is not None else _generated_cells(e, dmap, head))
    out = buf.getvalue()
    if not final_newline and out.endswith(newline):
        out = out[: -len(newline)]
    return ("﻿" if bom else "") + out


def export_parsed(parsed: ParsedCsv, *, dmap: DreambornMap | None = None) -> str:
    """Round trip of a parsed file: same header, line terminator, BOM and final newline as the source."""
    if parsed.fmt != "long":
        raise ValueError("only long-format files round-trip; call export_csv(parsed.entries) to convert")
    return export_csv(parsed.entries, header=parsed.header, newline=parsed.newline, bom=parsed.bom,
                      final_newline=parsed.final_newline, write_header=parsed.header is not None, dmap=dmap)
