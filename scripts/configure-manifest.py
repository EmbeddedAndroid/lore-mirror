#!/usr/bin/env python3
"""Generate an explicit full-manifest config without cloning or indexing mail."""
import argparse
import json
from pathlib import Path

from lore_mirror.deployment import manifest_config
from lore_mirror.sync import _request, validate_manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full-manifest", action="store_true", required=True)
    parser.add_argument("--template", type=Path, default=Path("/app/config/pilot.json"))
    parser.add_argument("--manifest", type=Path, help="Use a saved gzip manifest instead of fetching one")
    args = parser.parse_args()
    template = json.loads(args.template.read_text())
    raw = args.manifest.read_bytes() if args.manifest else _request(
        template["manifest_url"], template["user_agent"])[0]
    print(json.dumps(manifest_config(template, validate_manifest(raw)), indent=2))


if __name__ == "__main__":
    main()
