import csv
import io
import json
import unittest
from dataclasses import replace
from pathlib import Path

from services.lorcana_dreamborn import (
    DEFAULT_MAP_PATH,
    DreambornFormatError,
    export_csv,
    export_parsed,
    load_map,
    make_entry,
    parse_csv,
    totals,
)

FIXTURE = Path(__file__).parent / "fixtures" / "dreamborn" / "sample_export.csv"


def rows_of(text):
    return list(csv.reader(io.StringIO(text, newline="")))


class SampleRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.text = FIXTURE.read_text(encoding="utf-8")
        self.parsed = parse_csv(self.text)

    def test_fixture_has_header_plus_22_rows(self):
        self.assertEqual(len(rows_of(self.text)), 23)
        self.assertEqual(len(self.parsed.entries), 22)

    def test_round_trip_is_byte_equal(self):
        self.assertEqual(export_parsed(self.parsed), self.text)

    def test_round_trip_keeps_every_row_in_order(self):
        self.assertEqual(rows_of(export_parsed(self.parsed)), rows_of(self.text))

    def test_second_round_trip_is_stable(self):
        again = parse_csv(export_parsed(self.parsed))
        self.assertEqual(export_parsed(again), self.text)
        self.assertEqual([e.print_id for e in again.entries], [e.print_id for e in self.parsed.entries])

    def test_mapped_print_ids_counts_and_finish(self):
        got = {(e.line, e.print_id, e.count, e.foil) for e in self.parsed.mapped}
        expected = {
            (2, "en:1-1", 3, False), (3, "en:1-1", 1, True), (4, "en:1-207", 1, True),
            (5, "en:2-205", 1, False), (6, "en:3-4a", 2, False), (7, "en:3-4e", 1, True),
            (8, "en:4-43", 4, False), (9, "en:5-120", 10, False), (10, "en:9-0", 1, False),
            (11, "en:10-129", 2, True), (12, "en:10-205", 1, False), (13, "en:10-241", 1, True),
            (14, "en:13-100", 2, False), (15, "en:1-P1-1", 1, False), (16, "en:1-C1-1", 1, True),
            (17, "en:13-P4-9", 1, False), (18, "en:7-P2-24A", 1, True), (19, "en:7-P2-26~1433", 1, False),
            (21, "en:Q1-1", 1, False),
        }
        self.assertEqual(got, expected)

    def test_unmapped_rows_are_kept_and_reported_not_guessed(self):
        reasons = {e.line: e.reason for e in self.parsed.unmapped}
        self.assertEqual(sorted(reasons), [20, 22, 23])
        self.assertTrue(reasons[20].startswith("ambiguous: 7-P2-26~1433, 7-P2-26~1663"))
        self.assertIn("unknown set", reasons[22])
        self.assertIn("unknown variant", reasons[23])
        for e in self.parsed.unmapped:
            self.assertIsNone(e.print_id)
        report = self.parsed.report()
        self.assertEqual((report["rows"], report["mapped"], report["unmapped"]), (22, 19, 3))
        self.assertEqual(report["copies_mapped"], 3 + 1 + 1 + 1 + 2 + 1 + 4 + 10 + 1 + 2 + 1 + 1 + 2 + 1 + 1 + 1 + 1 + 1 + 1)

    def test_unmapped_row_is_written_back_verbatim(self):
        out = export_parsed(self.parsed)
        self.assertIn('099,001,normal,2,"Unknown, Card",Amber,Rare\n', out)
        self.assertIn("001,001,rainbow foil,1,Ariel - On Human Legs,Amber,Uncommon\n", out)

    def test_raw_keeps_the_source_columns(self):
        e = self.parsed.entries[6]
        self.assertEqual(e.raw["Name"], 'Flotsam - Ursula\'s "Baby"')
        self.assertEqual(e.raw["Set Number"], "004")

    def test_name_disambiguates_the_two_26_p2_cards_only_when_it_matches(self):
        e = self.parsed.entries[17]
        self.assertEqual(e.print_id, "en:7-P2-26~1433")
        vaiana = parse_csv("Set Number,Card Number,Variant,Count,Name\n007,026/P2,foil,1,Vaiana - Adventurer of Land and Sea\n")
        self.assertEqual(vaiana.entries[0].print_id, "en:7-P2-26~1663")

    def test_totals_sum_repeated_rows(self):
        text = "Set Number,Card Number,Variant,Count\n001,001,normal,1\n001,001,normal,2\n001,001,foil,4\n"
        self.assertEqual(totals(parse_csv(text).entries), {("en:1-1", False): 3, ("en:1-1", True): 4})


class GeneratedExportTests(unittest.TestCase):
    def test_hub_rows_export_in_the_documented_layout(self):
        text = export_csv([
            make_entry("en:1-207", 1, True), make_entry("en:3-4a", 2, False), make_entry("en:1-P1-1", 1, False),
            make_entry("en:1-C1-1", 1, True), make_entry("en:7-P2-24A", 1, True), make_entry("en:9-0", 1, False),
        ])
        self.assertEqual(text.splitlines(), [
            "Set Number,Card Number,Variant,Count,Name,Color,Rarity",
            "001,207,foil,1,Elsa - Spirit of Winter,Amethyst,Enchanted",
            "003,004a,normal,2,Dalmatian Puppy - Tail Wagger,Amber,Common",
            "001,001/P1,normal,1,Mickey Mouse - Brave Little Tailor,Ruby,Special",
            "C1,001,foil,1,Dragon Fire,Ruby,Special",
            "007,024A/P2,foil,1,Hiro Hamada - Armor Designer,Emerald Sapphire,Special",
            "009,000,normal,1,Bruno Madrigal - Undetected Uncle,Amethyst,Super Rare",
        ])

    def test_generated_export_parses_back_to_the_same_print_ids(self):
        items = [make_entry("en:1-207", 1, True), make_entry("en:1-C1-1", 2, False), make_entry("en:13-P4-9", 3, True),
                 make_entry("en:7-P2-26~1663", 1, False), make_entry("en:Q1-1", 1, False)]
        back = parse_csv(export_csv(items))
        self.assertEqual([(e.print_id, e.count, e.foil) for e in back.entries],
                         [(e.print_id, e.count, e.foil) for e in items])

    def test_languages_fold_and_counts_sum(self):
        text = export_csv([make_entry("en:1-1", 2, False), make_entry("de:1-1", 1, False), make_entry("en:1-1", 1, True)])
        back = parse_csv(text)
        self.assertEqual([(e.print_id, e.count, e.foil) for e in back.entries], [("en:1-1", 3, False), ("en:1-1", 1, True)])

    def test_unknown_print_id_is_refused_not_guessed(self):
        with self.assertRaises(KeyError):
            export_csv([make_entry("en:99-1", 1, False)])
        with self.assertRaises(KeyError):
            export_csv([make_entry("de:1-P1-1", 1, False)])

    def test_changed_entry_is_regenerated_unchanged_ones_stay_verbatim(self):
        text = "Set Number,Card Number,Variant,Count,Name\n1,1,normal,3,Ariel in my own words\n1,2,normal,1,x\n"
        parsed = parse_csv(text)
        parsed.entries[1] = replace(parsed.entries[1], count=5, cells=None)
        out = export_parsed(parsed)
        self.assertIn("1,1,normal,3,Ariel in my own words\n", out)
        self.assertEqual(rows_of(out)[2][:4], ["001", "002", "normal", "5"])


class FormatEdgeTests(unittest.TestCase):
    def test_crlf_bom_and_missing_final_newline_round_trip(self):
        text = "﻿Set Number,Card Number,Variant,Count,Name,Color,Rarity\r\n001,001,normal,1,A,Amber,Uncommon\r\n001,002,foil,2,B,Amber,Common"
        p = parse_csv(text)
        self.assertEqual(export_parsed(p), text)

    def test_other_languages_use_the_same_numbers(self):
        p = parse_csv("Set Number,Card Number,Variant,Count\n003,004a,normal,1\n001,001/P1,normal,1\n", lang="de")
        self.assertEqual(p.entries[0].print_id, "de:3-4a")
        self.assertIsNone(p.entries[1].print_id)          # the P1 promo has no German print
        self.assertIn("no de print", p.entries[1].reason)

    def test_headerless_paste_keeps_the_first_row(self):
        text = "001,001,normal,3,Ariel - On Human Legs,Amber,Uncommon\n001,001,foil,1,Ariel - On Human Legs,Amber,Uncommon\n"
        p = parse_csv(text)
        self.assertEqual(len(p.entries), 2)
        self.assertEqual(export_parsed(p), text)

    def test_bad_rows_are_kept_with_a_reason(self):
        p = parse_csv("Set Number,Card Number,Variant,Count\n001,001,normal,x\n001,001,normal\n001,0x1,normal,1\n001,999,normal,1\n")
        self.assertEqual([e.print_id for e in p.entries], [None] * 4)
        self.assertIn("bad count", p.entries[0].reason)
        self.assertIn("columns", p.entries[1].reason)
        self.assertIn("unreadable card number", p.entries[2].reason)
        self.assertIn("not in catalog", p.entries[3].reason)

    def test_unknown_header_is_refused(self):
        with self.assertRaises(DreambornFormatError):
            parse_csv("Name,Qty\nAriel,1\n")
        with self.assertRaises(DreambornFormatError):
            parse_csv("")

    def test_wide_format_imports_but_does_not_round_trip(self):
        text = "Name,Normal,Foil,Color,Rarity,Set,Card Number\nAriel - On Human Legs,3,1,Amber,UC,001,001\nElsa,0,0,Amethyst,EE,001,207\n"
        p = parse_csv(text)
        self.assertEqual(p.fmt, "wide")
        self.assertEqual([(e.print_id, e.count, e.foil) for e in p.entries],
                         [("en:1-1", 3, False), ("en:1-1", 1, True), ("en:1-207", 0, False)])
        with self.assertRaises(ValueError):
            export_parsed(p)
        self.assertEqual(len(rows_of(export_csv(p.entries))), 4)


class MapTests(unittest.TestCase):
    def test_map_file_is_the_documented_catalog_snapshot(self):
        doc = json.loads(DEFAULT_MAP_PATH.read_text(encoding="utf-8"))
        self.assertEqual(doc["meta"]["catalog_snapshot_sha256"],
                         "f9f079922934343be5e98c39d1e16c91a33029400c1e462dbd1b8349af382da1")
        self.assertEqual(len(doc["prints"]), doc["meta"]["n_rows"])

    def test_every_mapped_print_resolves_back_from_its_generated_cells(self):
        """All 3555 prints: generate cells -> parse -> same print_id (no print is unreachable or ambiguous)."""
        dmap = load_map()
        items = [make_entry("en:" + r.rest_id, 1, False) for r in dmap.rows.values()]
        for chunk in (items[i:i + 500] for i in range(0, len(items), 500)):
            back = parse_csv(export_csv(chunk))
            self.assertEqual([e.print_id for e in back.entries], [e.print_id for e in chunk],
                             [e.reason for e in back.entries if e.reason][:3])


if __name__ == "__main__":
    unittest.main()
