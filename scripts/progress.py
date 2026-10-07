#!/usr/bin/env python3
"""Summarize durable import checkpoints without scanning the mail tree."""
import json
from pathlib import Path
import shutil

root = Path('/data')
state = json.loads((root / 'sync-state.json').read_text())
config = json.loads(Path('/config/pilot.json').read_text())
rows = state.get('lists', {})
ready = {k: v for k, v in rows.items() if v.get('status') == 'ready'}
print(json.dumps({
    'selected': len(config['lists']), 'manifest_epochs': state.get('manifest', {}).get('entries'),
    'completed': len(ready), 'generation': state.get('generation'),
    'cycle_started_at': state.get('cycle_started_at'), 'status': state.get('status'),
    'unsafe_index': state.get('unsafe_index'),
    'active_or_failed': {k: {'status': v.get('status'), 'error': v.get('last_error'),
                             'started': v.get('last_attempt_at')} for k, v in rows.items() if k not in ready},
    'messages_memberships': sum(v.get('measurements', {}).get('messages') or 0 for v in ready.values()),
    'completed_measurements': {k: v.get('measurements') for k, v in ready.items()},
    'free_bytes': shutil.disk_usage(root).free,
    'cgroup': {p.name: p.read_text().strip() for name in
               ('memory.current', 'memory.peak', 'memory.swap.current', 'memory.events')
               if (p := Path('/sys/fs/cgroup') / name).exists()},
}, indent=2))
