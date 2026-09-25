#!/usr/bin/env python3
import base64
import json
import os
import sys
import urllib.error
import urllib.request

WP_URL = os.environ['WP_URL'].rstrip('/')
WP_USERNAME = os.environ['WP_USERNAME']
WP_APP_PASSWORD = os.environ['WP_APP_PASSWORD'].replace(' ', '')
PAGE_ID = 193

auth = base64.b64encode(f'{WP_USERNAME}:{WP_APP_PASSWORD}'.encode()).decode()

REPLACEMENTS = [
    ('29fb6269', ['title'], 'About', 'Giorgia Mangiafesta'),
    ('a430125', ['icon_list', 2, 'text'], 'About', 'Giorgia Mangiafesta'),
    ('625e4fd6', ['editor'], '<p>It has stood the test of time and proceeds Elevate your brand with the Agencyo Agency – everything from strategy to advertising &amp; scale.</p>', '<p>Ho iniziato a recitare a quattordici anni al Teatro dei Satiri a Roma. Dopo la laurea in Psicologia, nel 1998 mi sono trasferita a New York, dove ho studiato per cinque anni al Lee Strasberg Theatre Institute. Tornata in Italia nel 2004, ho proseguito il mio percorso teatrale e dal 2009 insegno recitazione. Nel 2015 ho fondato Acting Lab, un laboratorio dedicato alla formazione dell’attore e alla ricerca artistica.</p>'),
    ('6d05307b', ['title'], ' art direction', ' corpo'),
    ('43c2b0db', ['title'], ' motion graphics', ' rilassamento'),
    ('4721153c', ['title'], ' branding', ' voce'),
    ('41eaa72b', ['title'], ' product design', ' lavoro sensoriale'),
    ('4c93adcd', ['title'], ' development', ' ascolto'),
    ('12f79efd', ['title'], ' digital marketing', ' creatività'),
    ('709d7576', ['title'], ' strategy', ' improvvisazione'),
    ('4f875078', ['description_text'], 'Make your business prosper with our great team of experts. We’ll make your.', 'Il mio lavoro mette al centro presenza, ascolto e libertà espressiva. Utilizzo rilassamento, lavoro corporeo, voce, musica, esercizi sensoriali, visualizzazioni e improvvisazione per aiutare l’attore a vivere davvero ciò che accade in scena.'),
    ('1a1022fd', ['editor'], 'consumers today rely heavily on digital means to research products. we research a brand of bldend engaging with it, according to the meanwhile, 51% of consumers', 'Per me acting significa fare, agire, vivere. Non esistono toni rigidi o intonazioni fissate in anticipo: ciò che conta è il momento presente, l’ascolto profondo del partner e la capacità di lasciarsi toccare da ciò che accade, reagendo in modo autentico.'),
    ('3cc935a1', ['editor'], '<p>We helped to get companies</p>', '<p>Vivere davvero la scena</p>'),
    ('7de97869', ['editor'], '<p>We helped to get companies</p>', '<p>Ascoltare davvero l’altro</p>'),
    ('7310768d', ['title'], 'Work', 'Il mio percorso'),
]


def request(method, route, payload=None):
    url = f'{WP_URL}/wp-json/{route.lstrip("/")}'
    data = None
    headers = {
        'Authorization': f'Basic {auth}',
        'Accept': 'application/json',
        'User-Agent': 'ActingLab-GitHub-Giorgia/1.0',
    }
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json; charset=utf-8'
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode('utf-8')
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'HTTP {exc.code}: {raw}') from exc


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


def get_target(settings, path):
    cursor = settings
    for part in path:
        if not isinstance(cursor, (dict, list)):
            raise KeyError(path)
        cursor = cursor[part]
    return cursor


def set_target(settings, path, value):
    cursor = settings
    for part in path[:-1]:
        cursor = cursor[part]
    cursor[path[-1]] = value


page = request('GET', f'wp/v2/pages/{PAGE_ID}?context=edit&_fields=id,title,slug,status,meta')
raw = (page.get('meta') or {}).get('_elementor_data')
if not isinstance(raw, str) or not raw:
    raise SystemExit('Elementor data missing.')

data = json.loads(raw)

# Transactional preflight: nothing is written unless every target still matches exactly.
for widget_id, path, old_text, new_text in REPLACEMENTS:
    element = find_element(data, widget_id)
    if element is None:
        raise SystemExit(f'Widget not found: {widget_id}')
    settings = element.get('settings')
    if not isinstance(settings, dict):
        raise SystemExit(f'No settings for widget: {widget_id}')
    current = get_target(settings, path)
    if current != old_text:
        raise SystemExit(f'Preflight mismatch {widget_id} {path}: {current!r}')

for widget_id, path, old_text, new_text in REPLACEMENTS:
    element = find_element(data, widget_id)
    set_target(element['settings'], path, new_text)

new_raw = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
request('POST', f'wp/v2/pages/{PAGE_ID}', {'meta': {'_elementor_data': new_raw}})

# Verify persisted values from WordPress.
verify_page = request('GET', f'wp/v2/pages/{PAGE_ID}?context=edit&_fields=id,slug,status,modified_gmt,meta')
verify_raw = (verify_page.get('meta') or {}).get('_elementor_data')
verify_data = json.loads(verify_raw)
for widget_id, path, old_text, new_text in REPLACEMENTS:
    element = find_element(verify_data, widget_id)
    current = get_target(element['settings'], path)
    if current != new_text:
        raise SystemExit(f'Verification failed {widget_id} {path}: {current!r}')

print(json.dumps({
    'ok': True,
    'page_id': PAGE_ID,
    'slug': verify_page.get('slug'),
    'status': verify_page.get('status'),
    'modified_gmt': verify_page.get('modified_gmt'),
    'replacements': len(REPLACEMENTS),
}, ensure_ascii=False))
