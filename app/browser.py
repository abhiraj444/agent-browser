import glob, shutil
"""Headed Chrome on a virtual display, driven through passive CDP.

Only the Page, DOM, DOMSnapshot, Accessibility, Network (read-only events) and Emulation domains are used.
Never the Runtime domain, never script injection. Input does NOT go through CDP: see actor.py (OS-level XTEST).
"""
import asyncio, base64, json, os, random, subprocess, sys, tempfile, time, urllib.request
import websockets

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'agentmap'))
import agentmap as AM  # noqa: E402

CHROME = os.environ.get('CHROME_BIN', 'chromium')


class CDP:
    def __init__(self, ws):
        self.ws, self.i, self.pending, self.listeners = ws, 0, {}, []
        self.task = asyncio.create_task(self._reader())

    async def _reader(self):
        try:
            async for m in self.ws:
                d = json.loads(m)
                if 'id' in d and d['id'] in self.pending:
                    self.pending.pop(d['id']).set_result(d)
                else:
                    for fn in list(self.listeners):
                        try:
                            fn(d)
                        except Exception:
                            pass
        except Exception:
            pass

    async def send(self, method, timeout=60, **params):
        self.i += 1
        f = asyncio.get_event_loop().create_future()
        self.pending[self.i] = f
        await self.ws.send(json.dumps({'id': self.i, 'method': method, 'params': params}))
        r = await asyncio.wait_for(f, timeout)
        if 'error' in r:
            raise RuntimeError(f"{method}: {r['error'].get('message')}")
        return r['result']


class Browser:
    """One headed Chrome, one persistent profile, one tab. Same process (and so the same egress IP) for the task."""

    def __init__(self, display=':99', profile=None, width=1366, height=900, dpr=1.0, extra_args=()):
        self.display, self.width, self.height, self.dpr = display, width, height, dpr
        self.profile = profile or os.path.expanduser('~/.agentapp/profile')
        self.extra_args = list(extra_args)
        self.proc = self.cdp = self.ws = None
        self.main_status = {}  # url -> http status of the last main-document response
        self.last_status = None
        self.loaded = asyncio.Event()
        self.loading = False
        self.main_doc = {}     # url -> (requestId, mimeType) of main-document responses (to save PDFs shown in the viewer)
        self.bcdp = None       # browser-level CDP session (downloads)
        self.on_browser_event = None

    async def start(self):
        os.makedirs(self.profile, exist_ok=True)
        # a crashed run leaves a lock behind; Chrome refuses the profile until it is cleared
        for f in ('SingletonLock', 'SingletonSocket', 'SingletonCookie'):
            try:
                os.remove(os.path.join(self.profile, f))
            except OSError:
                pass
        exts = extension_dirs()
        ext_args = ([f"--load-extension={','.join(exts)}",
                     '--disable-features=DisableLoadExtensionCommandLineSwitch,Translate,MediaRouter'] if exts else [])
        port = random.randint(9300, 9399)
        env = dict(os.environ, DISPLAY=self.display)
        args = [CHROME, f'--user-data-dir={self.profile}', f'--remote-debugging-port={port}',
                '--no-first-run', '--no-default-browser-check', '--no-sandbox', '--no-proxy-server',
                '--kiosk', '--window-position=0,0', f'--window-size={self.width},{self.height}',
                f'--force-device-scale-factor={self.dpr}', '--lang=en-IN', '--disable-dev-shm-usage',
                '--password-store=basic', '--disable-features=Translate,MediaRouter',
                '--disable-blink-features=AutomationControlled', '--enable-logging=stderr', '--v=0'] + ext_args + self.extra_args + ['about:blank']
        # stale Chrome temp dirs pile up across restarts
        for d in glob.glob('/tmp/org.chromium.Chromium.*'):
            shutil.rmtree(d, ignore_errors=True)
        last_err = None
        for attempt in range(2):
            try:
                await self._launch(args, env, port + attempt)
                return self
            except RuntimeError as e:
                last_err = e
                print(f'chrome start attempt {attempt + 1} failed: {e}', flush=True)
                try:
                    self.proc.kill()
                except Exception:
                    pass
                subprocess.run(['pkill', '-f', f'user-data-dir={self.profile}'], capture_output=True)
                await asyncio.sleep(2)
                for f in ('SingletonLock', 'SingletonSocket', 'SingletonCookie'):
                    try:
                        os.remove(os.path.join(self.profile, f))
                    except OSError:
                        pass
        raise last_err

    async def _launch(self, args, env, port):
        args = [a if not a.startswith('--remote-debugging-port=') else f'--remote-debugging-port={port}' for a in args]
        self._errlog = open('/tmp/agentapp_chrome.log', 'w')
        self.proc = subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL, stderr=self._errlog)
        self.port = port
        for i in range(240):  # up to 60s: first launch with extensions is slow on a small VM
            if i % 8 == 7 and dismiss_extension_errors(self.display):
                print('closed an "Error Loading Extension" dialog; see /tmp/agentapp_chrome.log', flush=True)
                self._mark_broken_extensions()
            if self.proc.poll() is not None:
                raise RuntimeError(f'chrome exited with code {self.proc.returncode} (see /tmp/agentapp_chrome.log)')
            try:
                pages = self._pages()
                if pages:
                    self.known = {p['id'] for p in pages}
                    await self._attach(pages[0])
                    break
            except Exception:
                pass
            await asyncio.sleep(0.25)
        else:
            raise RuntimeError('chrome did not open its debug port within 60s')

    def _mark_broken_extensions(self):
        try:
            log = open('/tmp/agentapp_chrome.log', errors='ignore').read()
        except OSError:
            return
        import re
        bad = {os.path.basename(m.rstrip('/.')) for m in re.findall(r'Failed to load extension from: (\S+?)\.? ', log)}
        if bad:
            old = set(open(BROKEN_EXT).read().split()) if os.path.exists(BROKEN_EXT) else set()
            open(BROKEN_EXT, 'w').write('\n'.join(sorted(old | bad)))
            print(f'extensions disabled after load errors: {sorted(bad)}', flush=True)

    def _pages(self):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return [t for t in json.load(opener.open(f'http://127.0.0.1:{self.port}/json', timeout=5)) if t['type'] == 'page']

    async def follow_new_tab(self):
        """a click that opened a new tab (target=_blank): the kiosk window now shows it, so the agent follows it"""
        try:
            pages = self._pages()
        except Exception:
            return False
        new = [p for p in pages if p['id'] not in self.known]
        self.known |= {p['id'] for p in pages}
        if not new:
            return False
        try:
            await self.ws.close()
        except Exception:
            pass
        await self._attach(new[0])
        self.loading = False
        await asyncio.sleep(2.0)  # its load event may have fired before we attached
        return True

    async def reset_tabs(self, blank=True):
        """start of every task: close every extra tab (ones the user or a target=_blank link opened),
        keep exactly one, attach to it and bring it to the front so the live view and the agent see the same tab"""
        try:
            pages = self._pages()
        except Exception:
            return False
        if not pages:
            return False
        keep = next((p for p in pages if p['id'] == getattr(self, 'page_id', None)), pages[0])
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for p in pages:
            if p['id'] != keep['id']:
                try:
                    opener.open(f"http://127.0.0.1:{self.port}/json/close/{p['id']}", timeout=5).read()
                except Exception:
                    pass
        if keep['id'] != getattr(self, 'page_id', None) or self.ws is None or self.ws.closed:
            try:
                await self.ws.close()
            except Exception:
                pass
            await self._attach(keep)
        try:
            self.known = {p['id'] for p in self._pages()}
        except Exception:
            self.known = {keep['id']}
        try:
            await self.cdp.send('Page.bringToFront')
        except Exception:
            pass
        self.loading = False
        if blank:
            try:
                await self.goto('about:blank', settle=0.3, timeout=5)
            except Exception:
                pass
        return True

    async def _attach(self, page):
        self.page_id = page['id']
        self.ws = await websockets.connect(page['webSocketDebuggerUrl'], max_size=None)
        self.cdp = CDP(self.ws)
        self.cdp.listeners.append(self._on_event)
        await self.cdp.send('Page.enable')
        await self.cdp.send('Network.enable')  # read-only: we only listen for the main document's status
        await self.cdp.send('DOM.enable')
        await self._browser_session()
        tree = await self.cdp.send('Page.getFrameTree')
        self.main_frame = tree['frameTree']['frame']['id']
        return self

    async def _browser_session(self):
        """one browser-wide CDP connection for Browser.* (download events survive tab switches)"""
        if self.bcdp is not None and not self.bcdp.ws.closed:
            return
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            v = json.load(opener.open(f'http://127.0.0.1:{self.port}/json/version', timeout=5))
            ws = await websockets.connect(v['webSocketDebuggerUrl'], max_size=None)
            self.bcdp = CDP(ws)
            self.bcdp.listeners.append(lambda d: self.on_browser_event and self.on_browser_event(d))
        except Exception as e:
            print('browser session for downloads failed:', e, flush=True)
            self.bcdp = None

    async def set_download_dir(self, path):
        """Chrome saves downloads as <guid> in path and reports progress (see downloads.py)"""
        await self._browser_session()
        if not self.bcdp:
            return False
        os.makedirs(path, exist_ok=True)
        await self.bcdp.send('Browser.setDownloadBehavior', behavior='allowAndName', downloadPath=path, eventsEnabled=True)
        return True

    def current_doc(self, url):
        return self.main_doc.get(url, (None, ''))

    async def response_body(self, request_id):
        r = await self.cdp.send('Network.getResponseBody', timeout=30, requestId=request_id)
        return base64.b64decode(r['body']) if r.get('base64Encoded') else r['body'].encode('latin-1', 'ignore')

    async def cookie_header(self, url):
        try:
            cs = (await self.cdp.send('Network.getCookies', urls=[url]))['cookies']
        except Exception:
            return ''
        return '; '.join(f"{c['name']}={c['value']}" for c in cs)

    async def print_pdf(self):
        r = await self.cdp.send('Page.printToPDF', timeout=60, printBackground=True, preferCSSPageSize=True)
        return base64.b64decode(r['data'])

    # ---------- takeover helpers (the person's own input in the live view) ----------
    EDIT_ROLES = {'textbox', 'searchbox', 'combobox', 'spinbutton', 'textField', 'TextField', 'searchBox'}

    async def probe(self, x, y):
        """what is at viewport CSS point (x, y), and is it something you type into?
        Returns dict(editable, kind, label, type, value, be, options) or None."""
        try:
            r = await self.cdp.send('DOM.getNodeForLocation', timeout=5, x=int(x), y=int(y), includeUserAgentShadowDOM=False,
                                    ignorePointerEventsNone=True)
        except Exception:
            return None
        return await self.describe_field(r.get('backendNodeId'))

    async def focused_field(self):
        """the element that has keyboard focus (a tap on a <label> focuses its input), from the accessibility tree"""
        try:
            nodes = (await self.cdp.send('Accessibility.getFullAXTree', timeout=8))['nodes']
        except Exception:
            return None
        for n in nodes:
            props = {p['name']: p['value'].get('value') for p in n.get('properties', [])}
            if props.get('focused') and n.get('backendDOMNodeId'):
                f = await self.describe_field(n['backendDOMNodeId'])
                if f and (f['editable'] or f['kind'] == 'select'):
                    return f
        return None

    async def describe_field(self, be):
        if not be:
            return None
        try:
            d = (await self.cdp.send('DOM.describeNode', timeout=5, backendNodeId=be, depth=2))['node']
        except Exception:
            return None
        a = d.get('attributes') or []
        attrs = dict(zip(a[0::2], a[1::2]))
        tag = d.get('nodeName', '')
        try:
            ax = (await self.cdp.send('Accessibility.getPartialAXTree', timeout=5, backendNodeId=be, fetchRelatives=True))['nodes']
        except Exception:
            ax = []
        me = next((n for n in ax if n.get('backendDOMNodeId') == be), ax[0] if ax else {})
        role = (me.get('role') or {}).get('value', '')
        name = ((me.get('name') or {}).get('value') or '').strip()
        props = {p['name']: p['value'].get('value') for p in me.get('properties', [])}
        typ = (attrs.get('type') or '').lower()
        editable = (tag in ('INPUT', 'TEXTAREA') and typ not in ('checkbox', 'radio', 'submit', 'button', 'reset', 'file',
                                                                    'image', 'hidden', 'range', 'color')) \
            or bool(props.get('editable')) or attrs.get('contenteditable') in ('', 'true', 'plaintext-only')
        if not editable and tag not in ('SELECT',):
            # tapped inside an editable ancestor (rich editors put spans inside the contenteditable)
            for n in ax:
                pp = {p['name']: p['value'].get('value') for p in n.get('properties', [])}
                if n is not me and pp.get('editable') and n.get('backendDOMNodeId'):
                    return await self.describe_field(n['backendDOMNodeId'])
        kind = 'select' if tag == 'SELECT' else ('text' if editable else 'other')
        out = dict(editable=bool(editable), kind=kind, be=be, tag=tag, role=role,
                   label=(name or attrs.get('aria-label') or attrs.get('placeholder') or attrs.get('name') or '')[:90],
                   type=typ or ('textarea' if tag == 'TEXTAREA' else 'text'), inputmode=attrs.get('inputmode', ''),
                   maxlength=attrs.get('maxlength', ''), autocomplete=attrs.get('autocomplete', ''))
        val = (me.get('value') or {}).get('value')
        out['value'] = '' if typ == 'password' else (str(val) if val is not None else attrs.get('value', ''))
        if tag == 'SELECT':
            opts = []
            for c in d.get('children') or []:
                kids = (c.get('children') or []) if c.get('nodeName') == 'OPTGROUP' else [c]
                for o in kids:
                    if o.get('nodeName') == 'OPTION':
                        oa = o.get('attributes') or []
                        oa = dict(zip(oa[0::2], oa[1::2]))
                        txt = ' '.join(''.join(k.get('nodeValue') or '' for k in o.get('children') or []).split())
                        opts.append(dict(text=txt, disabled='disabled' in oa))
            out['options'] = opts
        return out

    async def focus_node(self, be):
        await self.cdp.send('DOM.focus', timeout=5, backendNodeId=be)

    async def insert_text(self, text):
        """the person's typed text from the live view (Unicode-safe; only used for takeover input, never by the agent)"""
        await self.cdp.send('Input.insertText', timeout=10, text=text)

    def _on_event(self, d):
        m = d.get('method')
        if m == 'Network.responseReceived' and d['params'].get('type') == 'Document':
            r = d['params']['response']
            self.main_status[r['url']] = r['status']
            self.last_status = r['status']
            if d['params'].get('frameId') in (None, getattr(self, 'main_frame', None)):
                self.main_doc[r['url']] = (d['params'].get('requestId'), (r.get('mimeType') or '').lower())
                if len(self.main_doc) > 50:
                    self.main_doc.pop(next(iter(self.main_doc)))
        elif m == 'Page.frameStartedLoading' and not d['params'].get('frameId', '').startswith('_') and \
                d['params'].get('frameId') == getattr(self, 'main_frame', None):
            self.loading = True
            self.loaded.clear()
        elif m == 'Page.frameNavigated' and not d['params']['frame'].get('parentId'):
            self.main_frame = d['params']['frame']['id']
        elif m == 'Page.loadEventFired':
            self.loading = False
            self.loaded.set()

    async def restart(self):
        """stop Chrome and start it again on the same profile (picks up newly added extensions)"""
        await self.stop()
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
        self.ws = None
        return await self.start()

    async def stop(self):
        for w in (self.ws, getattr(self.bcdp, 'ws', None)):
            try:
                await w.close()
            except Exception:
                pass
        self.bcdp = None
        if self.proc:
            self.proc.terminate()

    # ---------- navigation ----------
    async def goto(self, url, settle=1.5, timeout=25):
        self.loaded.clear()
        await self.cdp.send('Page.navigate', url=url)
        await self.wait_load(timeout, settle)

    async def wait_load(self, timeout=20, settle=1.5):
        try:
            await asyncio.wait_for(self.loaded.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        self.loading = False
        await asyncio.sleep(settle)

    async def back(self):
        h = await self.cdp.send('Page.getNavigationHistory')
        if h['currentIndex'] > 0:
            self.loaded.clear()
            await self.cdp.send('Page.navigateToHistoryEntry', entryId=h['entries'][h['currentIndex'] - 1]['id'])
            await self.wait_load()

    async def location(self):
        h = await self.cdp.send('Page.getNavigationHistory')
        cur = h['entries'][h['currentIndex']]
        return cur['url'], cur['title']

    # ---------- perception ----------
    async def screenshot(self, path=None):
        data = None
        for attempt in range(3):
            try:
                data = (await self.cdp.send('Page.captureScreenshot', timeout=12, format='png'))['data']
                break
            except Exception:
                await asyncio.sleep(1.5)  # mid-navigation: compositor not ready yet
        if data is None:
            return None
        raw = base64.b64decode(data)
        if path:
            open(path, 'wb').write(raw)
        return raw

    async def snapshot(self, shot_path=None):
        """agentmap of the current page (whole document, not just the viewport) + viewport screenshot"""
        if self.loading:
            await self.wait_load(15, 0.8)
        doc = await self.cdp.send('DOM.getDocument', depth=-1, pierce=True)
        ax = (await self.cdp.send('Accessibility.getFullAXTree'))['nodes']
        snap = await self.cdp.send('DOMSnapshot.captureSnapshot', computedStyles=[], includeDOMRects=True)
        lm = await self.cdp.send('Page.getLayoutMetrics')
        cw = lm['cssContentSize']['width']
        snap['_scale'] = round(snap['documents'][0].get('contentWidth', cw) / cw, 4) if cw else 1.0
        url, title = await self.location()
        shot = None
        if shot_path and await self.screenshot(shot_path):
            vv = lm['cssVisualViewport']
            shot = dict(path=shot_path, css_w=vv['clientWidth'], css_h=vv['clientHeight'], dpr=self.dpr,
                        viewport=True, page_x=vv['pageX'], page_y=vv['pageY'])
        cap = dict(url=url, title=title, ax=ax, snap=snap, shot=shot, status=self.main_status.get(url, self.last_status))
        m = AM.AgentMap(cap)
        m.dom = doc
        m.viewport = lm['cssVisualViewport']
        return m

    async def quad(self, backend_id):
        """viewport CSS-px rectangle of a node (x, y, w, h) or None"""
        try:
            q = (await self.cdp.send('DOM.getContentQuads', backendNodeId=backend_id))['quads']
        except Exception:
            return None
        if not q:
            return None
        q = q[0]
        xs, ys = q[0::2], q[1::2]
        return [min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)]

    async def viewport(self):
        return (await self.cdp.send('Page.getLayoutMetrics'))['cssVisualViewport']


BUILTIN_EXT = os.path.join(HERE, '..', 'extensions')          # baked into the image (uBlock Origin Lite)
USER_EXT = os.path.expanduser('~/.agentapp/extensions')      # added from the dashboard; survive restarts (volume)


BROKEN_EXT = os.path.expanduser('~/.agentapp/broken_extensions')


def _writable_copy(p):
    """Chrome writes indexed ad-block rulesets into an unpacked extension's own folder, so a read-only
    built-in copy fails with 'Internal error while parsing rules'. Load built-ins from a writable mirror."""
    if os.access(p, os.W_OK):
        return p
    dst = os.path.join(os.path.expanduser('~/.agentapp/builtin_extensions'), os.path.basename(p))
    src_m, dst_m = os.path.join(p, 'manifest.json'), os.path.join(dst, 'manifest.json')
    if not (os.path.isfile(dst_m) and open(dst_m, 'rb').read() == open(src_m, 'rb').read()):
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(p, dst)
    return dst


def extension_dirs():
    broken = set(open(BROKEN_EXT).read().split()) if os.path.exists(BROKEN_EXT) else set()
    out = []
    for root in (BUILTIN_EXT, USER_EXT):
        if os.path.isdir(root):
            for d in sorted(os.listdir(root)):
                p = os.path.abspath(os.path.join(root, d))
                if (os.path.isfile(os.path.join(p, 'manifest.json')) and not os.path.exists(os.path.join(p, '.disabled'))
                        and d not in broken):
                    try:
                        out.append(_writable_copy(p))
                    except OSError as e:
                        print(f'extension {d} skipped: {e}', flush=True)
    return out


def dismiss_extension_errors(display):
    """an extension that fails to load opens a modal 'Error Loading Extension' box that blocks Chrome's startup:
    close it, remember the extension as broken, and let Chrome carry on without it"""
    env = dict(os.environ, DISPLAY=display)
    r = subprocess.run(['xdotool', 'search', '--name', 'Error Loading Extension'], env=env, capture_output=True, text=True)
    wins = r.stdout.split()
    for w in wins:
        subprocess.run(['xdotool', 'windowactivate', '--sync', w, 'key', 'Return'], env=env, capture_output=True)
    return bool(wins)


def install_extension(ref):
    """download a Chrome Web Store extension (store URL or 32-letter id) and unpack it into USER_EXT. Returns (id, name)"""
    import io, re, zipfile
    m = re.search(r'([a-p]{32})', ref or '')
    if not m:
        raise ValueError('give a Chrome Web Store link or a 32-letter extension id')
    eid = m.group(1)
    url = ('https://clients2.google.com/service/update2/crx?response=redirect&prodversion=140.0&acceptformat=crx2,crx3'
           f'&x=id%3D{eid}%26uc')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = opener.open(urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=60).read()
    z = data.find(b'PK\x03\x04')  # a .crx is a zip behind a signed header
    if z < 0:
        raise ValueError('the Web Store did not return an extension (is it still listed?)')
    dest = os.path.join(USER_EXT, eid)
    import shutil
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest)
    zipfile.ZipFile(io.BytesIO(data[z:])).extractall(dest)
    shutil.rmtree(os.path.join(dest, '_metadata'), ignore_errors=True)  # Chrome refuses unpacked dirs with _metadata
    man = json.load(open(os.path.join(dest, 'manifest.json'), encoding='utf-8-sig'))
    if man.get('manifest_version') == 2:
        shutil.rmtree(dest, ignore_errors=True)
        raise ValueError('that is a Manifest V2 extension, which this Chromium no longer runs '
                         '(for uBlock Origin use uBlock Origin Lite)')
    return eid, _ext_name(dest, man)


def _ext_name(p, man):
    n = man.get('name', '')
    if n.startswith('__MSG_'):
        key = n[6:-2]
        for loc in (man.get('default_locale', 'en'), 'en', 'en_US'):
            try:
                msgs = json.load(open(os.path.join(p, '_locales', loc, 'messages.json'), encoding='utf-8-sig'))
                hit = next((v['message'] for k, v in msgs.items() if k.lower() == key.lower()), None)
                if hit:
                    return hit
            except Exception:
                pass
        return os.path.basename(p)
    return n or os.path.basename(p)


def list_extensions():
    out = []
    for p in extension_dirs():
        try:
            n = _ext_name(p, json.load(open(os.path.join(p, 'manifest.json'), encoding='utf-8-sig')))
        except Exception:
            n = os.path.basename(p)
        out.append(dict(id=os.path.basename(p), name=n,
                        builtin=p.startswith(os.path.abspath(BUILTIN_EXT))))
    return out


def iframes(dom_root):
    """[(src, backendNodeId)] for every iframe in the pierced DOM tree"""
    out = []

    def walk(n):
        if n.get('nodeName') in ('IFRAME', 'FRAME'):
            a = n.get('attributes', [])
            attrs = dict(zip(a[0::2], a[1::2]))
            out.append((attrs.get('src', ''), n.get('backendNodeId'), attrs.get('title', '')))
        for k in n.get('children', []) or []:
            walk(k)
        if n.get('contentDocument'):
            walk(n['contentDocument'])
        for k in n.get('shadowRoots', []) or []:
            walk(k)
    walk(dom_root['root'])
    return out
