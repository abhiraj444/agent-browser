"""Bridge to the Chrome MV3 extension (real-browser mode, teach mode, handoff in the user's own Chrome).

The extension opens a websocket to /ext?k=TOKEN. The runner can then ask it for a snapshot (AX tree + DOMSnapshot via
chrome.debugger, the same passive domains), navigate, or click/type; teach-mode steps it records are saved as flows.
"""
import asyncio, itertools, json, os, time
from aiohttp import web, WSMsgType
import agentmap as AM
from flow import Flow


class ExtBridge:
    def __init__(self):
        self.ws = None
        self.info = {}
        self.pending = {}
        self.ids = itertools.count(1)
        self.teach = None  # Flow being recorded
        self.resume_cb = None

    def status(self):
        return dict(connected=self.ws is not None and not self.ws.closed, **self.info,
                    teaching=bool(self.teach), teach_steps=len(self.teach.d['steps']) if self.teach else 0)

    async def call(self, cmd, timeout=30, **kw):
        if not self.ws or self.ws.closed:
            raise RuntimeError('extension not connected')
        i = next(self.ids)
        f = asyncio.get_event_loop().create_future()
        self.pending[i] = f
        await self.ws.send_json(dict(id=i, cmd=cmd, **kw))
        return await asyncio.wait_for(f, timeout)

    async def snapshot(self):
        d = await self.call('snapshot', 45)
        d['snap']['_scale'] = d.get('scale', 1.0)
        cap = dict(url=d['url'], title=d['title'], ax=d['ax'], snap=d['snap'], shot=None)
        return AM.AgentMap(cap)

    async def map_handler(self, request):
        try:
            m = await self.snapshot()
            return web.Response(text=m.render(int(request.query.get('budget', 3000))))
        except Exception as e:
            return web.Response(status=503, text=str(e))

    async def handler(self, request):
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=64 * 1024 * 1024)
        await ws.prepare(request)
        self.ws = ws
        self.info = dict(since=time.time())
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            d = json.loads(msg.data)
            if 'reply' in d and d['reply'] in self.pending:
                f = self.pending.pop(d['reply'])
                if d.get('error'):
                    f.set_exception(RuntimeError(d['error']))
                else:
                    f.set_result(d.get('data'))
            elif d.get('type') == 'hello':
                self.info.update(ua=d.get('ua'), version=d.get('version'))
            elif d.get('type') == 'teach_start':
                self.teach = Flow(d.get('name') or f"taught {time.strftime('%d %b %H:%M')}")
            elif d.get('type') == 'teach_step' and self.teach:
                s = d['step']
                self.teach.add(s.pop('kind'), **s)
            elif d.get('type') == 'teach_stop' and self.teach:
                self.teach.add('ai_check', question=d.get('check') or 'Did the taught flow reach the same page?', expect='yes')
                path = self.teach.save()
                self.teach = None
                await ws.send_json(dict(type='saved', file=os.path.basename(path)))
            elif d.get('type') == 'resume' and self.resume_cb:
                self.resume_cb()
        if self.ws is ws:
            self.ws = None
        return ws
