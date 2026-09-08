"""Populate cards.image_phash so offline recognition can work.

Downloads each card's artwork once and stores a 64-bit fingerprint. Resumable:
cards that already have a fingerprint are skipped, so it can be stopped and
restarted freely.

TCGdex is a free, community-run service, so requests are paced globally rather
than issued as fast as the CDN will allow.

    docker compose exec backend python scripts/backfill_fingerprints.py
    docker compose exec backend python scripts/backfill_fingerprints.py --lang en
    docker compose exec backend python scripts/backfill_fingerprints.py --rps 3
"""
import argparse
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from database import SessionLocal  # noqa: E402
from models import Card  # noqa: E402
from services import fingerprint_index  # noqa: E402
from services.card_fingerprint import fingerprint_reference  # noqa: E402

ABSENT = {400, 401, 403, 404, 410}   # do not retry these

_rate_lock = threading.Lock()
_next_slot = [0.0]


def _pace(rps: float) -> None:
    """Global pacing shared across worker threads."""
    interval = 1.0 / rps
    with _rate_lock:
        slot = max(time.monotonic(), _next_slot[0])
        _next_slot[0] = slot + interval
    delay = slot - time.monotonic()
    if delay > 0:
        time.sleep(delay)


def _download(client: httpx.Client, url: str, rps: float, attempts: int = 4) -> bytes | None:
    delay = 1.0
    for attempt in range(attempts):
        try:
            _pace(rps)
            response = client.get(url, timeout=30)
            if response.status_code == 200 and response.content:
                return response.content
            if response.status_code in ABSENT:
                return None
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = max(delay, min(float(retry_after), 60.0))
                except ValueError:
                    pass
        except Exception:
            pass
        if attempt < attempts - 1:
            time.sleep(delay + random.random() * 0.5)
            delay = min(delay * 2, 20.0)
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lang", action="append", default=None,
                        help="restrict to a language (repeatable)")
    parser.add_argument("--rps", type=float, default=5.0,
                        help="requests per second against the image CDN")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0, help="stop after N cards")
    parser.add_argument("--refresh", action="store_true",
                        help="recompute fingerprints that already exist")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        query = db.query(Card.id, Card.images_small).filter(
            Card.images_small.isnot(None)
        )
        if not args.refresh:
            query = query.filter(Card.image_phash.is_(None))
        if args.lang:
            query = query.filter(Card.lang.in_(args.lang))
        todo = query.all()
        if args.limit:
            todo = todo[: args.limit]

        if not todo:
            print("Nothing to do -- every card with an image already has a fingerprint.")
            return 0

        print(f"Fingerprinting {len(todo)} cards at ~{args.rps} req/s "
              f"({args.workers} workers)...", flush=True)

        done = skipped = 0
        lock = threading.Lock()
        pending: list[tuple[str, bytes]] = []

        with httpx.Client(
            headers={"User-Agent": "pokecollector/backfill-fingerprints"},
            limits=httpx.Limits(max_connections=args.workers),
        ) as client:
            def work(row):
                data = _download(client, row.images_small, args.rps)
                if not data:
                    return row.id, None
                return row.id, fingerprint_reference(data)

            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                for i, (card_id, digest) in enumerate(pool.map(work, todo), start=1):
                    if digest is None:
                        skipped += 1
                    else:
                        with lock:
                            pending.append((card_id, digest))
                    # Commit in batches so an interrupted run keeps its progress.
                    if len(pending) >= 200:
                        for cid, value in pending:
                            db.query(Card).filter(Card.id == cid).update(
                                {"image_phash": value}, synchronize_session=False
                            )
                        db.commit()
                        done += len(pending)
                        pending.clear()
                        print(f"  {i}/{len(todo)} stored={done} skipped={skipped}",
                              flush=True)

        for cid, value in pending:
            db.query(Card).filter(Card.id == cid).update(
                {"image_phash": value}, synchronize_session=False
            )
        db.commit()
        done += len(pending)

        print(f"\nDone. {done} fingerprinted, {skipped} had no usable image.")
        fingerprint_index.invalidate()
        print(fingerprint_index.coverage(db))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
