"""agentmap milestone 2.

Passive CDP capture (Page, DOM, DOMSnapshot, Accessibility, Emulation only; never Runtime, no script injection)
-> unified nodes -> prune -> sections -> labelled regions -> stable IDs -> budgeted, foldable outline.

New in M2
  (a) unnamed lists/regions are labelled from the nearest preceding heading/text (sibling first, then geometry)
  (b) budget folding: regions start folded and are unfolded breadth-first until the map hits --budget tokens;
      long paragraphs fold to their first words
  (c) expand(region_id): render one region in full (all collapsed items, full text), with its own budget
  (d) crop(id): PNG cut from the full-page screenshot using DOMSnapshot boxes x device pixel ratio
  (e) diff(a, b): change report between two snapshots, keyed by stable ID

CLI
  python3 agentmap.py map <url> <name> [--budget 3000] [--dpr 1]
  python3 agentmap.py expand <name> <region_id> [--budget 3000]
  python3 agentmap.py crop <name> <id> [out.png]
  python3 agentmap.py diff <nameA> <nameB>
  python3 agentmap.py mapfile <html_path> <name>      (capture a local file, used for fixtures)
Needs: chromium, websockets, tiktoken, pillow. Export NO_PROXY=127.0.0.1,localhost.
"""
import asyncio, base64, gzip, json, os, re, subprocess, sys, tempfile, time, urllib.request, random
import websockets

INTERACTIVE = {'link', 'button', 'textbox', 'searchbox', 'combobox', 'checkbox', 'radio', 'menuitem', 'tab',
               'option', 'switch', 'slider', 'spinbutton', 'listbox', 'menuitemcheckbox', 'menuitemradio', 'treeitem'}
LANDMARK = {'banner': 'Header', 'navigation': 'Nav', 'main': 'Main', 'contentinfo': 'Footer', 'complementary': 'Sidebar',
            'form': 'Form', 'search': 'Search', 'dialog': 'Dialog', 'alertdialog': 'Dialog', 'region': 'Region'}
CONTAINER = {'list', 'table', 'grid', 'rowgroup', 'listbox', 'menu', 'menubar', 'tablist', 'tree'}
LISTY = {'list', 'table', 'grid', 'menu', 'menubar', 'tablist', 'listbox', 'tree'}
REGION_ROLES = set(LANDMARK) | LISTY | {'section'}
ABBR = {'link': 'lnk', 'button': 'btn', 'textbox': 'in', 'searchbox': 'in', 'combobox': 'sel', 'checkbox': 'chk',
        'radio': 'rad', 'menuitem': 'mi', 'tab': 'tab', 'option': 'opt', 'switch': 'sw', 'slider': 'sld', 'spinbutton': 'num',
        'listbox': 'sel', 'treeitem': 'ti', 'menuitemcheckbox': 'mi', 'menuitemradio': 'mi'}
FOLD_WORDS, LEAD_WORDS = 50, 25          # paragraphs longer than FOLD_WORDS fold to their first LEAD_WORDS
SHOT_MAX_PX = 16000                      # Chrome texture limit for one full-page screenshot

_enc = None
def tok(s):
    global _enc
    if _enc is None:
        import tiktoken
        _enc = tiktoken.get_encoding('cl100k_base')
    return len(_enc.encode(s, disallowed_special=()))


# ======================= capture =======================
class CDP:
    def __init__(self, ws):
        self.ws, self.i, self.pending, self.events = ws, 0, {}, []

    async def reader(self):
        async for m in self.ws:
            d = json.loads(m)
            if 'id' in d and d['id'] in self.pending:
                self.pending.pop(d['id']).set_result(d)
            else:
                self.events.append(d)

    async def send(self, method, **params):
        self.i += 1
        f = asyncio.get_event_loop().create_future()
        self.pending[self.i] = f
        await self.ws.send(json.dumps({'id': self.i, 'method': method, 'params': params}))
        r = await asyncio.wait_for(f, 90)
        if 'error' in r:
            raise RuntimeError(f"{method}: {r['error']}")
        return r['result']


def launch(dpr=1):
    port = random.randint(9400, 9900)
    prof = tempfile.mkdtemp()
    p = subprocess.Popen(['chromium', '--headless=new', f'--remote-debugging-port={port}', f'--user-data-dir={prof}',
                          '--no-sandbox', '--no-proxy-server', '--disable-gpu', '--window-size=1366,900', '--lang=en-IN',
                          f'--force-device-scale-factor={dpr}', '--hide-scrollbars',
                          '--disable-blink-features=AutomationControlled', 'about:blank'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(60):
        try:
            tabs = json.load(opener.open(f'http://127.0.0.1:{port}/json'))
            return p, [t for t in tabs if t['type'] == 'page'][0]['webSocketDebuggerUrl']
        except Exception:
            time.sleep(0.25)
    raise RuntimeError('chrome did not start')


async def capture(url, settle=3.0, shot_path=None, dpr=1):
    proc, wsurl = launch(dpr)
    try:
        async with websockets.connect(wsurl, max_size=None) as ws:
            c = CDP(ws)
            rt = asyncio.create_task(c.reader())
            await c.send('Page.enable')
            t0 = time.time()
            await c.send('Page.navigate', url=url)
            for _ in range(250):
                if any(e.get('method') == 'Page.loadEventFired' for e in c.events):
                    break
                await asyncio.sleep(0.1)
            await asyncio.sleep(settle)
            load_s = time.time() - t0
            t1 = time.time()
            # Freeze one layout for everything: grow the viewport to the page height (capped) BEFORE reading boxes,
            # so the screenshot needs no captureBeyondViewport relayout and boxes and pixels agree.
            lm = await c.send('Page.getLayoutMetrics')
            cw, ch = lm['cssContentSize']['width'], lm['cssContentSize']['height']
            vh = int(min(ch, SHOT_MAX_PX / dpr)) if shot_path else 900
            if shot_path:
                await c.send('Emulation.setDeviceMetricsOverride', width=1366, height=vh, deviceScaleFactor=dpr, mobile=False)
                await asyncio.sleep(1.0)
            doc = await c.send('DOM.getDocument', depth=-1, pierce=True)
            html = (await c.send('DOM.getOuterHTML', nodeId=doc['root']['nodeId']))['outerHTML']
            ax = (await c.send('Accessibility.getFullAXTree'))['nodes']
            snap = await c.send('DOMSnapshot.captureSnapshot', computedStyles=[], includeDOMRects=True)
            hist = await c.send('Page.getNavigationHistory')
            lm = await c.send('Page.getLayoutMetrics')
            cw, ch = lm['cssContentSize']['width'], lm['cssContentSize']['height']
            # DOMSnapshot bounds are device px when DSF != 1 (zoom-for-DSF); measure the unit instead of assuming
            snap_scale = round(snap['documents'][0].get('contentWidth', cw) / cw, 4) if cw else 1.0
            shot = None
            if shot_path:
                h = min(ch, vh)
                data = (await c.send('Page.captureScreenshot', format='png',
                                     clip=dict(x=0, y=0, width=min(cw, 1366), height=h, scale=1)))['data']
                open(shot_path, 'wb').write(base64.b64decode(data))
                from PIL import Image
                im = Image.open(shot_path)
                # true DPR = image pixels / CSS pixels of the clip
                shot = dict(path=shot_path, css_w=min(cw, 1366), css_h=h, px_w=im.width, px_h=im.height,
                            dpr=round(im.width / min(cw, 1366), 4), page_css_h=ch)
            snap['_scale'] = snap_scale
            cap_s = time.time() - t1
            cur = hist['entries'][hist['currentIndex']]
            rt.cancel()
            return dict(url=cur['url'], title=cur['title'], html=html, ax=ax, snap=snap, shot=shot,
                        load_s=load_s, cap_s=cap_s, at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    finally:
        proc.kill()


def save_cap(cap, name):
    keep = {k: v for k, v in cap.items() if k != 'html'}
    with gzip.open(f'out/{name}.cap.json.gz', 'wt') as f:
        json.dump(keep, f)


def load_cap(name):
    with gzip.open(f'out/{name}.cap.json.gz', 'rt') as f:
        return json.load(f)


def boxes_from_snapshot(snap):
    """backendNodeId -> [x, y, w, h] in CSS px, document coordinates"""
    out, k = {}, snap.get('_scale', 1.0) or 1.0
    for d in snap['documents']:
        be = d['nodes']['backendNodeId']
        for ni, b in zip(d['layout']['nodeIndex'], d['layout']['bounds']):
            out.setdefault(be[ni], [v / k for v in b])
    return out


# ======================= normalize + prune =======================
def normalize(ax, boxes):
    nodes = {}
    for n in ax:
        role = (n.get('role') or {}).get('value', 'none')
        name = ((n.get('name') or {}).get('value') or '').strip()
        val = (n.get('value') or {}).get('value')
        props = {p['name']: p['value'].get('value') for p in n.get('properties', [])}
        nodes[n['nodeId']] = dict(
            role=role, name=re.sub(r'\s+', ' ', name), value=val if isinstance(val, (str, int, float)) else None,
            ignored=n.get('ignored', False), children=n.get('childIds', []), box=boxes.get(n.get('backendDOMNodeId')),
            be=n.get('backendDOMNodeId'),
            state=[k for k in ('focused', 'checked', 'selected', 'expanded', 'disabled', 'required', 'invalid')
                   if props.get(k) not in (None, False, 'false')],
            level=props.get('level'))
    return nodes, ax[0]['nodeId']


def visible(n):
    b = n['box']
    return b is None or (b[2] > 0 and b[3] > 0)


KEEP = set(LANDMARK) | CONTAINER | {'heading', 'row', 'listitem', 'cell', 'columnheader', 'rowheader', 'article',
                                    'RootWebArea', 'WebArea', 'Iframe', 'paragraph', 'blockquote'}


def build(nodes, nid, parent_name=''):
    n = nodes.get(nid)
    if not n:
        return []
    kids = []
    for c in n['children']:
        kids += build(nodes, c, n['name'] or parent_name)
    role = n['role']
    if n['ignored'] or not visible(n) and role not in INTERACTIVE:
        return kids
    if role == 'StaticText':
        t = n['name']
        if not t or t == parent_name or len(t) < 2 or not re.search(r'\w', t):
            return []
        return [dict(role='text', name=t, kids=[], box=n['box'])]
    if role in ('image', 'img'):
        return [dict(role='img', name=n['name'], kids=[], box=n['box'])] if n['name'] and len(n['name']) > 2 else []
    if role in INTERACTIVE:
        name = n['name'] or ' '.join(k['name'] for k in kids if k['role'] in ('text', 'img'))[:80] or '(no label)'
        sub = [k for k in kids if k['role'] not in ('text', 'img')]
        return [dict(role=role, name=name, value=n['value'], state=n['state'], kids=sub, box=n['box'], be=n['be'])]
    if role in KEEP:
        if not kids and not n['name']:
            return []
        if role == 'blockquote':
            role = 'paragraph'
        return [dict(role=role, name=n['name'] if role != 'paragraph' else '', kids=kids, box=n['box'], level=n['level'],
                     be=n['be'])]
    return kids


def merge_text(kids):
    out = []
    for k in kids:
        k['kids'] = merge_text(k.get('kids', []))
        if k['role'] == 'text' and out and out[-1]['role'] == 'text':
            out[-1]['name'] = (out[-1]['name'] + ' ' + k['name']).strip()
        else:
            out.append(k)
    return out


# ======================= structure: sections, labels =======================
def sectionize(kids):
    """Group heading-led sibling runs into synthetic `section` nodes (h2 owns until the next h<=2)."""
    for k in kids:
        k['kids'] = sectionize(k.get('kids', []))
    if not any(k['role'] == 'heading' for k in kids):
        return kids
    out, stack = [], []
    for k in kids:
        if k['role'] == 'heading':
            lvl = int(k.get('level') or 2)
            while stack and stack[-1][0] >= lvl:
                stack.pop()
            sec = dict(role='section', name=k['name'], level=lvl, kids=[k], box=k['box'], synthetic=True)
            (stack[-1][1]['kids'] if stack else out).append(sec)
            stack.append((lvl, sec))
        else:
            (stack[-1][1]['kids'] if stack else out).append(k)
    return out


def hoist(tree):
    """<section aria-labelledby=h2> already is the section: drop the synthetic duplicate inside it"""
    for n in tree:
        hoist(n.get('kids', []))
        k = n.get('kids', [])
        if n['role'] in LANDMARK and len(k) == 1 and k[0].get('synthetic') and (not n['name'] or n['name'] == k[0]['name']):
            n['kids'], n['name'], n['level'] = k[0]['kids'], k[0]['name'], k[0]['level']


def flat_text(n, limit=120):
    if n['role'] in ('text', 'heading', 'img') or n['role'] in INTERACTIVE and not n.get('kids'):
        return n.get('name', '')
    s = ''
    for k in n.get('kids', []):
        s = (s + ' ' + flat_text(k, limit)).strip()
        if len(s) > limit:
            break
    return s


def walk(tree, fn, parent=None):
    for n in tree:
        fn(n, parent)
        walk(n.get('kids', []), fn, n)


def label_regions(tree):
    """(a) give unnamed lists/regions a label: preceding sibling text/heading/link (<=40 chars), else the nearest
    short text box sitting directly above it (<=90px, horizontally overlapping)."""
    leaves = []

    def mark(nodes, lm):
        for n in nodes:
            n['_lm'] = lm
            if n['role'] in ('text', 'heading', 'link') and n.get('box') and 0 < len(n.get('name', '')) <= 40:
                leaves.append(n)
            mark(n.get('kids', []), id(n) if n['role'] in LANDMARK else lm)
    mark(tree, None)

    def geo(box, lm):
        x, y, w, h = box
        best = None
        for l in leaves:
            if l['_lm'] != lm:
                continue  # never borrow a label across a landmark boundary
            lx, ly, lw, lh = l['box']
            gap = y - (ly + lh)
            if -2 <= gap <= 90 and lx < x + w and lx + lw > x:
                if best is None or gap < best[0]:
                    best = (gap, l['name'])
        return best[1] if best else ''

    def visit(kids, pname=''):
        for i, k in enumerate(kids):
            visit(k.get('kids', []), k.get('name') if k['role'] in LANDMARK else '')
            if k['role'] in LISTY and not k.get('name'):
                lab = ''
                for j in (i - 1, i - 2):
                    if j < 0:
                        break
                    p = kids[j]
                    t = flat_text(p, 60) if p['role'] in ('text', 'heading', 'link', 'paragraph') else ''
                    if t and len(t) <= 40:
                        lab = t
                        break
                    if p['role'] in REGION_ROLES:
                        break
                if not lab and k.get('box'):
                    lab = geo(k['box'], k['_lm'])
                if not lab and pname and i == 0:
                    lab = pname  # first unnamed list in a named landmark takes the landmark's name
                if lab:
                    k['name'], k['label_src'] = lab, 'inferred'
    visit(tree)


# ======================= stable IDs =======================
def slug(s, n=24):
    s = re.sub(r'[^a-z0-9]+', '-', (s or '').lower()).strip('-')
    return s[:n].rstrip('-') or 'x'


def assign_ids(tree):
    """Regions: r:<label>. Paragraphs: p:<region>.<n>. Controls: <role>:<name>, made unique by the region they
    sit in (lnk:apply-online@latest-jobs) before falling back to a counter. Scoping by region keeps an ID
    stable when a same-named control appears elsewhere on the page."""
    reg_used, ctl = {}, []

    def uniq(base, used):
        k = used.get(base, 0)
        used[base] = k + 1
        return base if k == 0 else f'{base}-{k + 1}'

    para_used = {}

    def visit(n, region, scope):
        r = n['role']
        if r in REGION_ROLES:
            n['id'] = uniq('r:' + slug(n.get('name') or (LANDMARK.get(r) or r).lower()), reg_used)
            region = n
            if not n.get('synthetic'):
                scope = n  # synthetic sections come from headings an ad can inject; do not scope IDs by them
        elif r == 'paragraph':
            # keyed by its opening words, not its position, so inserting a paragraph above does not renumber it
            lead = slug(' '.join(flat_text(n, 80).split()[:5]), 28)
            n['id'] = uniq(f"p:{lead}", para_used)
        elif r in INTERACTIVE:
            n['region'] = scope['id'] if scope else 'r:top'
            ctl.append(n)
        n['region_of'] = region['id'] if region else 'r:top'
        for c in n.get('kids', []):
            visit(c, region, scope)

    for n in tree:
        visit(n, None, None)
    base_cnt = {}
    for n in ctl:
        n['_base'] = f"{ABBR.get(n['role'], n['role'][:3])}:{slug(n['name'])}"
        base_cnt[n['_base']] = base_cnt.get(n['_base'], 0) + 1
    used = {}
    for n in ctl:
        b = n['_base']
        n['id'] = b if base_cnt[b] == 1 else uniq(f"{b}@{n['region'][2:]}", used)


# ======================= stats + signatures =======================
def sig(n, depth=2):
    if depth == 0:
        return n['role']
    return n['role'] + '(' + ','.join(sig(k, depth - 1) for k in n.get('kids', [])[:6]) + ')'


def stats(n):
    if '_st' in n:
        return n['_st']
    s = dict(words=0, links=0, ctl=0, items=0, lists=0, tables=0)
    if n['role'] == 'text':
        s['words'] = len(n['name'].split())
    if n['role'] in INTERACTIVE:
        s['ctl'] = 1
        s['links'] = int(n['role'] == 'link')
        s['words'] = len((n['name'] or '').split())
    for k in n.get('kids', []):
        ks = stats(k)
        for key in ('words', 'links', 'ctl', 'lists', 'tables'):
            s[key] += ks[key]
        if k['role'] in ('list', 'menu', 'tree', 'listbox'):
            s['lists'] += 1
        if k['role'] in ('table', 'grid'):
            s['tables'] += 1
    s['items'] = sum(1 for k in n.get('kids', []) if k['role'] in ('listitem', 'row', 'option', 'menuitem', 'tab', 'treeitem')
                     or k['role'] in INTERACTIVE) or len(n.get('kids', []))
    n['_st'] = s
    return s


def bbox(n):
    if '_bb' in n:
        return n['_bb']
    bs = [n['box']] if n.get('box') and n['box'][2] > 0 else []
    for k in n.get('kids', []):
        b = bbox(k)
        if b:
            bs.append(b)
    if not bs:
        n['_bb'] = None
        return None
    x0 = min(b[0] for b in bs); y0 = min(b[1] for b in bs)
    x1 = max(b[0] + b[2] for b in bs); y1 = max(b[1] + b[3] for b in bs)
    n['_bb'] = [x0, y0, x1 - x0, y1 - y0]
    return n['_bb']


# ======================= render =======================
def short(t, n=90):
    t = t or ''
    return t if len(t) <= n else t[:n - 1] + '…'


def is_foldable(n):
    if n['role'] == 'paragraph':
        return stats(n)['words'] > FOLD_WORDS
    return n['role'] in REGION_ROLES and 'id' in n


def fold_line(n):
    s, r = stats(n), n['role']
    if r == 'paragraph':
        words = flat_text(n, 400).split()
        return f"{' '.join(words[:LEAD_WORDS])}… ▸[{n['id']}] +{max(0, s['words'] - LEAD_WORDS)} words, {s['links']} links"
    bits = []
    if r in LISTY:
        bits.append(f"{s['items']} items")
        heads = [short(flat_text(k, 60), 34) for k in n['kids'][:2] if flat_text(k, 60)]
        if heads:
            bits.append(' | '.join(f'"{h}"' for h in heads))
    else:
        if s['words']:
            bits.append(f"{s['words']} words")
        if s['links']:
            bits.append(f"{s['links']} links")
        if s['ctl'] - s['links'] > 0:
            bits.append(f"{s['ctl'] - s['links']} controls")
        if s['lists']:
            bits.append(f"{s['lists']} lists")
        if s['tables']:
            bits.append(f"{s['tables']} tables")
    kind = {'section': '§', 'region': '§', **{k: '##' for k in LANDMARK if k != 'region'}}.get(r, r)
    label = LANDMARK.get(r, '') if r in LANDMARK and r != 'region' else ''
    nm = n.get('name') or ''
    head = f"{kind} {label + (': ' if label and nm else '')}{short(nm, 50)}".rstrip()
    return f"▸ [{n['id']}] {head} — {', '.join(bits)}"


def ctl_str(n, inline=False):
    if inline:
        return f"[{short(n['name'], 50)}]({n['id']})"
    s = f"[{n['id']}] {n['role']} \"{short(n['name'], 60)}\""
    if n.get('value') not in (None, ''):
        s += f" = \"{short(str(n['value']), 40)}\""
    if n.get('state'):
        s += ' {' + ','.join(n['state']) + '}'
    return s


def head_line(n):
    r = n['role']
    if r in INTERACTIVE:
        return ctl_str(n)
    if r == 'heading':
        return f"H{n.get('level') or ''} {short(n['name'], 80)}"
    if r in LANDMARK:
        return f"## {LANDMARK[r]}" + (f": {short(n['name'], 50)}" if n['name'] else '') + f"  [{n['id']}]"
    if r == 'section':
        return None  # its heading child speaks for it
    if r == 'text':
        return short(n['name'], 160)
    if r == 'img':
        return f"img \"{short(n['name'], 50)}\""
    if r in LISTY:
        q = f" \"{short(n['name'], 40)}\"" if n['name'] else ''
        return f"{r}{q} ({len(n['kids'])})  [{n['id']}]"
    return None


class Renderer:
    """Renders the pruned tree. `open_` = set of node ids that are unfolded. Anything foldable not in open_ prints
    as a one-line summary. `full` = ids whose repeated items are all shown and whose text is not truncated."""

    def __init__(self, open_, full=frozenset(), keep=3, minrun=6):
        self.open, self.full, self.keep, self.minrun = open_, full, keep, minrun

    def paragraph(self, n, full):
        parts = []
        for k in n['kids']:
            if k['role'] in INTERACTIVE:
                parts.append(ctl_str(k, inline=True))
            else:
                t = flat_text(k, 10 ** 6) if k['role'] != 'text' else k['name']
                if t:
                    parts.append(t if full else t)
        return ' '.join(parts)

    def render(self, nodes, indent=0, out=None, full=False):
        out = [] if out is None else out
        nodes = self.compress(nodes, full)
        for n in nodes:
            pad = '  ' * indent
            if n['role'] == 'more':
                out.append(pad + f"… {n['name']}")
                continue
            if is_foldable(n) and n['id'] not in self.open:
                out.append(pad + fold_line(n))
                continue
            if n['role'] == 'paragraph':
                out.append(pad + self.paragraph(n, full))
                continue
            k0 = n.get('kids', [{}])[0] if n.get('kids') else {}
            if n['role'] in ('section', 'region') and k0.get('role') == 'heading' and k0['name'] == n.get('name'):
                out.append(pad + head_line(k0) + f"  [{n['id']}]")
                self.render(n['kids'][1:], indent + int(n['role'] == 'region'), out, full or n.get('id') in self.full)
                continue
            l = head_line(n)
            if l is None:
                if n['role'] in ('row', 'listitem', 'article') and n.get('kids'):
                    flat = []
                    for k in n['kids']:
                        fl = head_line(k)
                        if fl and not is_foldable(k):
                            flat.append(fl)
                        else:
                            flat += [head_line(g) for g in k.get('kids', []) if head_line(g) and not is_foldable(g)]
                    if flat and sum(len(x) for x in flat) < 220 and all(
                            (not k.get('kids') or head_line(k) is None) and not is_foldable(k) for k in n['kids']):
                        out.append(pad + '• ' + ' · '.join(flat))
                        continue
                self.render(n.get('kids', []), indent, out, full or n.get('id') in self.full)
                continue
            if n['role'] == 'text' and not full and len(n['name'].split()) > FOLD_WORDS:
                w = n['name'].split()
                l = ' '.join(w[:LEAD_WORDS]) + f'… (+{len(w) - LEAD_WORDS} words)'
            elif n['role'] == 'text' and full:
                l = n['name']
            out.append(pad + l)
            deeper = n['role'] in LANDMARK or n['role'] in CONTAINER
            self.render(n.get('kids', []), indent + int(deeper), out, full or n.get('id') in self.full)
        return out

    def compress(self, kids, full):
        if full or len(kids) < self.minrun:
            return kids
        sigs = [sig(k) for k in kids]
        top = max(set(sigs), key=sigs.count)
        if sigs.count(top) < self.minrun or top.startswith('text'):
            return kids
        shown, hidden, out = 0, 0, []
        for k, s in zip(kids, sigs):
            if s == top:
                if shown < self.keep:
                    out.append(k); shown += 1
                else:
                    hidden += 1
            else:
                out.append(k)
        out.append(dict(role='more', name=f'+{hidden} more similar {top.split("(")[0]} items', kids=[]))
        return out


def foldables(nodes, depth=0, acc=None, parent=None):
    """foldable regions in document order with their depth among foldables"""
    acc = [] if acc is None else acc
    for n in nodes:
        if is_foldable(n):
            n['_fdepth'], n['_fparent'] = depth, parent
            acc.append(n)
            foldables(n.get('kids', []), depth + 1, acc, n)
        else:
            foldables(n.get('kids', []), depth, acc, parent)
    return acc


def _value(n, in_main):
    """what an agent gains by seeing inside a region: controls first, prose last"""
    s = stats(n)
    v = s['ctl'] + 0.05 * s['words'] + 2 * (s['lists'] + s['tables'])
    if n['role'] == 'paragraph':
        v = 0.5 * s['links'] + 0.02 * s['words']
    if n['role'] in ('main', 'dialog', 'alertdialog', 'search', 'form'):
        v += 1000  # always try these first
    return v * (1.5 if in_main else 1.0)


def plan_folds(roots, budget, forced_open=(), full=frozenset(), boost=None):
    """(b) Budgeted unfolding. Everything foldable starts folded (one summary line each). A frontier of visible,
    folded regions is kept in a heap ordered by value-per-token (controls > structure > prose; Main and dialogs
    first). Pop the best; unfold it if its additive cost (own lines with child regions folded, minus its summary)
    still fits the budget, then push its children. Regions that do not fit stay folded and remain expandable."""
    import heapq
    fl = foldables(roots)
    open_ = set(forced_open)
    total = tok('\n'.join(Renderer(set(), full).render(roots, full=bool(full))))
    for n in fl:
        if n['id'] in open_:
            total += unfold_delta(n, open_, full)
    in_main = {}
    kids_of = {}
    for n in fl:
        p = n.get('_fparent')
        in_main[n['id']] = n['role'] == 'main' or (p is not None and in_main.get(p['id'], False))
        kids_of.setdefault(p['id'] if p is not None else None, []).append(n)
    heap, seq = [], 0

    def push_children(pid):
        nonlocal seq
        for c in kids_of.get(pid, []):
            if c['id'] in open_:
                push_children(c['id'])
                continue
            d = unfold_delta(c, open_, full)
            seq += 1
            v = _value(c, in_main[c['id']]) * (boost(c) if boost else 1.0)
            heapq.heappush(heap, (-v / max(d, 1), seq, d, c))

    push_children(None)
    for rid in list(forced_open):
        push_children(rid)
    while heap:
        _, _, d, n = heapq.heappop(heap)
        if n['id'] in open_ or total + d > budget:
            continue
        open_.add(n['id'])
        total += d
        push_children(n['id'])
    return open_, total


def unfold_delta(n, open_, full):
    r = Renderer(set(open_) - {n['id']}, full)
    folded = tok(fold_line(n))
    r.open = set(x for x in open_ if x != n['id']) | {n['id']}
    # render only n with its descendants folded (children ids not in open_ stay folded)
    own = '\n'.join(r.render([n], full=n['id'] in full))
    return tok(own) - folded


# ======================= the map object =======================
class AgentMap:
    def __init__(self, cap):
        self.cap = cap
        boxes = boxes_from_snapshot(cap['snap'])
        nodes, root = normalize(cap['ax'], boxes)
        tree = merge_text(build(nodes, root))
        if len(tree) == 1 and tree[0]['role'] in ('RootWebArea', 'WebArea'):
            tree = tree[0]['kids']
        tree = sectionize(tree)
        hoist(tree)
        label_regions(tree)
        assign_ids(tree)
        self.tree = tree
        self.by_id = {}
        walk(tree, lambda n, p: self.by_id.setdefault(n['id'], n) if 'id' in n else None)

    def header(self):
        return f"PAGE {short(self.cap['title'], 80)}  <{self.cap['url'][:80]}>"

    def render(self, budget=3000):
        open_, _ = plan_folds(self.tree, budget)
        order = [n['id'] for n in foldables(self.tree) if n['id'] in open_]
        while True:
            text = '\n'.join([self.header()] + Renderer(open_).render(self.tree))
            if tok(text) <= budget or not order:
                break
            open_.discard(order.pop())  # estimates drifted (indentation): fold the last-opened-in-doc-order region
        self.last_open = open_
        return text

    FORM_ROLES = {'textbox', 'searchbox', 'combobox', 'checkbox', 'radio', 'spinbutton', 'listbox', 'switch', 'slider'}
    CHROME_ROLES = {'banner', 'navigation', 'contentinfo', 'complementary'}

    def render_focused(self, budget=3000, viewport=None, focus_ids=(), extra_lines=()):
        """render() with priorities for an agent step: regions on or near the screen, forms, dialogs and the element the
        last action targeted unfold first; repeated site chrome (nav, header, footer, sidebars) unfolds last. Ends with
        a note listing what stayed folded and how to open it."""
        top = (viewport or {}).get('pageY', 0)
        h = (viewport or {}).get('clientHeight', 900)
        focus = set(i for i in focus_ids if i)
        cache = {}

        def boost(n):
            if id(n) not in cache:
                cache[id(n)] = _boost(n)
            return cache[id(n)]

        def _boost(n):
            b = bbox(n)
            k = 1.0
            if b and b[1] < top + 2 * h and b[1] + b[3] > top - h:
                k *= 4.0 if (b[1] < top + h and b[1] + b[3] > top) else 2.0
            desc = self._desc(n)
            roles = {d['role'] for d in desc}
            if roles & self.FORM_ROLES or n['role'] == 'form':
                k *= 3.0
            if n['role'] in ('dialog', 'alertdialog') or roles & {'dialog', 'alertdialog'}:
                k *= 10.0
            if focus and (n.get('id') in focus or any(d.get('id') in focus for d in desc)):
                k *= 10.0
            elif n['role'] in self.CHROME_ROLES:
                k *= 0.15
            return k
        open_, _ = plan_folds(self.tree, budget, boost=boost)
        fl = [n for n in foldables(self.tree)]
        order = [n['id'] for n in sorted((n for n in fl if n['id'] in open_), key=lambda n: boost(n))]
        while True:
            body = Renderer(open_).render(self.tree)
            text = '\n'.join([self.header()] + body)
            if tok(text) <= budget or not order:
                break
            open_.discard(order.pop(0))  # over budget: fold the lowest-priority open region first
        self.last_open = open_
        folded = [l.strip()[3:].split(']')[0] for l in body if l.strip().startswith('▸ [')]
        if folded:
            text += (f"\n(FOLDED to fit: {len(folded)} regions, e.g. " + ', '.join(folded[:8]) +
                     '. To see inside one, reply {"action": "expand", "id": "<that r: id>"}.)')
        return text

    def expand(self, rid, budget=3000):
        """(c) one region in full: every collapsed item listed, text untruncated; nested regions unfold breadth-first
        within `budget`."""
        n = self.by_id.get(rid)
        if n is None:
            raise KeyError(f'no region {rid}')
        full = frozenset(x['id'] for x in [n] + [k for k in self._desc(n) if 'id' in k and k['role'] in LISTY])
        open_, _ = plan_folds([n], budget, forced_open={rid}, full=full)
        body = Renderer(open_, full).render([n], full=True)
        return '\n'.join([f"EXPAND {rid}  ({short(n.get('name') or n['role'], 60)})"] + body)

    def _desc(self, n):
        acc = []
        walk(n.get('kids', []), lambda k, p: acc.append(k))
        return acc

    def crop(self, id_, path, pad=8):
        """(d) PNG of one node or region, cut from the full-page screenshot. DOMSnapshot boxes are CSS px in
        document space; the screenshot is device px, so every edge is multiplied by the measured DPR."""
        from PIL import Image
        n = self.by_id.get(id_)
        if n is None:
            raise KeyError(id_)
        b = bbox(n)
        if not b:
            raise ValueError(f'{id_} has no layout box')
        shot = self.cap['shot']
        dpr = shot['dpr']
        x0, y0 = max(0, b[0] - pad), max(0, b[1] - pad)
        x1, y1 = min(shot['css_w'], b[0] + b[2] + pad), min(shot['css_h'], b[1] + b[3] + pad)
        if y0 >= shot['css_h']:
            raise ValueError(f"{id_} is at y={b[1]:.0f}px, below the captured {shot['css_h']:.0f}px")
        im = Image.open(shot['path'])
        c = im.crop((round(x0 * dpr), round(y0 * dpr), round(x1 * dpr), round(y1 * dpr)))
        c.save(path)
        return dict(id=id_, css_box=[round(v, 1) for v in b], dpr=dpr, px=list(c.size), path=path)

    def snapshot(self):
        """flat, ID-keyed state for diffing"""
        out = {}
        for i, n in self.by_id.items():
            if n['role'] in INTERACTIVE:
                out[i] = dict(kind='ctl', role=n['role'], name=n['name'], value=n.get('value'),
                              state=n.get('state', []), region=n.get('region'))
            elif n['role'] in LISTY or n['role'] in ('dialog', 'alertdialog'):
                s = stats(n)
                kids = [short(flat_text(k, 80), 60) for k in n['kids'] if flat_text(k, 80)]
                out[i] = dict(kind='region', role=n['role'], name=n.get('name', ''), items=s['items'],
                              words=s['words'], first=kids[:3])
            elif n['role'] == 'paragraph':
                out[i] = dict(kind='para', text=flat_text(n, 10 ** 6))
        return out


def diff(a, b, max_lines=60):
    """(e) added / removed / changed, keyed by stable ID, grouped by region."""
    added = [k for k in b if k not in a]
    removed = [k for k in a if k not in b]
    # same control whose scope suffix changed (it moved to another region): pair it up instead of add+remove
    key = lambda s, k: (s[k].get('kind'), s[k].get('role'), s[k].get('name'), s[k].get('text'))
    pool = {}
    for k in removed:
        pool.setdefault(key(a, k), []).append(k)
    moved = []
    for k in list(added):
        lst = pool.get(key(b, k))
        if lst:
            old = lst.pop(0)
            moved.append((old, k))
            added.remove(k); removed.remove(old)
    changed = []
    for k in a:
        if k in b and a[k] != b[k]:
            x, y = a[k], b[k]
            fields = [f for f in sorted(set(x) | set(y)) if x.get(f) != y.get(f) and f != 'first']
            if x.get('kind') == 'region' and not fields and x.get('first') != y.get('first'):
                fields = ['first']
            if fields:
                changed.append((k, fields))
    lines = [f"DIFF  +{len(added)} added  -{len(removed)} removed  ~{len(changed)} changed  >{len(moved)} moved"]

    def where(s, k):
        return s[k].get('region') or ''

    def desc(s, k):
        v = s[k]
        if v['kind'] == 'ctl':
            return f"{v['role']} \"{short(v['name'], 60)}\""
        if v['kind'] == 'region':
            return f"{v['role']} \"{short(v['name'], 40)}\" ({v['items']} items)"
        return f"\"{short(v['text'], 60)}\""

    body = []
    for k in added:
        body.append((where(b, k), f"+ [{k}] {desc(b, k)}"))
    for k in removed:
        body.append((where(a, k), f"- [{k}] {desc(a, k)}"))
    for k, fs in changed:
        x, y = a[k], b[k]
        bits = []
        for f in fs:
            fv, tv = x.get(f), y.get(f)
            if isinstance(fv, str) and isinstance(tv, str) and len(fv) > 40:
                import difflib
                aw, bw = fv.split(), tv.split()
                ops = [(t, ' '.join(aw[i1:i2]), ' '.join(bw[j1:j2]))
                       for t, i1, i2, j1, j2 in difflib.SequenceMatcher(None, aw, bw).get_opcodes() if t != 'equal']
                bits.append(f"{f}: " + '; '.join((f'-"{short(x, 40)}" ' if x else '') + (f'+"{short(y, 40)}"' if y else '')
                                                for _, x, y in ops[:3]).strip())
                continue
            bits.append(f"{f}: {json.dumps(fv, ensure_ascii=False)} → {json.dumps(tv, ensure_ascii=False)}")
        body.append((where(b, k), f"~ [{k}] " + '; '.join(bits)))
    for o, n_ in moved:
        body.append((where(b, n_), f"> [{o}] → [{n_}] {desc(b, n_)}"))
    cur = None
    for reg, l in sorted(body, key=lambda t: t[0])[:max_lines]:
        if reg != cur:
            lines.append(f"@{reg or 'page'}")
            cur = reg
        lines.append('  ' + l)
    if len(body) > max_lines:
        lines.append(f"… {len(body) - max_lines} more changes")
    return '\n'.join(lines), dict(added=added, removed=removed, changed=[k for k, _ in changed], moved=moved)


# ======================= CLI =======================
def count_interactive_ax(ax):
    return sum(1 for n in ax if not n.get('ignored') and (n.get('role') or {}).get('value') in INTERACTIVE)


def cmd_map(url, name, budget=3000, dpr=1):
    os.makedirs('out', exist_ok=True)
    cap = asyncio.run(capture(url, shot_path=f'out/{name}.png', dpr=dpr))
    save_cap(cap, name)
    open(f'out/{name}.html', 'w').write(cap['html'])
    t = time.time()
    m = AgentMap(cap)
    text = m.render(budget)
    map_ms = (time.time() - t) * 1000
    open(f'out/{name}.map.txt', 'w').write(text)
    json.dump(m.snapshot(), open(f'out/{name}.snap.json', 'w'), ensure_ascii=False)
    ids = len(re.findall(r'\[(?:lnk|btn|in|sel|chk|rad|mi|tab|opt|sw|sld|num|ti):[^\]]+\]|\]\((?:lnk|btn|in|sel|chk|rad|mi|tab|opt|sw|sld|num|ti):', text))
    regions = [n for n in m.by_id.values() if n['role'] in REGION_ROLES]
    res = dict(site=name, url=cap['url'], title=cap['title'], html_tokens=tok(cap['html']),
               map_tokens=tok(text), ratio=round(tok(cap['html']) / max(1, tok(text)), 1),
               interactive_in_ax=count_interactive_ax(cap['ax']), ids_in_map=ids,
               ids_addressable=sum(1 for n in m.by_id.values() if n['role'] in INTERACTIVE),
               regions=len(regions), regions_folded=sum(1 for n in regions if n['id'] not in m.last_open),
               regions_labelled_inferred=sum(1 for n in regions if n.get('label_src') == 'inferred'),
               dpr=cap['shot']['dpr'] if cap['shot'] else None,
               load_s=round(cap['load_s'], 1), capture_s=round(cap['cap_s'], 2), map_ms=round(map_ms))
    json.dump(res, open(f'out/{name}.stats.json', 'w'), indent=1)
    print(json.dumps(res))
    return m


if __name__ == '__main__':
    a = sys.argv[1:]
    opt = lambda k, d: type(d)(a[a.index(k) + 1]) if k in a else d
    if a[0] == 'map':
        cmd_map(a[1], a[2], opt('--budget', 3000), opt('--dpr', 1))
    elif a[0] == 'mapfile':
        cmd_map('file://' + os.path.abspath(a[1]), a[2], opt('--budget', 3000), opt('--dpr', 1))
    elif a[0] == 'expand':
        print(AgentMap(load_cap(a[1])).expand(a[2], opt('--budget', 3000)))
    elif a[0] == 'crop':
        print(json.dumps(AgentMap(load_cap(a[1])).crop(a[2], a[3] if len(a) > 3 and not a[3].startswith('--') else f'out/{a[1]}.{slug(a[2], 40)}.png')))
    elif a[0] == 'diff':
        A, B = json.load(open(f'out/{a[1]}.snap.json')), json.load(open(f'out/{a[2]}.snap.json'))
        print(diff(A, B)[0])
