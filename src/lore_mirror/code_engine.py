"""Shared optional-index identity policy, independent of writer and reader."""
from pathlib import Path

from .code_common import SHA256

ENGINE_COMMIT = "e54cd91d1a64864fda7e681c9d31171eaab09ca7"
INDEXER_BINARY = Path("/usr/local/bin/zoekt-git-index")
QUERY_BINARY = Path("/usr/local/bin/lore-zoekt")
CTAGS_BINARY = Path("/usr/bin/ctags-universal")
BUILD_MANIFEST = Path("/usr/local/share/lore-mirror/zoekt-build.json")
MAX_BINARY_BYTES = 64 * 1024**2
MAX_TRIGRAM_COUNT = 20000


class EngineIdentityError(ValueError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def verify_engine_identity(manifest, identity, *, symbols=False):
    """Bind both executable hashes and optional ctags to the pinned engine."""
    if (not isinstance(manifest, dict) or manifest.get("upstream_commit") != ENGINE_COMMIT
            or not isinstance(identity, dict) or identity.get("engine_commit") != ENGINE_COMMIT):
        raise EngineIdentityError("engine")
    for key in ("indexer_sha256", "query_bridge_sha256", *(('ctags_sha256',) if symbols else ())):
        digest = identity.get(key)
        if not isinstance(digest, str) or not SHA256.fullmatch(digest) or manifest.get(key) != digest:
            raise EngineIdentityError("symbols" if key == "ctags_sha256" else "binary")
    return identity
