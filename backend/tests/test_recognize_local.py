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

    def test_authentication_is_required(self):
        self.app.dependency_overrides.pop(get_current_user)

        def deny():
            raise HTTPException(status_code=401, detail="Not authenticated")

        self.app.dependency_overrides[get_current_user] = deny
        self.assertEqual(self._post(_jpeg(_card_image(1))).status_code, 401)
        self.assertEqual(
            self.client.get("/api/cards/recognize/local/status").status_code, 401
        )

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

    # --- status -------------------------------------------------------------

    def test_status_reports_coverage_and_readiness(self):
        self._add_card("sv1-30_en", image=_card_image(30))
        self._add_card("sv1-31_en", image_phash=None)
        body = self.client.get("/api/cards/recognize/local/status").json()
        self.assertEqual(body["cards_with_image"], 2)
        self.assertEqual(body["cards_fingerprinted"], 1)
        self.assertEqual(body["coverage"], 0.5)
        self.assertFalse(body["ready"])


if __name__ == "__main__":
    unittest.main()
