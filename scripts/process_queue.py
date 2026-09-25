#!/usr/bin/env python3
import base64
import json
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
PENDING = ROOT / 'queue' / 'pending'
PROCESSED = ROOT / 'queue' / 'processed'
FAILED = ROOT / 'queue' / 'failed'
RESULTS = ROOT / 'queue' / 'results'

for d in (PENDING, PROCESSED, FAILED, RESULTS):
    d.mkdir(parents=True, exist_ok=True)

WP_URL = os.environ.get('WP_URL', '').rstrip('/')
WP_USERNAME = os.environ.get('WP_USERNAME', '')
WP_APP_PASSWORD = os.environ.get('WP_APP_PASSWORD', '').replace(' ', '')

if not (WP_URL and WP_USERNAME and WP_APP_PASSWORD):
    print('Missing one or more required secrets: WP_URL, WP_USERNAME, WP_APP_PASSWORD', file=sys.stderr)
    sys.exit(2)

auth = base64.b64encode(f'{WP_USERNAME}:{WP_APP_PASSWORD}'.encode()).decode()


def request(method: str, route: str, payload=None):
    url = f'{WP_URL}/wp-json/{route.lstrip("/")}'
    data = None
    headers = {
        'Authorization': f'Basic {auth}',
        'Accept': 'application/json',
        'User-Agent': 'ActingLab-GitHub-Bridge/0.1.0',
    }
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json; charset=utf-8'
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode('utf-8')
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode('utf-8', errors='replace')
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {'raw': raw}
        raise RuntimeError(f'WordPress HTTP {exc.code}: {json.dumps(body, ensure_ascii=False)}') from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f'Could not reach WordPress: {exc.reason}') from exc


def is_content_field(field: dict) -> bool:
    if field.get('el_type') != 'widget':
        return False
    path = field.get('path') or []
    if not path:
        return False
    leaf = str(path[-1]).lower()
    content_markers = (
        'title', 'text', 'content', 'label', 'placeholder', 'message',
        'caption', 'description', 'button', 'heading', 'subheading'
    )
    return any(marker in leaf for marker in content_markers)


def replace_text(page_id: int, item: dict, default_dry_run=False):
    payload = {
        'widget_id': item['widget_id'],
        'path': item['path'],
        'old_text': item['old_text'],
        'new_text': item.get('new_text', ''),
        'replace_all': bool(item.get('replace_all', False)),
        'dry_run': bool(item.get('dry_run', default_dry_run)),
    }
    return request('POST', f'acting-lab/v1/pages/{page_id}/replace-text', payload)[1]


def process(command: dict):
    action = command.get('action')
    if action == 'status':
        return request('GET', 'acting-lab/v1/status')[1]

    if action == 'list_pages':
        params = {}
        if command.get('search'):
            params['search'] = command['search']
        if command.get('per_page'):
            params['per_page'] = command['per_page']
        qs = urllib.parse.urlencode(params)
        route = 'acting-lab/v1/pages' + (f'?{qs}' if qs else '')
        return request('GET', route)[1]

    if action == 'read_page':
        page_id = int(command['page_id'])
        return request('GET', f'acting-lab/v1/pages/{page_id}')[1]

    if action == 'read_widget_texts':
        page_id = int(command['page_id'])
        page = request('GET', f'acting-lab/v1/pages/{page_id}')[1]
        fields = [f for f in page.get('text_fields', []) if is_content_field(f)]
        return {
            'id': page.get('id'),
            'title': page.get('title'),
            'url': page.get('url'),
            'count': len(fields),
            'text_fields': fields,
        }

    if action == 'replace_text':
        page_id = int(command['page_id'])
        return replace_text(page_id, command)

    if action == 'replace_text_batch':
        page_id = int(command['page_id'])
        dry_run = bool(command.get('dry_run', False))
        results = []
        for index, item in enumerate(command.get('replacements', []), start=1):
            try:
                result = replace_text(page_id, item, default_dry_run=dry_run)
                results.append({'index': index, 'ok': True, 'result': result})
            except Exception as exc:
                results.append({'index': index, 'ok': False, 'error': str(exc)})
        return {
            'page_id': page_id,
            'dry_run': dry_run,
            'total': len(results),
            'successful': sum(1 for x in results if x['ok']),
            'failed': sum(1 for x in results if not x['ok']),
            'results': results,
        }

    if action == 'wp_rest':
        method = str(command.get('method', 'GET')).upper()
        route = str(command.get('route', '')).lstrip('/')
        if method not in {'GET', 'POST', 'PUT'}:
            raise ValueError('wp_rest only allows GET, POST or PUT')
        if not route.startswith('wp/v2/'):
            raise ValueError('wp_rest route must start with wp/v2/')
        payload = command.get('payload') if method in {'POST', 'PUT'} else None
        return request(method, route, payload)[1]

    raise ValueError(f'Unsupported action: {action!r}')


files = sorted(p for p in PENDING.glob('*.json') if p.is_file())
if not files:
    print('No pending commands.')
    sys.exit(0)

failed_any = False
for path in files:
    stamp = datetime.now(timezone.utc).isoformat()
    command = None
    try:
        command = json.loads(path.read_text(encoding='utf-8'))
        result = process(command)
        envelope = {
            'ok': True,
            'processed_at': stamp,
            'source': path.name,
            'command': command,
            'result': result,
        }
        (RESULTS / f'{path.stem}.result.json').write_text(
            json.dumps(envelope, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
        )
        shutil.move(str(path), str(PROCESSED / path.name))
        print(f'OK {path.name}')
    except Exception as exc:
        failed_any = True
        envelope = {
            'ok': False,
            'processed_at': stamp,
            'source': path.name,
            'command': command,
            'error': str(exc),
        }
        (RESULTS / f'{path.stem}.result.json').write_text(
            json.dumps(envelope, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
        )
        shutil.move(str(path), str(FAILED / path.name))
        print(f'FAIL {path.name}: {exc}', file=sys.stderr)

sys.exit(1 if failed_any else 0)
