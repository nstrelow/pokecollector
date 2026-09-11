import io
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


def _result(**overrides):
    result = {
        "considered": 10, "attempted": 10, "stored": 0, "skipped": 0,
        "cleared": 0, "placeholders": 0, "placeholders_undone": 0,
        "stopped_early": None,
    }
    result.update(overrides)
    return result


@unittest.skipUnless(DEPS_AVAILABLE, "Backfill CLI dependencies are not installed")
class BackfillFingerprintsCliTests(unittest.TestCase):
    """`fingerprint_cards` only calls `fingerprint_index.bump_version` when it
    stored, cleared or withheld something. A run that persisted nothing but
    negative-cache provenance -- `stored == cleared == placeholders == 0`, all
    the attempted rows were absent/undecodable and only `image_phash_source`
    was written -- skips that call, so the CLI's closing line must not claim
    it happened.
    """

    def _run(self, result):
        db = MagicMock()
        # Must stay the shape fingerprint_index.coverage() really returns; the
        # CLI prints every key and a stub missing one fails here, loudly, which
        # is the point of listing them rather than using a MagicMock.
        stats = {
            "cards_total": 0, "cards_with_image": 0, "cards_fingerprinted": 0,
            "coverage": 0.0, "catalogue_coverage": 0.0, "ready": False,
        }
        out = io.StringIO()
        with patch.object(backfill_fingerprints, "SessionLocal", return_value=db), \
                patch.object(backfill_fingerprints, "run_backfill", return_value=result), \
                patch.object(
                    backfill_fingerprints.fingerprint_index, "coverage",
                    return_value=stats,
                ), \
                patch.object(sys, "argv", ["backfill_fingerprints.py"]), \
                redirect_stdout(out):
            code = backfill_fingerprints.main()
        return code, out.getvalue()

    def test_the_closing_message_is_silent_when_nothing_bumped_the_marker(self):
        code, out = self._run(_result(stored=0, cleared=0, placeholders=0, skipped=7))
        self.assertEqual(code, 0)
        self.assertNotIn("bumped the shared index version marker", out)

    def test_the_closing_message_appears_when_something_did_bump_it(self):
        for overrides in ({"stored": 3}, {"cleared": 1}, {"placeholders": 9}):
            with self.subTest(**overrides):
                _code, out = self._run(_result(**overrides))
                self.assertIn("bumped the shared index version marker", out)


if __name__ == "__main__":
    unittest.main()
