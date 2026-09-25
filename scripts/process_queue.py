#!/usr/bin/env python3
import base64
import json
import mimetypes
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


def decode_json(raw: str):
    if not raw or not raw.strip():
        return {}
    return json.loads(raw)


def request(method: str, route: str, payload=None):
    url = f'{WP_URL}/wp-json/{route.lstrip("/")}'
    data = None
    headers = {
        'Authorization': f'Basic {auth}',
        'Accept': 'application/json',
        'User-Agent': 'ActingLab-GitHub-Bridge/0.2.0',
    }
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json; charset=utf-8'
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode('utf-8')
            return resp.status, decode_json(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode('utf-8', errors='replace')
        try:
            body = decode_json(raw)
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


def get_page_elementor_data(page_id: int):
    page = request('GET', f'wp/v2/pages/{page_id}?context=edit&_fields=id,title,slug,status,template,meta')[1]
    meta = page.get('meta') or {}
    raw = meta.get('_elementor_data')
    if not isinstance(raw, str) or not raw:
        raise ValueError('Page has no Elementor data in REST meta')
    return page, json.loads(raw)


def collect_images(elements, out):
    for element in elements:
        if not isinstance(element, dict):
            continue
        if element.get('elType') == 'widget' and element.get('widgetType') == 'image':
            image = (element.get('settings') or {}).get('image') or {}
            out.append({
                'widget_id': str(element.get('id', '')),
                'image_id': image.get('id'),
                'url': image.get('url'),
                'alt': image.get('alt'),
            })
        children = element.get('elements')
        if isinstance(children, list):
            collect_images(children, out)


def find_element(elements, widget_id):
    for element in elements:
        if not isinstance(element, dict):
            continue
        if str(element.get('id', '')) == widget_id:
            return element
        children = element.get('elements')
        if isinstance(children, list):
            found = find_element(children, widget_id)
            if found is not None:
                return found
    return None


def upload_media(url: str, filename: str, title: str = '', alt_text: str = ''):
    if not url.startswith('https://raw.githubusercontent.com/infoalessandromacci-rgb/acting-lab/'):
        raise ValueError('Media source must be a raw URL from the acting-lab GitHub repository')
    with urllib.request.urlopen(url, timeout=60) as src:
        blob = src.read()
    mime = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
    endpoint = f'{WP_URL}/wp-json/wp/v2/media'
    headers = {
        'Authorization': f'Basic {auth}',
        'Accept': 'application/json',
        'Content-Type': mime,
        'Content-Disposition': f'attachment; filename="{filename}"',
        'User-Agent': 'ActingLab-GitHub-Bridge/0.2.0',
    }
    req = urllib.request.Request(endpoint, data=blob, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            item = decode_json(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'WordPress media upload HTTP {exc.code}: {raw}') from exc
    if title or alt_text:
        update = {}
        if title:
            update['title'] = title
        if alt_text:
            update['alt_text'] = alt_text
        item = request('POST', f'wp/v2/media/{int(item["id"])}', update)[1]
    return {
        'id': item.get('id'),
        'url': item.get('source_url'),
        'title': (item.get('title') or {}).get('rendered') if isinstance(item.get('title'), dict) else item.get('title'),
        'alt_text': item.get('alt_text'),
    }


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

    if action == 'list_elementor_templates':
        items = request('GET', 'wp/v2/elementor_library?context=edit&per_page=100')[1]
        return [{
            'id': x.get('id'),
            'title': (x.get('title') or {}).get('raw') or (x.get('title') or {}).get('rendered'),
            'slug': x.get('slug'),
            'status': x.get('status'),
            'template': x.get('template'),
        } for x in items]

    if action == 'clone_elementor_template':
        template_id = int(command['template_id'])
        source = request('GET', f'wp/v2/elementor_library/{template_id}?context=edit')[1]
        meta = source.get('meta') or {}
        data = meta.get('_elementor_data')
        if not isinstance(data, str) or not data:
            raise ValueError('Template has no Elementor data')
        payload = {
            'title': command['title'],
            'slug': command.get('slug', ''),
            'status': command.get('status', 'draft'),
            'template': command.get('page_template') or source.get('template') or 'elementor_header_footer',
            'meta': {
                '_elementor_edit_mode': 'builder',
                '_elementor_template_type': 'wp-page',
                '_elementor_data': data,
            },
        }
        created = request('POST', 'wp/v2/pages', payload)[1]
        return {
            'id': created.get('id'),
            'title': (created.get('title') or {}).get('raw') or (created.get('title') or {}).get('rendered'),
            'slug': created.get('slug'),
            'status': created.get('status'),
            'link': created.get('link'),
            'template_id': template_id,
        }

    if action == 'read_elementor_images':
        page_id = int(command['page_id'])
        page, data = get_page_elementor_data(page_id)
        images = []
        collect_images(data, images)
        return {'page_id': page_id, 'count': len(images), 'images': images}

    if action == 'upload_media_from_github':
        return upload_media(
            str(command['url']),
            str(command['filename']),
            str(command.get('title', '')),
            str(command.get('alt_text', '')),
        )

    if action == 'replace_elementor_image':
        page_id = int(command['page_id'])
        widget_id = str(command['widget_id'])
        image_id = int(command['image_id'])
        image_url = str(command['image_url'])
        alt = str(command.get('alt', ''))
        page, data = get_page_elementor_data(page_id)
        element = find_element(data, widget_id)
        if element is None or element.get('widgetType') != 'image':
            raise ValueError('Image widget not found')
        settings = element.setdefault('settings', {})
        old = settings.get('image') or {}
        settings['image'] = {
            'id': image_id,
            'url': image_url,
            'alt': alt,
            'source': 'library',
            'size': old.get('size', ''),
        }
        raw = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
        updated = request('POST', f'wp/v2/pages/{page_id}', {'meta': {'_elementor_data': raw}})[1]
        return {
            'page_id': page_id,
            'widget_id': widget_id,
            'old_image': old,
            'new_image': settings['image'],
            'modified_gmt': updated.get('modified_gmt'),
        }

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
