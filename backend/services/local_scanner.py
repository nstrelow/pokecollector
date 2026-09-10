"""The per-user switch behind the "Scanner v2 (Beta)" toggle.

Offline recognition (`api/recognize_local.py`) matches a photo against locally
stored artwork fingerprints. It needs no provider credential, no internet and no
card name -- but it reads no text at all, so it cannot separate two language
printings of one artwork, and it abstains on cluttered frames. That trade is a
per-collector choice rather than an installation-wide one, so the flag lives in
`user_settings` beside the provider keys instead of in `settings`.

Kept in its own module so both `api/settings.py` (which owns the write path) and
`services/scan_queue.py` (which owns the background scan worker) can read it
without importing each other.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from models import UserSetting

LOCAL_SCANNER_SETTING_KEY = "local_scanner_enabled"

# Stored as the same "true"/"false" text every other boolean user setting uses,
# so the generic settings endpoints coerce and return it without a special case.
_TRUE = "true"


def local_scanner_enabled(db: Session, user_id: int) -> bool:
    """Whether this user's scans should skip the vision provider entirely."""
    row = (
        db.query(UserSetting)
        .filter(
            UserSetting.user_id == user_id,
            UserSetting.key == LOCAL_SCANNER_SETTING_KEY,
        )
        .first()
    )
    return row is not None and str(row.value or "").strip().lower() == _TRUE
