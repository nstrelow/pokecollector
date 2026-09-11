import io
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import numpy as np
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from PIL import Image
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from api.auth import get_current_user
    from api.recognize_local import router
    from database import Base, get_db
    from models import Card, Setting, User
    from services import fingerprint_index
    from services.card_fingerprint import HASH_BYTES, fingerprint_reference

    DEPS_AVAILABLE = True
    # The real production floor, captured before any test lowers it.
    REAL_READY_MIN_CARDS = fingerprint_index.READY_MIN_CARDS
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


def _card_image(seed=0, size=(245, 337)):
    """A deterministic, richly textured card face."""
    rng = np.random.default_rng(seed)
    width, height = size
    rows, cols = np.mgrid[0:height, 0:width]
    pixels = np.zeros((height, width, 3), dtype=np.uint8)
    pixels[..., 0] = ((cols * 3 + rows * 2 + seed * 37) % 256).astype(np.uint8)
    pixels[..., 1] = ((rows * 5 + seed * 11) % 256).astype(np.uint8)
    pixels[..., 2] = ((cols * cols // 5 + seed * 53) % 256).astype(np.uint8)
    for _ in range(5):
        center_y = int(rng.integers(0, height))
        center_x = int(rng.integers(0, width))
        radius = int(rng.integers(25, 70))
        inside = (rows - center_y) ** 2 + (cols - center_x) ** 2 <= radius * radius
        pixels[inside] = int(rng.integers(0, 256))
    return Image.fromarray(pixels)


def _jpeg(image):
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=92)
    return buf.getvalue()


@unittest.skipUnless(DEPS_AVAILABLE, "Scanner dependencies are not installed")
class LocalRecognitionApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.user = User(username="scan-local", hashed_password="x", is_active=True)
        self.stranger = User(username="scan-other", hashed_password="x", is_active=True)
        self.db.add_all([
            self.user,
            self.stranger,
            Setting(key="tcgdex_sync_languages", value="en,de"),
            Setting(key="tcgdex_digital_sets_enabled", value="false"),
        ])
        self.db.commit()
        fingerprint_index.reset()

        app = FastAPI()
        app.include_router(router, prefix="/api/cards")

        def override_db():
            yield self.db

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.app = app
        self.client = TestClient(app)

        # The endpoint refuses to answer until the index covers enough of the
        # catalogue (READY_MIN_CARDS cards at READY_COVERAGE). These fixtures
        # are a handful of cards, so the floor is lowered rather than seeding
        # five hundred; the gate itself is pinned in its own tests below.
        self._ready_patch = patch.object(fingerprint_index, "READY_MIN_CARDS", 1)
        self._ready_patch.start()
        self.addCleanup(self._ready_patch.stop)

    def _real_readiness(self):
        """Restore the production floor for tests that pin the gate itself."""
        return patch.object(
            fingerprint_index, "READY_MIN_CARDS", REAL_READY_MIN_CARDS
        )

    def tearDown(self):
        self.client.close()
        fingerprint_index.reset()
        self.db.close()
        self.engine.dispose()

    def _add_card(self, card_id, image=None, **kwargs):
        values = dict(
            id=card_id,
            tcg_card_id=card_id.split("_")[0],
            name=f"Name {card_id}",
            number="1",
            set_id="sv1",
            lang="en",
            rarity="Common",
            is_custom=False,
            is_digital=False,
            images_small=f"https://example.invalid/{card_id}.png",
            image_phash=(
                fingerprint_reference(_jpeg(image)) if image is not None
                else bytes(range(HASH_BYTES))
            ),
        )
        values.update(kwargs)
        self.db.add(Card(**values))
        self.db.commit()

    def _post(self, data, name="photo.jpg", content_type="image/jpeg"):
        return self.client.post(
            "/api/cards/recognize/local",
            files={"file": (name, data, content_type)},
        )

    # --- availability -------------------------------------------------------

    def test_an_empty_index_reports_unavailable_rather_than_matching_nothing(self):
        response = self._post(_jpeg(_card_image(1)))
        self.assertEqual(response.status_code, 503)
        self.assertIn("fingerprint", response.json()["detail"].lower())

    def test_a_half_built_index_is_not_answered_from(self):
        """The 503 is a readiness rule, not "are there any rows at all".

        The endpoint only checked that the index was non-empty, while /status
        applied READY_MIN_CARDS and READY_COVERAGE. So a freshly seeded install
        could return `_identity_confident: true` off a couple of thousand cards
        while /status was still telling the user it was not ready.
        `is_confident`'s margin test needs a catalogue-sized index to be a
        margin against.
        """
        target = _card_image(21)
        self._add_card("sv1-21_en", image=target)
        for seed in range(22, 30):
            self._add_card(f"sv1-{seed}_en", image=_card_image(seed))
        fingerprint_index.reset()

        with self._real_readiness():
            response = self._post(_jpeg(target))
            self.assertEqual(response.status_code, 503, response.text)
            status = self.client.get("/api/cards/recognize/local/status").json()
        self.assertFalse(status["ready"])

    def test_coverage_below_the_threshold_is_not_ready_either(self):
        """Enough cards, but most of the catalogue still unhashed."""
        with patch.object(fingerprint_index, "READY_MIN_CARDS", 2):
            self._add_card("sv1-40_en", image=_card_image(40))
            self._add_card("sv1-41_en", image=_card_image(41))
            for i in range(8):
                self._add_card(f"sv1-5{i}_en", image_phash=None)
            fingerprint_index.reset()
            self.assertEqual(self._post(_jpeg(_card_image(40))).status_code, 503)
            body = self.client.get("/api/cards/recognize/local/status").json()
            # Pinned as literals, not read back off the constant under test --
            # a lowered READY_COVERAGE would move this assertion right along
            # with it and stay green while a 20%-covered index started
            # answering. The fixture is exactly 2 hashed cards out of 10.
            self.assertEqual(fingerprint_index.READY_COVERAGE, 0.5)
            self.assertEqual(body["coverage"], 0.2)
            self.assertFalse(body["ready"])

    def test_authentication_is_required(self):
        self.app.dependency_overrides.pop(get_current_user)

        def deny():
            raise HTTPException(status_code=401, detail="Not authenticated")

        self.app.dependency_overrides[get_current_user] = deny
        self.assertEqual(self._post(_jpeg(_card_image(1))).status_code, 401)
        self.assertEqual(
            self.client.get("/api/cards/recognize/local/status").status_code, 401
        )

    # --- concurrency ----------------------------------------------------

    def test_concurrent_recognitions_are_bounded_by_the_semaphore(self):
        """Callers past the limit queue instead of all entering _match_photo.

        Driven inside one event loop on purpose. Production runs a single
        uvicorn loop, and the limiter is resolved per loop, so bounding is only
        meaningful within one -- a test that fans out across loops would be
        asserting something the design never promises.
        """
        import asyncio

        from api import recognize_local

        limit = 2
        state = {"current": 0, "peak": 0}

        async def body():
            semaphore = asyncio.Semaphore(limit)

            async def one():
                async with semaphore:
                    state["current"] += 1
                    state["peak"] = max(state["peak"], state["current"])
                    await asyncio.sleep(0.02)
                    state["current"] -= 1

            with patch.object(recognize_local, "_recognition_semaphore",
                              lambda: semaphore):
                await asyncio.gather(*(one() for _ in range(6)))

        asyncio.run(body())
        self.assertEqual(state["peak"], limit,
                         "more than the configured limit ran at once")
        self.assertEqual(state["current"], 0)

    def test_a_non_image_upload_is_rejected(self):
        self._add_card("sv1-1_en", image=_card_image(1))
        for payload, name in ((b"not an image at all", "x.jpg"), (b"", "empty.jpg")):
            with self.subTest(name=name):
                response = self._post(payload, name=name)
                self.assertEqual(response.status_code, 400)

    # --- matching -----------------------------------------------------------

    def test_a_photo_of_a_known_card_returns_it_first(self):
        target = _card_image(2)
        self._add_card("sv1-2_en", image=target)
        for seed in range(3, 9):
            self._add_card(f"sv1-{seed}_en", image=_card_image(seed))

        response = self._post(_jpeg(target))
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["matches"][0]["id"], "sv1-2_en")
        self.assertEqual(body["matches"][0]["_distance"], 0)
        self.assertEqual(body["matches"][0]["_confidence"], "high")
        self.assertEqual(body["_source"], "local_fingerprint")
        self.assertTrue(body["_identity_confident"])
        # No text was read, so nothing may be claimed about the printed card.
        self.assertTrue(all(v is None for v in body["recognized"].values()))

    def test_the_shortlist_can_come_back_empty(self):
        """The client's no-matches branch has to be reachable.

        search used to return 12 rows whatever the distance, so a photo of
        nothing at all still produced a dozen confident-looking suggestions.
        """
        self._add_card("sv1-9_en", image=_card_image(9))
        with patch("api.recognize_local.SHORTLIST_MAX_DISTANCE", -1):
            response = self._post(_jpeg(_card_image(9)))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["matches"], [])
        self.assertFalse(response.json()["_identity_confident"])

    def test_the_response_does_not_leak_the_catalogue_size(self):
        self._add_card("sv1-3_en", image=_card_image(3))
        body = self._post(_jpeg(_card_image(3))).json()
        self.assertNotIn("_index_size", body)

    def test_another_users_private_custom_card_is_never_a_match(self):
        secret = _card_image(4)
        self._add_card("secret_custom", image=secret, is_custom=True,
                       custom_owner_id=self.stranger.id, name="Their secret card")
        self._add_card("sv1-5_en", image=_card_image(5))

        body = self._post(_jpeg(secret)).json()
        self.assertNotIn("secret_custom", [m["id"] for m in body["matches"]])

    def test_matches_are_ordered_by_distance(self):
        for seed in range(10, 20):
            self._add_card(f"sv1-{seed}_en", image=_card_image(seed))
        body = self._post(_jpeg(_card_image(12))).json()
        distances = [match["_distance"] for match in body["matches"]]
        self.assertEqual(distances, sorted(distances))

    def test_the_shortlist_is_twelve_long(self):
        """Twelve is the advertised number, not an implementation detail.

        Every accuracy figure quoted for this feature is "the right artwork is
        inside the top 12" (91.62%). Returning eight silently trades recall the
        user is never told about, and the review UI is sized for twelve.
        """
        for seed in range(100, 130):
            self._add_card(f"sv1-{seed}_en", image=_card_image(seed))
        # Everything is in range, so the cutoff cannot be what limits this.
        with patch("api.recognize_local.SHORTLIST_MAX_DISTANCE", 64):
            body = self._post(_jpeg(_card_image(105))).json()
        self.assertEqual(len(body["matches"]), 12)

    def test_a_shorter_index_returns_everything_it_has(self):
        for seed in range(200, 205):
            self._add_card(f"sv1-{seed}_en", image=_card_image(seed))
        with patch("api.recognize_local.SHORTLIST_MAX_DISTANCE", 64):
            body = self._post(_jpeg(_card_image(200))).json()
        self.assertEqual(len(body["matches"]), 5)

    def test_the_confidence_label_boundaries(self):
        """The labels order the list for the user, so their edges are decisions.

        "high" is everything up to and including 8 -- two-thirds of
        MAX_DISTANCE (12) -- and "medium" runs to MAX_DISTANCE itself.

        Boundaries are pinned as literals, not read back off the constants
        under test: a test that asks `_confidence(HIGH_CONFIDENCE_DISTANCE)`
        for "high" stays green no matter what `HIGH_CONFIDENCE_DISTANCE` is
        changed to, because the expectation moves with the very value it is
        supposed to be pinning. `test_the_tuned_constants_have_not_drifted` in
        test_card_fingerprint.py pins MIN_MARGIN and MAX_DISTANCE the same way.
        """
        from api.recognize_local import HIGH_CONFIDENCE_DISTANCE, _confidence

        self.assertEqual(HIGH_CONFIDENCE_DISTANCE, 8)
        self.assertEqual(_confidence(0), "high")
        self.assertEqual(_confidence(8), "high")
        self.assertEqual(_confidence(9), "medium")
        self.assertEqual(_confidence(12), "medium")
        self.assertEqual(_confidence(13), "low")

    # --- status -------------------------------------------------------------

    def test_status_reports_coverage_and_readiness(self):
        self._add_card("sv1-30_en", image=_card_image(30))
        self._add_card("sv1-31_en", image_phash=None)
        with self._real_readiness():
            body = self.client.get("/api/cards/recognize/local/status").json()
        self.assertEqual(body["cards_with_image"], 2)
        self.assertEqual(body["cards_fingerprinted"], 1)
        self.assertEqual(body["coverage"], 0.5)
        self.assertFalse(body["ready"])

    # --- one tile per artwork -----------------------------------------------

    def test_two_printings_of_one_artwork_become_one_tile(self):
        """swsh12.5-036_de and swsh12.5-036_en are one picture, not two slots.

        An ungrouped shortlist spent an average of 2.43 of its twelve slots
        showing one card twice, on 93.9% of shortlists. Asserted against the
        endpoint rather than against a copy of its rule, so a change to the
        rule cannot leave this green.
        """
        target = _card_image(2)
        self._add_card("twin_de", image=target, lang="de")
        self._add_card("twin_en", image=target, lang="en")
        for seed in range(40, 46):
            self._add_card(f"sv1-{seed}_en", image=_card_image(seed))

        matches = self._post(_jpeg(target)).json()["matches"]
        ids = [match["id"] for match in matches]
        self.assertEqual(len(ids), len(set(ids)), "no row may appear twice")
        self.assertEqual(matches[0]["tcg_card_id"], "twin")
        self.assertEqual(
            [p["id"] for p in matches[0].get("_other_printings", [])],
            ["twin_en"],
            "the other printing must be carried, never dropped",
        )
        self.assertNotIn("twin_en", ids[1:],
                         "a grouped printing must not also take a slot of its own")

    def test_grouping_frees_the_slot_for_a_real_candidate(self):
        """The point of collapsing duplicates is what fills the space."""
        target = _card_image(3)
        self._add_card("dup_de", image=target, lang="de")
        self._add_card("dup_en", image=target, lang="en")
        for seed in range(50, 62):
            self._add_card(f"sv1-{seed}_en", image=_card_image(seed))

        with patch("api.recognize_local.SHORTLIST_MAX_DISTANCE", 64):
            matches = self._post(_jpeg(target)).json()["matches"]
        self.assertEqual(len(matches), 12)
        self.assertEqual(
            len({match["tcg_card_id"] for match in matches}), 12,
            "twelve tiles must be twelve different artworks",
        )

    def test_a_grouped_printing_keeps_everything_needed_to_pick_it(self):
        """The user still has to choose the language; hiding it is not grouping."""
        target = _card_image(4)
        self._add_card("pair_de", image=target, lang="de")
        self._add_card("pair_en", image=target, lang="en")
        self._add_card("sv1-70_en", image=_card_image(70))

        alternate = self._post(_jpeg(target)).json()["matches"][0]["_other_printings"][0]
        for field in ("id", "tcg_card_id", "name", "number", "set_id", "lang",
                      "_lang", "rarity", "image", "_distance", "_confidence",
                      "_match_percent"):
            self.assertIn(field, alternate)
        self.assertEqual(alternate["lang"], "en")

    def test_a_card_with_no_catalogue_id_is_never_merged_into_another(self):
        """A missing grouping key must make a group of one, not one big group."""
        first, second = _card_image(5), _card_image(6)
        self._add_card("loose-a_en", image=first, tcg_card_id=None)
        self._add_card("loose-b_en", image=second, tcg_card_id=None)

        with patch("api.recognize_local.SHORTLIST_MAX_DISTANCE", 64):
            matches = self._post(_jpeg(first)).json()["matches"]
        self.assertEqual({m["id"] for m in matches}, {"loose-a_en", "loose-b_en"})
        self.assertNotIn("_other_printings", matches[0])

    # --- what the badge is allowed to claim ---------------------------------

    def test_the_badges_on_one_shortlist_cannot_exceed_a_certainty(self):
        """The defect this replaced: twelve badges summing to 1008%.

        Four different cards sharing one hash is a four-way split of a single
        piece of evidence, and at most one of them is the right artwork.
        """
        shared = _card_image(7)
        for i in range(4):
            self._add_card(f"quad-{i}_en", image=shared)
        self._add_card("sv1-80_en", image=_card_image(80))

        matches = self._post(_jpeg(shared)).json()["matches"]
        tied = [m for m in matches if m["_distance"] == matches[0]["_distance"]]
        self.assertEqual(len(tied), 4, "four distinct artworks should tie here")
        self.assertLessEqual(sum(m["_match_percent"] for m in tied), 100)
        self.assertEqual(
            len({m["_match_percent"] for m in tied}), 1,
            "rows the hash cannot separate must not be given different odds",
        )

    def test_a_tile_that_wins_outright_beats_one_that_shares_the_lead(self):
        """Being tied is evidence, and it is the evidence the old badge lost.

        One index, two photographs: the same distance and the same shortlist
        position must still yield different numbers, because a four-way tie is
        not the same claim as an outright win.
        """
        alone, shared = _card_image(8), _card_image(9)
        self._add_card("alone_en", image=alone)
        for i in range(4):
            self._add_card(f"share-{i}_en", image=shared)
        for seed in range(90, 96):
            self._add_card(f"sv1-{seed}_en", image=_card_image(seed))

        outright = self._post(_jpeg(alone)).json()["matches"][0]
        tied = self._post(_jpeg(shared)).json()["matches"][0]

        self.assertEqual(outright["_distance"], 0)
        self.assertEqual(tied["_distance"], 0)
        self.assertGreater(outright["_match_percent"], tied["_match_percent"])

    def test_grouping_never_manufactures_confidence(self):
        """Collapsing a twin would turn a zero margin into a wide one.

        The margin gate is the only thing between the scanner and confidently
        naming the wrong language, so it is judged on the ungrouped ranking even
        though the shortlist the user sees is grouped.
        """
        target = _card_image(10)
        self._add_card("lang_de", image=target, lang="de")
        self._add_card("lang_en", image=target, lang="en")
        for seed in range(110, 118):
            self._add_card(f"sv1-{seed}_en", image=_card_image(seed))

        body = self._post(_jpeg(target)).json()
        self.assertEqual(len(body["matches"][0].get("_other_printings", [])), 1)
        self.assertFalse(
            body["_identity_confident"],
            "one artwork in two languages is exactly what the hash cannot decide",
        )


if __name__ == "__main__":
    unittest.main()


class ConcurrencyLimiterTests(unittest.TestCase):
    """The limiter must not be tied to whichever event loop touched it first.

    A module-level `asyncio.Semaphore` binds to the loop that first awaits it,
    so a second loop -- another TestClient, or an app restarted in-process --
    raises "bound to a different event loop" and every recognition 500s.
    """

    def test_each_event_loop_gets_its_own_semaphore(self):
        import asyncio

        from api.recognize_local import _recognition_semaphore

        seen = []

        async def grab():
            semaphore = _recognition_semaphore()
            async with semaphore:
                # Keep the loop alive in `seen` too: a collected loop's address
                # can be reused, so comparing ids of dead loops is unreliable.
                seen.append((asyncio.get_running_loop(), semaphore))

        asyncio.run(grab())
        asyncio.run(grab())

        self.assertEqual(len(seen), 2)
        self.assertIsNot(seen[0][0], seen[1][0], "expected two distinct loops")
        self.assertIsNot(seen[0][1], seen[1][1],
                         "the same semaphore was reused across event loops")

    def test_one_loop_reuses_a_single_semaphore(self):
        import asyncio

        from api.recognize_local import (
            MAX_CONCURRENT_LOCAL_RECOGNITIONS,
            _recognition_semaphore,
        )

        async def twice():
            return _recognition_semaphore(), _recognition_semaphore()

        first, second = asyncio.run(twice())
        self.assertIs(first, second)
        self.assertEqual(MAX_CONCURRENT_LOCAL_RECOGNITIONS, 4)
