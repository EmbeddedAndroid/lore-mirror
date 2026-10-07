#!/usr/bin/env python3
"""Inventory advertised Git packs serially, without cloning any mail."""
import argparse
import json
from pathlib import Path

from lore_mirror.sync import _request, pack_budget, validate_manifest

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--manifest', type=Path)
args = parser.parse_args()
config = {'upstream': 'https://erol.kernel.org', 'user_agent': 'lore-mirror/0.1 capacity inventory (serial)'}
raw = args.manifest.read_bytes() if args.manifest else _request(
    config['upstream'] + '/manifest.js.gz', config['user_agent'])[0]
manifest = validate_manifest(raw)
budget = pack_budget(config, sorted(manifest))
budget['inboxes'] = len({k.split('/')[1] for k in manifest})
print(json.dumps(budget, indent=2))
