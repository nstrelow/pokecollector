"""Populate cards.image_phash so offline recognition can work.

Downloads each card's artwork once and stores a 64-bit fingerprint. Resumable:
cards that already have a fingerprint are skipped, so it can be stopped and
restarted freely.

The scheduler runs the same work hourly in bounded batches, so this script is
only needed to seed a catalogue quickly or to force a refresh. The downloading
and storing rules live in services/fingerprint_backfill.py, shared with that
job, including the global request pacing against TCGdex.

    docker compose exec backend python scripts/backfill_fingerprints.py
    docker compose exec backend python scripts/backfill_fingerprints.py --lang en
    docker compose exec backend python scripts/backfill_fingerprints.py --rps 3
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import SessionLocal  # noqa: E402
from services import card_embedding, fingerprint_index  # noqa: E402
from services.fingerprint_backfill import (  # noqa: E402
    DEFAULT_RPS,
    DEFAULT_WORKERS,
    run_backfill,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lang", action="append", default=None,
                        help="restrict to a language (repeatable)")
    parser.add_argument("--rps", type=float, default=DEFAULT_RPS,
                        help="requests per second against the image CDN")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--limit", type=int, default=0, help="stop after N cards")
    parser.add_argument("--time-budget", type=float, default=0.0,
                        help="stop after N seconds (0 = no limit)")
    parser.add_argument("--in-order", action="store_true",
                        help="walk the queue by card id instead of at random; "
                             "reproducible, but a block of always-failing cards "
                             "will hold up everything behind it")
    parser.add_argument("--refresh", action="store_true",
                        help="recompute fingerprints that already exist, and "
                             "retry cards previously recorded as permanently "
                             "unusable; a card whose image is definitively gone "
                             "(404/410) has its stale fingerprint removed, while "
                             "an unreachable CDN leaves it untouched")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        def progress(index, total, stored, skipped, cleared):
            print(f"  {index}/{total} stored={stored} skipped={skipped} "
                  f"cleared={cleared}", flush=True)

        print(f"Fingerprinting at ~{args.rps} req/s ({args.workers} workers)...",
              flush=True)
        result = run_backfill(
            db,
            langs=args.lang,
            refresh=args.refresh,
            limit=args.limit,
            rps=args.rps,
            workers=args.workers,
            time_budget=args.time_budget or float("inf"),
            shuffle=not args.in_order,
            on_progress=progress,
        )
        if not result["considered"]:
            print("Nothing to do -- every card with an image has either been "
                  "fingerprinted or already tried. Use --refresh to retry.")
            return 0

        print(f"\nDone. {result['stored']} fingerprinted, "
              f"{result['skipped']} had no usable image, "
              f"{result['cleared']} stale fingerprints removed, "
              f"{result['placeholders']} discarded as CDN placeholders.")
        if result["stopped_early"]:
            print(f"Stopped early: {result['stopped_early']}. "
                  f"{result['attempted']} of {result['considered']} attempted; "
                  f"run again to continue.")

        stats = fingerprint_index.coverage(db)
        print("\nOffline recognition coverage:")
        print(f"  catalogue cards        {stats['cards_total']:>8,}")
        print(f"  with artwork           {stats['cards_with_image']:>8,}")
        print(f"  fingerprinted          {stats['cards_fingerprinted']:>8,}"
              f"  ({stats['coverage']:.1%} of those with artwork,"
              f" {stats['catalogue_coverage']:.1%} of the catalogue)")
        print(f"  ready for scanning     {'yes' if stats['ready'] else 'not yet':>8}")
        if card_embedding.available():
            print(f"  dense embeddings       enabled ({card_embedding.model_path()})")
        else:
            print("  dense embeddings       off -- set LOCAL_SCANNER_MODEL to a "
                  "DINOv2 ONNX export to enable the accurate path")
        # Matches fingerprint_cards' own condition for calling bump_version: a
        # run that only wrote negative-cache provenance (no hash stored,
        # cleared or withheld) never bumps the marker, so the message must not
        # claim it did.
        if result["stored"] or result["cleared"] or result["placeholders"]:
            print("\nA running server picks these up on its next scan: this "
                  "run bumped the shared index version marker in the "
                  "settings table.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
