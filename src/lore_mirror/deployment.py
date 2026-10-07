"""Read-only deployment probes and explicit manifest-to-config selection."""
import datetime as dt
import fcntl
import json
import os
from pathlib import Path


def check_sync(config, now=None):
    root = Path(config["data_dir"])
    state = json.loads((root / "daemon-state.json").read_text())
    pid = state["pid"]
    if type(pid) is not int or pid <= 0:
        raise ValueError("invalid sync PID")
    os.kill(pid, 0)
    with (root / "sync.lock").open("rb") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise ValueError("sync coordinator does not hold the writer lock")
    now = now or dt.datetime.now(dt.timezone.utc)
    if state["status"] == "sleeping":
        if state.get("last_cycle_status") != "ready":
            raise ValueError("last sync cycle failed; inspect sync-state.json")
        deadline = dt.datetime.fromisoformat(state["next_scheduled_at"])
        if now > deadline + dt.timedelta(seconds=90):
            raise ValueError("sync missed its scheduled cycle")
    elif state["status"] == "syncing":
        started = dt.datetime.fromisoformat(state["cycle_started_at"])
        budget = config.get("job_timeout_seconds", 43200) * 4 * max(1, len(config["lists"])) + 180
        if (now - started).total_seconds() > budget:
            raise ValueError("sync cycle exceeded its job budgets")
    else:
        raise ValueError("sync is not running")


def manifest_config(template, manifest):
    """Materialize scope so API and writer read the exact same inbox list."""
    result = json.loads(json.dumps(template))
    known = {row["id"]: row for row in result["lists"]}
    ids = sorted({path.split("/")[1] for path in manifest})
    if not ids or len(ids) > 1000:
        raise ValueError("manifest selection must contain 1 to 1000 inboxes")
    result["lists"] = [known.get(ident, {
        "id": ident,
        "class": "u-boot" if ident.startswith("u-boot") else "unclassified",
        "inbox_path": str(Path(result["data_dir"]) / "inboxes" / ident),
        "newsgroup": "org.kernel.lore." + ident,
        "aliases": [ident],
    }) for ident in ids]
    result["scope_note"] = "Explicit full-manifest snapshot; new namespaces require regenerating this file."
    return result
