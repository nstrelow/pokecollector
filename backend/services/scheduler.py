from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.date import DateTrigger
import logging
import datetime

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler()

_DEFAULT_FULL_SYNC_DAYS = 5
_DEFAULT_PRICE_SYNC_MINUTES = 30

# Offline-recognition fingerprints. At 5 req/s a 2,000-card batch is ~7 minutes
# of CDN time IF every download succeeds on the first attempt. It does not
# follow that a 45k catalogue clears in a day: `download_image` retries 4 times
# at a 30s timeout with 1+2+4s of backoff, so a card that is simply unreachable
# costs up to ~127s, and 2,000 of those over 4 workers is 17.6 hours. That is
# why the run has a wall-clock budget as well as a row limit -- and why an
# honest estimate is "a healthy catalogue clears in about a day, a sick one
# takes as long as it takes, a budget-limited batch at a time".
#
# The budget is well under the hour between runs, so a healthy run is always
# finished by the time the next one is due, and max_instances=1 never has
# reason to skip an overlapping start.
#
# misfire_grace_time is set explicitly rather than left at APScheduler's
# 1-second default. That default means a late run isn't queued at all: if the
# scheduler's check for this job lands more than a second after its due time
# (a slow prior run, GIL contention, the process briefly stalled), APScheduler
# logs it as misfired and drops it outright, and the job simply waits for its
# next hourly slot. coalesce=True never enters into it at 1 second -- there is
# nothing to coalesce when a miss is never allowed to accumulate. The grace
# period below is what actually gives coalesce=True something to do: if the
# process is unresponsive for a few minutes, one or more due firings can build
# up within it, and coalesce=True runs them once instead of back-to-back.
_FINGERPRINT_BATCH_LIMIT = 2000
_FINGERPRINT_RPS = 5.0
_FINGERPRINT_INTERVAL_HOURS = 1
_FINGERPRINT_TIME_BUDGET_SECONDS = 40 * 60.0
_FINGERPRINT_MISFIRE_GRACE_SECONDS = 10 * 60


def _get_full_sync_interval_days() -> int:
    """Read full sync interval from DB settings."""
    try:
        from database import SessionLocal
        from models import Setting
        with SessionLocal() as db:
            row = db.query(Setting).filter(Setting.key == "full_sync_interval_days").first()
            if row:
                return int(row.value)
    except Exception:
        pass
    return _DEFAULT_FULL_SYNC_DAYS


def _get_price_sync_interval_minutes() -> int:
    """Read price sync interval from DB settings."""
    try:
        from database import SessionLocal
        from models import Setting
        with SessionLocal() as db:
            row = db.query(Setting).filter(Setting.key == "price_sync_interval_minutes").first()
            if row:
                return int(row.value)
    except Exception:
        pass
    return _DEFAULT_PRICE_SYNC_MINUTES


def run_full_sync():
    """Full sync job — syncs sets + cards + prices."""
    from database import SessionLocal
    from services.sync_service import perform_full_sync

    db = SessionLocal()
    try:
        logger.info("Starting scheduled full sync...")
        perform_full_sync(db)
        logger.info("Scheduled full sync completed successfully")
    except Exception as e:
        logger.error(f"Scheduled full sync failed: {e}")
    finally:
        db.close()


def run_price_sync():
    """Price-only sync job."""
    from database import SessionLocal
    from services.sync_service import perform_price_sync

    db = SessionLocal()
    try:
        logger.info("Starting scheduled price sync...")
        perform_price_sync(db)
        logger.info("Scheduled price sync completed successfully")
    except Exception as e:
        logger.error(f"Scheduled price sync failed: {e}")
    finally:
        db.close()


def run_scan_queue_maintenance():
    """Recover, process, and expire persistent background scans."""
    import asyncio

    from database import SessionLocal
    from services.gemini_rate_limit import purge_stale_quota_states
    from services.provider_rate_limit import purge_stale_provider_limit_states
    from services.scan_queue import (
        drain_scan_queue,
        purge_expired_scan_jobs,
        recover_expired_leases,
    )
    from services.scan_storage import purge_orphaned_scan_directories

    db = SessionLocal()
    try:
        recovered = recover_expired_leases(db)
        removed = purge_expired_scan_jobs(db)
        orphans = purge_orphaned_scan_directories(db)
        stale_quotas = purge_stale_quota_states()
        stale_provider_limits = purge_stale_provider_limit_states()
        if recovered or removed or orphans or stale_quotas or stale_provider_limits:
            logger.info(
                "Scan queue maintenance recovered %s lease(s), expired %s job(s), "
                "removed %s orphaned upload directory/directories, and purged %s "
                "inactive Gemini quota state(s) and %s provider limit state(s)",
                recovered,
                removed,
                orphans,
                stale_quotas,
                stale_provider_limits,
            )
    except Exception:
        db.rollback()
        logger.exception("Scan queue recovery/expiry failed")
    finally:
        db.close()

    try:
        asyncio.run(drain_scan_queue(max_items=50))
    except Exception:
        logger.exception("Scan queue processing failed")


def run_pokedex_metadata_backfill():
    """One-time startup backfill for Pokédex mappings added to existing card rows."""
    from database import SessionLocal
    from services.pokedex_backfill import run_pokedex_metadata_backfill as run_backfill
    from services.pokedex_backfill import startup_pokedex_backfill_batch_delay_seconds
    from services.pokedex_backfill import startup_pokedex_backfill_batch_limit

    db = SessionLocal()
    try:
        logger.info("Starting one-time Pokédex metadata backfill...")
        result = run_backfill(
            db,
            batch_limit=startup_pokedex_backfill_batch_limit(),
            batch_delay_seconds=startup_pokedex_backfill_batch_delay_seconds(),
        )
        if result.get("skipped"):
            logger.info("Pokédex metadata backfill skipped: %s", result.get("reason"))
        elif result.get("completed"):
            logger.info(
                "Pokédex metadata backfill completed: attempted=%s updated=%s missing=%s failed=%s batches=%s",
                result["attempted"],
                result["updated"],
                result["missing"],
                result["failed"],
                result["batches"],
            )
        else:
            logger.warning(
                "Pokédex metadata backfill stopped before completion: attempted=%s updated=%s missing=%s failed=%s batches=%s",
                result["attempted"],
                result["updated"],
                result["missing"],
                result["failed"],
                result["batches"],
            )
    except Exception as e:
        logger.error("Pokédex metadata backfill failed: %s", e)
    finally:
        db.close()


def run_fingerprint_backfill():
    """Keep cards.image_phash populated as the catalogue changes.

    Without this, offline recognition coverage only ever decays: a newly synced
    card has no fingerprint at all, and a card whose artwork URL rotates has its
    old one dropped. This job picks up whatever is outstanding -- including rows
    whose stored fingerprint no longer matches their current artwork URL, which
    is how a writer that forgot to clear one gets repaired -- a bounded batch at
    a time, paced politely against TCGdex.

    Bounded by rows AND by wall clock, and it gives up early if the CDN is
    plainly not answering. The run is meant to finish inside the scheduler's
    interval; see `_FINGERPRINT_MISFIRE_GRACE_SECONDS` above for what actually
    happens on the rare run that does not.
    """
    from database import SessionLocal
    from services.fingerprint_backfill import run_backfill

    db = SessionLocal()
    try:
        result = run_backfill(
            db,
            limit=_FINGERPRINT_BATCH_LIMIT,
            rps=_FINGERPRINT_RPS,
            time_budget=_FINGERPRINT_TIME_BUDGET_SECONDS,
        )
        # Always logged, even when `considered` is 0. A catalogue-wide outage
        # (e.g. a CDN 403 wave) can make `pending_cards()` legitimately return
        # nothing every run -- every row already carries a negative-cache
        # provenance -- and that must not look identical in the logs to a
        # healthy, fully-covered catalogue idling. Suppressing this line
        # whenever there was nothing to do is exactly what made a wiped queue
        # silent.
        logger.info(
            "Fingerprint backfill: considered=%s attempted=%s stored=%s "
            "skipped=%s cleared=%s placeholders=%s stopped_early=%s",
            result["considered"], result["attempted"], result["stored"],
            result["skipped"], result["cleared"], result["placeholders"],
            result["stopped_early"],
        )
    except Exception:
        logger.exception("Fingerprint backfill failed")
    finally:
        db.close()


# Keep legacy alias
def run_sync():
    """Legacy alias for run_full_sync."""
    run_full_sync()


def start_scheduler():
    """Start the background scheduler with separate full and small price sync jobs."""
    if not scheduler.running:
        now_utc = datetime.datetime.now(datetime.timezone.utc)

        # Only run full sync immediately on first boot if DB has no cards
        from database import SessionLocal
        from models import Card
        from services.pokedex_backfill import (
            missing_pokedex_metadata_count,
            pokedex_metadata_backfill_completed,
            startup_pokedex_backfill_enabled,
        )
        with SessionLocal() as db:
            needs_initial_sync = db.query(Card).count() == 0
            needs_pokedex_backfill = (
                startup_pokedex_backfill_enabled()
                and not pokedex_metadata_backfill_completed(db)
                and missing_pokedex_metadata_count(db) > 0
            )

        full_interval_days = _get_full_sync_interval_days()
        price_interval_minutes = _get_price_sync_interval_minutes()

        # Job 1: Full sync (sets + cards + tracked prices)
        full_next_run = now_utc if needs_initial_sync else now_utc + datetime.timedelta(days=full_interval_days)
        scheduler.add_job(
            run_full_sync,
            trigger=IntervalTrigger(days=full_interval_days),
            id="full_sync_job",
            name="Pokemon TCG Full Sync",
            replace_existing=True,
            next_run_time=full_next_run,
        )

        # Recurring auto sync: small price sync.
        scheduler.add_job(
            run_price_sync,
            trigger=IntervalTrigger(minutes=price_interval_minutes),
            id="price_sync_job",
            name="Pokemon TCG Price Sync",
            replace_existing=True,
            next_run_time=now_utc + datetime.timedelta(minutes=price_interval_minutes),
        )

        scheduler.add_job(
            run_scan_queue_maintenance,
            trigger=IntervalTrigger(minutes=1),
            id="scan_queue_maintenance_job",
            name="Persistent Scan Queue",
            replace_existing=True,
            next_run_time=now_utc + datetime.timedelta(seconds=45),
        )

        # Deliberately not at startup: this downloads images, so it must not sit
        # in front of the app becoming available.
        scheduler.add_job(
            run_fingerprint_backfill,
            trigger=IntervalTrigger(hours=_FINGERPRINT_INTERVAL_HOURS),
            id="fingerprint_backfill_job",
            name="Card Image Fingerprints",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=_FINGERPRINT_MISFIRE_GRACE_SECONDS,
            next_run_time=now_utc + datetime.timedelta(minutes=10),
        )

        if needs_pokedex_backfill:
            scheduler.add_job(
                run_pokedex_metadata_backfill,
                trigger=DateTrigger(run_date=now_utc + datetime.timedelta(seconds=30)),
                id="pokedex_metadata_backfill_job",
                name="One-time Pokédex Metadata Backfill",
                replace_existing=True,
            )

        scheduler.start()
        logger.info(
            f"Scheduler started — full sync every {full_interval_days} days "
            f"({'immediately' if needs_initial_sync else f'in {full_interval_days} days'}), "
            f"small price sync every {price_interval_minutes} minutes"
            f"{', one-time Pokédex metadata backfill scheduled' if needs_pokedex_backfill else ''}"
        )
    else:
        logger.info("Scheduler already running")


def stop_scheduler():
    """Stop the background scheduler."""
    if scheduler.running:
        scheduler.shutdown()
        logger.info("Scheduler stopped")


def reschedule_full_sync(interval_days: int):
    """Reschedule the full sync job with a new interval."""
    if scheduler.running:
        scheduler.reschedule_job(
            "full_sync_job",
            trigger=IntervalTrigger(days=interval_days),
        )
        logger.info(f"Full sync rescheduled to every {interval_days} days")


def reschedule_price_sync(interval_minutes: int):
    """Reschedule the price sync job with a new interval."""
    if scheduler.running:
        scheduler.reschedule_job(
            "price_sync_job",
            trigger=IntervalTrigger(minutes=interval_minutes),
        )
        logger.info(f"Price sync rescheduled to every {interval_minutes} minutes")
