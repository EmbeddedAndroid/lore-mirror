#!/usr/bin/env python3
"""Exercise the two-small-inbox deployment through a real Streamable HTTP client."""
import argparse
import asyncio
import base64
import hashlib
import json
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

ROOT = Path(__file__).resolve().parents[1]


async def smoke(url):
    fixture = json.loads((ROOT / 'tests/fixtures/real-pilot/manifest.json').read_text())['messages'][1]
    mid = fixture['message_id']
    results = []
    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as client:
            await client.initialize()
            tools = (await client.list_tools()).tools
            expected = {'mail.search', 'mail.get_message', 'mail.get_thread', 'mail.get_series',
                        'mail.in_flight', 'mail.was_posted', 'mail.list_lists', 'mail.status',
                        'issue.report', 'issue.get', 'issue.list'}
            assert {tool.name for tool in tools} == expected
            assert all(tool.annotations.openWorldHint is False for tool in tools)
            assert all(tool.annotations.readOnlyHint == (tool.name != 'issue.report') for tool in tools)
            results.append({'operation': 'initialize/tools.list', 'tools': len(tools)})

            async def call(name, arguments=None, *, error=False):
                response = await client.call_tool(name, arguments or {})
                assert response.isError == error, (name, response)
                if error:
                    results.append({'operation': name, 'expected_error': True})
                    return None
                result = response.structuredContent
                if result is None:
                    result = json.loads(response.content[0].text)
                assert isinstance(result, dict), (name, result)
                assert 'error' not in result, (name, result)
                results.append({'operation': name, 'items': len(result.get('items', []))})
                return result

            status = await call('mail.status')
            assert status['backend']['native_query_syntax']
            assert status['backend']['index_exists']
            assert status['freshness']['stale'] is False
            lists = await call('mail.list_lists')
            assert {row['id'] for row in lists['items']} == {'devicetree-spec', 'u-boot-amlogic'}
            assert all(row['sync']['status'] == 'ready' and row['sync']['message_count'] > 0
                       for row in lists['items'])
            search = await call('mail.search', {'query': 'mid:' + mid, 'limit': 5})
            assert any(row['message_id'] == mid for row in search['items'])
            arguments = {'message_id': mid, 'part': 'raw', 'limit': 1024}
            octets = bytearray()
            while True:
                raw = await call('mail.get_message', arguments)
                assert not raw['partial']
                item = raw['items'][0]
                assert item['byte_offset'] == len(octets)
                octets.extend(base64.b64decode(item['raw_base64'], validate=True))
                if not raw['has_more']:
                    break
                arguments['cursor'] = raw['next_cursor']
            assert hashlib.sha256(octets).hexdigest() == fixture['sha256']
            thread = await call('mail.get_thread', {'message_id': mid, 'limit': 10})
            assert any(row['message_id'] == mid for row in thread['items'])
            series = await call('mail.get_series', {'message_id': mid, 'limit': 10})
            assert series['items']
            flight = await call('mail.in_flight', {'paths': ['drivers/net/designware.c'],
                'lists': ['u-boot-amlogic'], 'since': '2026-10-05', 'until': '2026-10-06', 'limit': 10})
            assert flight['items']
            posted = await call('mail.was_posted', {'message_id': mid})
            assert posted['items']
            report_args = {'title': 'Docker MCP verification',
                'description': 'Local demo verification of the HTTP issue store.',
                'idempotency_key': 'docker-mcp-verification-v1', 'message_ids': [mid]}
            issue = await call('issue.report', report_args)
            replay = await call('issue.report', report_args)
            assert issue['id'] == replay['id'] and replay['idempotent_replay']
            stored = await call('issue.get', {'issue_id': issue['id']})
            assert stored['description'] == report_args['description']
            issue_list = await call('issue.list')
            assert any(row['id'] == issue['id'] for row in issue_list['items'])
            empty = await call('mail.search', {'query': 'mid:absent-docker-probe@invalid.example'})
            assert empty['items'] == []
            await call('mail.get_message', {'message_id': 'absent-docker-probe@invalid.example'}, error=True)
            await call('mail.search', {'query': '', 'limit': 0}, error=True)
            await call('issue.report', report_args | {'description': 'Conflicting replay'}, error=True)
            await call('mail.fetch', {'url': 'https://example.invalid/'}, error=True)
    print(json.dumps({'url': url, 'message_id': mid, 'issue_id': issue['id'],
                      'operations': results, 'passed': True}, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8765/mcp')
    asyncio.run(smoke(parser.parse_args().url))
