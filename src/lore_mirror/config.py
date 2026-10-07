from __future__ import annotations
import json
from pathlib import Path
from .errors import MirrorError

DEFAULT_LIMITS = {"page_size": 20, "max_page_size": 100, "response_bytes": 65536,
                  "query_timeout": 30, "scan_limit": 10000, "cursor_ttl": 900,
                  "raw_bytes": 32 * 1024 * 1024, "query_output_bytes": 16 * 1024 * 1024,
                  "max_sessions": 64, "session_bytes": 32 * 1024 * 1024}

def load_config(value):
    cfg = json.loads(Path(value).read_text()) if isinstance(value, (str, Path)) else dict(value)
    if not isinstance(cfg, dict) or not cfg.get("data_dir"):
        raise MirrorError("invalid_config", "data_dir is required")
    cfg["data_dir"] = str(Path(cfg["data_dir"]).expanduser().resolve())
    for key, default in (("runtime_dir", "runtime"), ("issue_dir", "issues")):
        cfg[key] = str(Path(cfg.get(key, Path(cfg["data_dir"]) / default)).expanduser().resolve())
    cfg.setdefault("backend", "public-inbox")
    if cfg["backend"] not in ("fixture", "lei", "public-inbox"):
        raise MirrorError("invalid_config", "backend must be fixture, lei or public-inbox")
    cfg["limits"] = DEFAULT_LIMITS | cfg.get("limits", {})
    for key, number in cfg["limits"].items():
        if isinstance(number, bool) or not isinstance(number, (int, float)) or number <= 0:
            raise MirrorError("invalid_config", f"Invalid limit {key}")
    cfg["limits"]["max_page_size"] = min(int(cfg["limits"]["max_page_size"]), 100)
    cfg["limits"]["response_bytes"] = min(int(cfg["limits"]["response_bytes"]), 65536)
    cfg["limits"]["scan_limit"] = min(int(cfg["limits"]["scan_limit"]), 10000)
    cfg.setdefault("lists", [])
    if len(cfg["lists"]) > 1000:
        raise MirrorError("invalid_config", "Too many inboxes")
    ids = [x["id"] for x in cfg["lists"]]
    if len(set(ids)) != len(ids):
        raise MirrorError("invalid_config", "Duplicate inbox IDs")
    return cfg
