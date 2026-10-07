#!/usr/bin/env python3
"""Probe the local API or the sync process and its durable schedule."""
import json
from pathlib import Path
import sys
import urllib.request

from lore_mirror.deployment import check_sync


def main():
    try:
        if sys.argv[1] == "sync":
            check_sync(json.loads(Path(sys.argv[2]).read_text()))
        elif sys.argv[1] == "api":
            with urllib.request.urlopen("http://127.0.0.1:8765/api/health", timeout=3) as response:
                status = json.load(response)
            if not status.get("backend", {}).get("index_exists"):
                raise ValueError("mail index is not initialized")
        else:
            raise ValueError("expected api or sync")
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
