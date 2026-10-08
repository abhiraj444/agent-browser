import re
"""HTTP server: dashboard, task API, live view (noVNC over a websocket->VNC bridge), takeover input, extension bridge.

Everything sits behind one secret token (?k=TOKEN once sets a cookie), because the live view controls a real browser.
"""
import asyncio, json, os, secrets
from aiohttp import web, WSMsgType

from agent import Task
from flow import FLOWS
import downloads as DL

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')
NOVNC = os.environ.get('NOVNC_DIR', '/usr/share/novnc')


def get_token():
    p = os.path.expanduser('~/.agentapp/token')
    if os.environ.get('APP_TOKEN'):
        return os.environ['APP_TOKEN']
    if os.path.exists(p):
        return open(p).read().strip()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    t = secrets.token_urlsafe(12)
    open(p, 'w').write(t)
    return t


def make_app(runner, ext=None):
    token = get_token()

    @web.middleware
    async def auth(request, handler):
        k = request.query.get('k')
        if k and request.path == '/ext' and secrets.compare_digest(k, token):
            return await handler(request)  # the extension's websocket carries the key in the URL
        if k and secrets.compare_digest(k, token):
            # serve the page directly (no redirect: some browsers drop a cookie set on a 302) and remember the key
            resp = await handler(request)
            resp.set_cookie('k', token, max_age=30 * 86400, httponly=True, samesite='Lax', secure=True)
            return resp
        if request.cookies.get('k') == token or request.headers.get('X-Token') == token or request.path == '/health':
            return await handler(request)
        return web.Response(status=401, text='Open the link with its ?k= key.')

    app = web.Application(middlewares=[auth], client_max_size=4 * 1024 * 1024)
    r = app.router

    async def page(name):
        return web.FileResponse(os.path.join(WEB, name), headers={'Cache-Control': 'no-store'})

    r.add_get('/health', lambda q: web.json_response({'ok': True}))
    r.add_get('/', lambda q: page('dashboard.html'))
    r.add_get('/live/{id}', lambda q: page('live.html'))
    r.add_get('/live', lambda q: page('live.html'))

    async def tasks(q):
        ts = sorted(runner.tasks.values(), key=lambda t: -t.created)
        return web.json_response(dict(tasks=[t.public() for t in ts], current=runner.current.id if runner.current else None,
                                      ext=ext.status() if ext else None))

    async def new_task(q):
        d = await q.json()
        query = (d.get('query') or '').strip()
        if not query:
            return web.json_response({'error': 'query required'}, status=400)
        try:
            ms = max(2, min(40, int(d.get('max_steps') or 12)))
        except (TypeError, ValueError):
            ms = 12
        t = runner.submit(Task(query, d.get('goal') or None, force_block=bool(d.get('force_block')), max_steps=ms))
        return web.json_response(t.public())

    async def one(q):
        t = runner.tasks.get(q.match_info['id'])
        if not t:
            raise web.HTTPNotFound()
        return web.json_response(t.public())

    async def resume(q):
        t = runner.tasks.get(q.match_info['id'])
        if t:
            try:
                d = await q.json()
            except Exception:
                d = {}
            t.user_note = (d.get('note') or '').strip()[:500]
            t.resume_evt.set()
        return web.json_response({'ok': bool(t)})

    SETTABLE = {'openrouter_key': 'OPENROUTER_API_KEY', 'gemini_key': 'GEMINI_API_KEY', 'groq_key': 'GROQ_API_KEY',
                'custom_key': 'CUSTOM_LLM_KEY', 'custom_base': 'CUSTOM_LLM_BASE_URL', 'custom_model': 'CUSTOM_LLM_MODEL',
                'ntfy_topic': 'NTFY_TOPIC', 'brave_key': 'BRAVE_API_KEY', 'serper_key': 'SERPER_API_KEY',
                'github_token': 'GITHUB_TOKEN', 'github_repo': 'GITHUB_REPO', 'file_retention_hours': 'FILE_RETENTION_HOURS',
                'auto_captcha': 'AUTO_CAPTCHA', 'confirm_mode': 'CONFIRM_MODE'}

    async def settings_get(q):
        import llm
        out = {k: bool(os.environ.get(v)) for k, v in SETTABLE.items()}
        out['models'] = await asyncio.to_thread(llm.available)
        out['file_retention_hours'] = DL.retention_hours()
        out['auto_captcha'] = (os.environ.get('AUTO_CAPTCHA') or 'on').lower() not in ('0', 'off', 'false', 'no')
        out['confirm_mode'] = (os.environ.get('CONFIRM_MODE') or 'final').lower()
        return web.json_response(out)

    async def settings_set(q):
        d = await q.json()
        path = os.path.expanduser('~/.agentapp/env')
        cur = {}
        if os.path.exists(path):
            for line in open(path):
                k, _, v = line.strip().partition('=')
                if k:
                    cur[k] = v
        changed = []
        for k, env in SETTABLE.items():
            v = (d.get(k) or '').strip()
            if v:
                if not re.fullmatch(r'[A-Za-z0-9_\-\.:/@]{1,300}', v):
                    return web.json_response({'ok': False, 'error': f'{k} has odd characters'}, status=400)
                cur[env] = v
                os.environ[env] = v
                changed.append(k)
                import llm
                llm._model_cache.clear()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(''.join(f'{k}={v}\n' for k, v in cur.items()))
        return web.json_response({'ok': True, 'saved': changed})

    async def ext_list(q):
        from browser import list_extensions
        return web.json_response(dict(extensions=list_extensions()))

    async def ext_add(q):
        """install a Web Store extension (e.g. uBlock Origin Lite) and restart Chrome when no task is running"""
        from browser import install_extension
        d = await q.json()
        try:
            eid, name = await asyncio.to_thread(install_extension, d.get('ref', ''))
        except Exception as e:
            return web.json_response({'ok': False, 'error': str(e)[:200]}, status=400)
        if runner.current is None:
            await runner.b.restart()
            return web.json_response({'ok': True, 'id': eid, 'name': name, 'active': True})
        runner.restart_pending = True
        return web.json_response({'ok': True, 'id': eid, 'name': name, 'active': False,
                                  'note': 'installed; it turns on when the current task ends'})

    async def ext_remove(q):
        import shutil
        from browser import USER_EXT
        eid = q.match_info['id']
        if not re.fullmatch(r'[a-p]{32}', eid):
            return web.json_response({'ok': False, 'error': 'only extensions you added can be removed'}, status=400)
        shutil.rmtree(os.path.join(USER_EXT, eid), ignore_errors=True)
        if runner.current is None:
            await runner.b.restart()
        else:
            runner.restart_pending = True
        return web.json_response({'ok': True})

    async def github_sync(q):
        import ghsync
        return web.json_response(await asyncio.to_thread(ghsync.sync))

    async def guide(q):
        t = runner.tasks.get(q.match_info['id'])
        try:
            d = await q.json()
        except Exception:
            d = {}
        txt = (d.get('text') or '').strip()
        if t and txt and t.status not in ('done', 'failed', 'stopped', 'cancelled'):
            r = t.input_req
            if r and r.get('kind') == 'confirm' and not r.get('takeover') and getattr(t, 'input_evt', None) is not None:
                # a typed answer to "press this button?" counts as the answer, not just a note
                no = re.search(r"\b(no|not yet|don'?t|do not|stop|wait|cancel|mat|nahi|nahin)\b", txt, re.I)
                yes = re.search(r'\b(yes|yeah|yep|ok|okay|sure|go|go ahead|press|click|submit|send|proceed|do it|continue|haan?|ji)\b', txt, re.I)
                if yes and not no:
                    t.input_result = dict(action='approve', values={}, skipped=[])
                    t.input_evt.set()
                    t.emit('user', f'you: "{txt[:80]}" (taken as yes)')
                    return web.json_response({'ok': True, 'answered': 'approve'})
                if no:
                    t.add_guidance(txt, emit=False)
                    t.input_result = dict(action='reject', values={}, skipped=[])
                    t.input_evt.set()
                    t.emit('user', f'you: "{txt[:80]}" (taken as no)')
                    return web.json_response({'ok': True, 'answered': 'reject'})
            t.add_guidance(txt)
            return web.json_response({'ok': True})
        return web.json_response({'ok': False})

    async def pause(q):
        t = runner.tasks.get(q.match_info['id'])
        if t and t.status in ('running', 'queued'):
            t.pause_req = True
            t.emit('user', 'you asked to pause / take over (takes effect before the next action)')
        return web.json_response({'ok': bool(t)})

    async def stop(q):
        t = runner.tasks.get(q.match_info['id'])
        if not t:
            return web.json_response({'ok': False})
        if t.status == 'queued':
            t.status = 'cancelled'
        else:
            t.stop_req = True
            t.resume_evt.set()
            if t.aio and not t.aio.done():
                t.aio.cancel()  # stop now, even mid-action
        return web.json_response({'ok': True})

    async def events(q):
        t = runner.tasks.get(q.match_info['id'])
        if not t:
            raise web.HTTPNotFound()
        after = int(q.query.get('after', -1))
        return web.json_response(dict(status=t.status, events=[e for e in t.events if e['seq'] > after][:300]))

    async def shot(q):
        t = runner.tasks.get(q.match_info['id'])
        if t and t.shot and os.path.exists(t.shot):
            return web.FileResponse(t.shot, headers={'Cache-Control': 'no-store'})
        raise web.HTTPNotFound()

    async def remote_input(q):
        ev = await q.json()
        t = runner.current
        if t and t.status in ('running', 'queued'):
            return web.json_response({'ok': False, 'why': 'The agent is driving. Tap Pause / Take over first.'}, status=409)
        b, dpr = runner.b, runner.b.dpr or 1
        typ = ev.get('type')
        if typ == 'tap':
            await runner.a.remote(ev)
            await asyncio.sleep(0.25)
            fld = await b.probe(ev['x'] / dpr, ev['y'] / dpr)
            if not fld or not (fld['editable'] or fld['kind'] == 'select'):
                fld = await b.focused_field() or fld
            runner.takeover_be = fld['be'] if fld and (fld['editable'] or fld['kind'] == 'select') else None
            if t:
                t.emit('user', f"you: tap at ({ev.get('x')},{ev.get('y')})" + (f" on field \"{fld['label'][:40]}\"" if runner.takeover_be else ''))
            pub = {k: fld.get(k) for k in ('editable', 'kind', 'label', 'type', 'inputmode', 'maxlength', 'autocomplete',
                                            'options', 'value')} if fld else {'editable': False}
            return web.json_response({'ok': True, 'field': pub})
        if typ in ('set_text', 'select'):
            be = runner.takeover_be
            if not be:
                return web.json_response({'ok': False, 'why': 'Tap the field on the screen first.'}, status=400)
            try:
                await b.focus_node(be)
            except Exception:
                return web.json_response({'ok': False, 'why': 'That field is gone. Tap it again.'}, status=410)
            if typ == 'set_text':
                text = str(ev.get('text') or '')[:2000]
                runner.a._xdo('key', 'ctrl+a')
                if text:
                    if text.isascii() and '\n' not in text:
                        runner.a._xdo('type', '--delay', '6', '--', text)
                    else:
                        await b.insert_text(text)
                else:
                    runner.a._xdo('key', 'BackSpace')
            else:
                n, idx = int(ev.get('count') or 1), int(ev.get('index') or 0)
                runner.a._xdo('key', '--delay', '10', '--repeat', str(n + 1), 'Up')
                if idx:
                    runner.a._xdo('key', '--delay', '20', '--repeat', str(idx), 'Down')
            await asyncio.sleep(0.15)
            fld = await b.describe_field(be)
            if t:
                t.emit('user', 'you: typed into a field' if typ == 'set_text' else 'you: picked an option')  # text not logged
            return web.json_response({'ok': True, 'value': (fld or {}).get('value', '')})
        if ev.get('type') == 'goto':  # the user navigates the remote browser (e.g. to the Chrome Web Store)
            u = str(ev.get('url') or '').strip()
            if not re.match(r'^https?://', u):
                u = 'https://' + u
            await runner.b.goto(u)
            if t:
                t.emit('user', f'you: opened {u[:80]}')
            return web.json_response({'ok': True})
        await runner.a.remote(ev)
        if typ == 'scroll':
            runner.takeover_be = None
        if t:
            what = {'tap': f"tap at ({ev.get('x')},{ev.get('y')})", 'scroll': f"scroll {ev.get('ticks')}",
                    'text': 'typed text', 'key': f"key {ev.get('key')}"}.get(ev.get('type'), ev.get('type'))
            t.emit('user', f'you: {what}')  # typed text itself is not logged (could be a password)
        return web.json_response({'ok': True})

    async def fields(q):
        """editable fields on screen in screen pixels, so the live view knows synchronously that a tap hits a field
        (needed to open the phone keyboard inside the tap gesture)"""
        t = runner.current
        if t and t.status in ('running', 'queued'):
            return web.json_response({'fields': []})
        import forms
        try:
            m = await runner.b.snapshot()
            fl, _ = forms.extract(m, m.viewport)
        except Exception as e:
            return web.json_response({'fields': [], 'error': str(e)[:100]})
        vp, d = m.viewport, runner.b.dpr or 1
        out = []
        for f in fl:
            bx = f.get('box')
            if not bx or f['frame'] or f['type'] in ('checkbox', 'radio'):
                continue
            x, y = (bx[0] - vp.get('pageX', 0)) * d, (bx[1] - vp.get('pageY', 0)) * d
            if y + bx[3] * d < 0 or y > vp['clientHeight'] * d:
                continue
            out.append(dict(box=[round(x), round(y), round(bx[2] * d), round(bx[3] * d)], kind='select' if f['type'] == 'select' else 'text',
                            label=f['label'][:60]))
        return web.json_response({'fields': out})

    async def task_input(q):
        """answer a needs_input request: submit values / skip, approve or reject a final click, or take over"""
        t = runner.tasks.get(q.match_info['id'])
        if not t or not t.input_req:
            return web.json_response({'ok': False, 'why': 'Nothing is waiting for your input.'}, status=409)
        d = await q.json()
        act = d.get('action') or 'submit'
        if d.get('req') and d['req'] != t.input_req['id']:
            return web.json_response({'ok': False, 'why': 'That request was already answered.'}, status=409)
        if act == 'takeover':
            t.input_req['takeover'] = True
            t.status = 'paused'
            t.resume_evt.clear()
            t.emit('user', 'you chose to fill it yourself: tap the field on the screen, type, then tap Done, continue')
            return web.json_response({'ok': True})
        if act not in ('submit', 'approve', 'reject'):
            return web.json_response({'ok': False, 'why': 'unknown action'}, status=400)
        keys = {f['key'] for f in t.input_req['fields']}
        vals = {k: str(v)[:500] for k, v in (d.get('values') or {}).items() if k in keys and str(v).strip() != ''}
        skipped = [k for k in d.get('skipped') or [] if k in keys and k not in vals]
        t.input_result = dict(action=act, values=vals, skipped=skipped)
        t.input_evt.set()
        return web.json_response({'ok': True, 'got': sorted(vals), 'skipped': skipped})

    async def file_get(q):
        p = DL.resolve(q.match_info['id'], q.match_info['name'])
        if not p:
            raise web.HTTPNotFound()
        from urllib.parse import quote
        name = os.path.basename(p)
        inline = q.query.get('inline') == '1'
        return web.FileResponse(p, headers={
            'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
            'Content-Disposition': f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(name)}"})

    async def file_delete(q):
        p = DL.resolve(q.match_info['id'], q.match_info['name'])
        if p:
            os.remove(p)
        t = runner.tasks.get(q.match_info['id'])
        if t:
            t.files = [f for f in t.files if f['name'] != q.match_info['name']]
            t.emit('user', f"you deleted {q.match_info['name']}")
        return web.json_response({'ok': bool(p)})

    async def files_all(q):
        """every task's files still on disk (also ones from before a restart)"""
        out = []
        if os.path.isdir(DL.ROOT):
            for tid in sorted(os.listdir(DL.ROOT)):
                for name in DL.list_files(tid):
                    p = os.path.join(DL.ROOT, tid, name)
                    out.append(dict(task=tid, name=name, size=os.path.getsize(p), time=os.path.getmtime(p)))
        return web.json_response(dict(files=sorted(out, key=lambda f: -f['time']), retention_hours=DL.retention_hours()))

    async def flows(q):
        os.makedirs(FLOWS, exist_ok=True)
        out = []
        for f in sorted(os.listdir(FLOWS)):
            if f.endswith('.json'):
                d = json.load(open(os.path.join(FLOWS, f)))
                out.append(dict(file=f, name=d.get('name'), steps=len(d.get('steps', [])),
                                kinds=[s['kind'] for s in d.get('steps', [])], created=d.get('created')))
        return web.json_response(out)

    async def flow_get(q):
        p = os.path.join(FLOWS, os.path.basename(q.match_info['name']))
        if not os.path.exists(p):
            raise web.HTTPNotFound()
        return web.FileResponse(p)

    async def replay(q):
        d = await q.json()
        p = os.path.join(FLOWS, os.path.basename(d['file']))
        t = Task(d['file'], mode='replay', flow_path=p)
        runner.submit(t)
        return web.json_response(t.public())

    async def vnc(q):
        """websocket <-> x11vnc TCP bridge (what websockify does), so noVNC rides the same origin and token"""
        ws = web.WebSocketResponse(protocols=('binary',), max_msg_size=0)
        await ws.prepare(q)
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', int(os.environ.get('VNC_PORT', 5900)))
        except OSError:
            await ws.close(message=b'vnc not running')
            return ws

        async def up():
            try:
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    await ws.send_bytes(data)
            except Exception:
                pass
            await ws.close()

        task = asyncio.create_task(up())
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                writer.write(msg.data)
                await writer.drain()
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break
        task.cancel()
        writer.close()
        return ws

    r.add_get('/api/tasks', tasks)
    r.add_post('/api/tasks', new_task)
    r.add_get('/api/tasks/{id}', one)
    r.add_post('/api/tasks/{id}/resume', resume)
    r.add_post('/api/tasks/{id}/cancel', stop)
    r.add_post('/api/tasks/{id}/stop', stop)
    r.add_post('/api/tasks/{id}/pause', pause)
    r.add_post('/api/tasks/{id}/guide', guide)
    r.add_get('/api/settings', settings_get)
    r.add_post('/api/settings', settings_set)
    r.add_post('/api/github/sync', github_sync)
    r.add_get('/api/extensions', ext_list)
    r.add_post('/api/extensions', ext_add)
    r.add_post('/api/extensions/{id}/remove', ext_remove)
    r.add_get('/api/tasks/{id}/events', events)
    r.add_get('/api/shot/{id}.png', shot)
    r.add_post('/api/input', remote_input)
    r.add_get('/api/flows', flows)
    r.add_get('/api/fields', fields)
    r.add_post('/api/tasks/{id}/input', task_input)
    r.add_get('/api/tasks/{id}/files/{name}', file_get)
    r.add_post('/api/tasks/{id}/files/{name}/delete', file_delete)
    r.add_get('/api/files', files_all)
    r.add_get('/api/flows/{name}', flow_get)
    r.add_post('/api/replay', replay)
    r.add_get('/vnc', vnc)
    if ext:
        r.add_get('/ext', ext.handler)
        r.add_get('/api/ext', lambda q: web.json_response(ext.status()))
        r.add_get('/api/ext/map', ext.map_handler)
    r.add_static('/novnc', NOVNC, follow_symlinks=True)
    r.add_static('/static', WEB)

    async def janitor(app_):
        async def loop():
            while True:
                try:
                    n = await asyncio.to_thread(DL.cleanup)
                    if n:
                        print(f'deleted {n} downloaded files past the retention period', flush=True)
                except Exception as e:
                    print('file cleanup failed', e, flush=True)
                await asyncio.sleep(1800)
        app_['janitor'] = asyncio.create_task(loop())
    app.on_startup.append(janitor)
    return app, token
