#!/usr/bin/env python3
"""Summarize durable import checkpoints without scanning the mail tree."""
import contextlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys

from lore_mirror.service import import_activity

# Container paths by default; tests pass a data directory and config file.
root = Path(sys.argv[1] if len(sys.argv) > 1 else '/data')
state = json.loads((root / 'sync-state.json').read_text())
config = json.loads(Path(sys.argv[2] if len(sys.argv) > 2 else '/config/pilot.json').read_text())
rows = state.get('lists', {})
importing = state.get('importing')
if importing:
    # Live count between slice samples; the sampled figures carry the rate.
    item = next((x for x in config['lists'] if x['id'] == importing.get('inbox')), {})
    newsgroup = item.get('newsgroup', 'org.kernel.lore.' + str(importing.get('inbox')))
    over = Path(config.get('extindex', root / 'extindex')) / 'ei15' / 'over.sqlite3'
    try:
        with contextlib.closing(sqlite3.connect(f'file:{over}?mode=ro', uri=True, timeout=60)) as conn:
            importing['live_done'] = conn.execute(
                'SELECT COUNT(*) FROM xref3 WHERE ibx_id=(SELECT ibx_id FROM inboxes WHERE eidx_key=?)',
                (newsgroup,)).fetchone()[0]
    except sqlite3.Error as exc:
        importing['live_done_error'] = str(exc)
ready = {k: v for k, v in rows.items() if v.get('status') == 'ready'}
print(json.dumps({
    'selected': len(config['lists']), 'manifest_epochs': state.get('manifest', {}).get('entries'),
    'completed': len(ready), 'generation': state.get('generation'),
    'cycle_started_at': state.get('cycle_started_at'), 'status': state.get('status'),
    'unsafe_index': state.get('unsafe_index'),
    'importing': importing, 'activity': import_activity(importing) if importing else None,
    'last_error': state.get('last_error'), 'last_error_at': state.get('last_error_at'),
    'error_history': state.get('error_history', [])[-5:],
    'active_or_failed': {k: {'status': v.get('status'), 'error': v.get('last_error'),
                             'started': v.get('last_attempt_at')} for k, v in rows.items() if k not in ready},
    'messages_memberships': sum(v.get('measurements', {}).get('messages') or 0 for v in ready.values()),
    'completed_measurements': {k: v.get('measurements') for k, v in ready.items()},
    'free_bytes': shutil.disk_usage(root).free,
    'cgroup': {p.name: p.read_text().strip() for name in
               ('memory.current', 'memory.peak', 'memory.swap.current', 'memory.events')
               if (p := Path('/sys/fs/cgroup') / name).exists()},
}, indent=2))
