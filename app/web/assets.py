"""Cache-busting tags for the static files this app serves itself.

Static assets are served with far-future caching in mind, which is right until
the moment a deploy changes one: the browser keeps the old stylesheet and the
new markup, and the result looks like a bug in whatever was shipped. The tag is
a digest of the file, so it changes exactly when the file does — the cache is
bypassed on the deploy that changed it and used on every request after.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

STATIC_DIR = Path(__file__).parent / "static"


@lru_cache(maxsize=32)
def _digest(name: str) -> str:
    path = STATIC_DIR / name
    try:
        return hashlib.blake2b(path.read_bytes(), digest_size=6).hexdigest()
    except OSError:
        # A missing file is the template's problem to show, not ours to crash on.
        return "0"


def static_url(name: str) -> str:
    return f"/static/{name}?v={_digest(name)}"
