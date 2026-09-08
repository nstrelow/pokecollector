import datetime
import unittest
from unittest.mock import MagicMock, patch

try:
    from services import scheduler
    from services.scheduler import run_fingerprint_backfill

    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


@unittest.skipUnless(DEPS_AVAILABLE, "Scheduler dependencies are not installed")
class FingerprintSchedulerTests(unittest.TestCase):
    def _run(self, result=None):
        db = MagicMock()
        result = result or {
            "considered": 0, "attempted": 0, "stored": 0, "skipped": 0,
            "cleared": 0, "placeholders": 0, "placeholders_undone": 0,
            "stopped_early": None,
        }
        with patch("database.SessionLocal", return_value=db), \
                patch("services.fingerprint_backfill.run_backfill",
                      return_value=result) as run_backfill:
            run_fingerprint_backfill()
        return db, run_backfill

    def test_the_job_passes_its_wall_clock_budget_down(self):
        """A batch that outlives the interval is thrown away, not queued.

        APScheduler is configured max_instances=1, coalesce=True: a run that
        overruns means the runs it overlapped are silently discarded, so the
        budget is what makes an "hourly" job actually hourly.
        """
        _db, run_backfill = self._run()
        kwargs = run_backfill.call_args.kwargs
        self.assertEqual(kwargs["limit"], scheduler._FINGERPRINT_BATCH_LIMIT)
        self.assertEqual(kwargs["rps"], scheduler._FINGERPRINT_RPS)
        self.assertEqual(
            kwargs["time_budget"], scheduler._FINGERPRINT_TIME_BUDGET_SECONDS
        )

    def test_the_budget_fits_inside_the_interval_between_runs(self):
        interval = datetime.timedelta(
            hours=scheduler._FINGERPRINT_INTERVAL_HOURS
        ).total_seconds()
        self.assertLess(scheduler._FINGERPRINT_TIME_BUDGET_SECONDS, interval)

    def test_the_pace_stays_polite_and_the_batch_bounded(self):
        self.assertLessEqual(scheduler._FINGERPRINT_RPS, 5.0)
        self.assertGreater(scheduler._FINGERPRINT_BATCH_LIMIT, 0)

    def test_the_session_is_always_returned_to_the_pool(self):
        db, _ = self._run()
        db.close.assert_called_once_with()

        db = MagicMock()
        with patch("database.SessionLocal", return_value=db), \
                patch("services.fingerprint_backfill.run_backfill",
                      side_effect=RuntimeError("cdn on fire")):
            run_fingerprint_backfill()   # must not propagate out of the job
        db.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
