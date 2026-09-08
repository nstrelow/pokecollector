"""Unit tests for the offline-fingerprint backfill command."""

import io
import math
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from scripts import backfill_fingerprints

    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


@unittest.skipUnless(DEPS_AVAILABLE, "Backend dependencies are not installed")
class BackfillFingerprintsScriptTests(unittest.TestCase):
    def _result(self, **overrides):
        result = {
            "considered": 3,
            "attempted": 3,
            "stored": 2,
            "skipped": 1,
            "cleared": 0,
            "placeholders": 0,
            "placeholders_undone": 0,
            "stopped_early": None,
        }
        result.update(overrides)
        return result

    def _run(self, args, result=None, run_side_effect=None):
        db = MagicMock()
        output = io.StringIO()
        stats = {
            "cards_total": 1000,
            "cards_with_image": 800,
            "cards_fingerprinted": 600,
            "coverage": 0.75,
            "ready": True,
        }
        with patch.object(sys, "argv", ["backfill_fingerprints", *args]), \
             patch.object(backfill_fingerprints, "SessionLocal", return_value=db), \
             patch.object(
                 backfill_fingerprints,
                 "run_backfill",
                 return_value=result or self._result(),
                 side_effect=run_side_effect,
             ) as run_backfill, \
             patch.object(
                 backfill_fingerprints.fingerprint_index,
                 "coverage",
                 return_value=stats,
             ) as coverage, \
             redirect_stdout(output):
            exit_code = backfill_fingerprints.main()
        return exit_code, output.getvalue(), db, run_backfill, coverage

    def test_defaults_are_forwarded_to_the_shared_backfill_service(self):
        exit_code, output, db, run_backfill, coverage = self._run([])

        self.assertEqual(exit_code, 0)
        kwargs = run_backfill.call_args.kwargs
        self.assertIs(run_backfill.call_args.args[0], db)
        self.assertIsNone(kwargs["langs"])
        self.assertFalse(kwargs["refresh"])
        self.assertEqual(kwargs["limit"], 0)
        self.assertEqual(kwargs["rps"], backfill_fingerprints.DEFAULT_RPS)
        self.assertEqual(kwargs["workers"], backfill_fingerprints.DEFAULT_WORKERS)
        self.assertTrue(math.isinf(kwargs["time_budget"]))
        self.assertTrue(kwargs["shuffle"])
        self.assertTrue(callable(kwargs["on_progress"]))
        coverage.assert_called_once_with(db)
        db.close.assert_called_once_with()
        self.assertIn("ready for scanning          yes", output)

    def test_repeatable_languages_and_all_execution_flags_are_forwarded(self):
        args = [
            "--lang", "en", "--lang", "de", "--rps", "2.5",
            "--workers", "3", "--limit", "17", "--time-budget", "45",
            "--in-order", "--refresh",
        ]

        exit_code, _output, _db, run_backfill, _coverage = self._run(args)

        self.assertEqual(exit_code, 0)
        kwargs = run_backfill.call_args.kwargs
        self.assertEqual(kwargs["langs"], ["en", "de"])
        self.assertTrue(kwargs["refresh"])
        self.assertEqual(kwargs["limit"], 17)
        self.assertEqual(kwargs["rps"], 2.5)
        self.assertEqual(kwargs["workers"], 3)
        self.assertEqual(kwargs["time_budget"], 45.0)
        self.assertFalse(kwargs["shuffle"])

    def test_progress_and_early_stop_are_reported_for_a_resumable_run(self):
        result = self._result(
            considered=10,
            attempted=4,
            stored=3,
            stopped_early="time budget exhausted",
        )

        def run_and_report(_db, **kwargs):
            kwargs["on_progress"](4, 10, 3, 1, 0)
            return result

        exit_code, output, _db, _run_backfill, _coverage = self._run(
            [], result=result, run_side_effect=run_and_report
        )

        self.assertEqual(exit_code, 0)
        self.assertIn("4/10 stored=3 skipped=1 cleared=0", output)
        self.assertIn("Stopped early: time budget exhausted. 4 of 10 attempted", output)
        self.assertIn("fingerprinted               600  (75.0%)", output)

    def test_no_pending_cards_skips_coverage_but_still_closes_the_session(self):
        result = self._result(
            considered=0,
            attempted=0,
            stored=0,
            skipped=0,
        )

        exit_code, output, db, _run_backfill, coverage = self._run([], result=result)

        self.assertEqual(exit_code, 0)
        self.assertIn("Nothing to do", output)
        coverage.assert_not_called()
        db.close.assert_called_once_with()

    def test_backfill_failure_propagates_after_closing_the_session(self):
        db = MagicMock()
        with patch.object(sys, "argv", ["backfill_fingerprints"]), \
             patch.object(backfill_fingerprints, "SessionLocal", return_value=db), \
             patch.object(
                 backfill_fingerprints,
                 "run_backfill",
                 side_effect=RuntimeError("database unavailable"),
             ), \
             redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                backfill_fingerprints.main()

        db.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
