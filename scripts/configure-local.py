#!/usr/bin/env python3
"""Write a relocatable native-host configuration, without fetching or starting work."""
import argparse
import json
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=root / "config/local.json")
    parser.add_argument("--data-dir", type=Path, default=root / "data")
    parser.add_argument("--runtime-dir", type=Path, default=root / "runtime")
    parser.add_argument("--issue-dir", type=Path, default=root / "issues")
    args = parser.parse_args()
    cfg = json.loads((root / "config/pilot.json").read_text())
    data = args.data_dir.expanduser().resolve()
    cfg.update(data_dir=str(data), runtime_dir=str(args.runtime_dir.expanduser().resolve()),
               issue_dir=str(args.issue_dir.expanduser().resolve()), extindex=str(data / "extindex"),
               public_inbox_config=str(data / "public-inbox-config"),
               query_helper=str(root / "scripts/public-inbox-query.pl"), backend="public-inbox")
    for item in cfg["lists"]:
        item["inbox_path"] = str(data / "inboxes" / item["id"])
    for catalog in cfg.get("maintainers_catalogs", {}).values():
        catalog["path"] = str(root / catalog["path"].removeprefix("/app/"))
    output = args.output.expanduser().resolve()
    try:
        with output.open("x") as stream:
            json.dump(cfg, stream, indent=2)
            stream.write("\n")
    except FileExistsError:
        parser.error(f"Refusing to overwrite existing configuration: {output}")
    except OSError as error:
        parser.error(f"Cannot create configuration: {error}")
    print(f"Created {output}; data will use {data}. No mail fetched or service started.")


if __name__ == "__main__":
    main()
