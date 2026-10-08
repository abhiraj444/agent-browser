"""Actor: human-like OS-level input through the X server (XTEST via xdotool). No ChromeDriver, no CDP Input.*.

The browser runs in kiosk mode at (0,0), so viewport CSS px * DPR == screen px.
On the user's laptop the same interface is backed by the OS input APIs (see README: SendInput / CGEvent / uinput).
"""
import asyncio, math, os, random, subprocess


class Actor:
    def __init__(self, browser, display=':99'):
        self.b, self.display = browser, display
        self.env = dict(os.environ, DISPLAY=display)
        self.x, self.y = 683, 450
        self.on_event = None  # callback(kind, text, **data) -> the task's live action stream

    def emit(self, kind, text, **d):
        if self.on_event:
            try:
                self.on_event(kind, text, **d)
            except Exception:
                pass

    def _xdo(self, *args):
        subprocess.run(['xdotool', *map(str, args)], env=self.env, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    async def pause(self, lo=0.25, hi=0.8):
        await asyncio.sleep(random.uniform(lo, hi))

    # ---------- mouse ----------
    async def move(self, x, y):
        """cubic Bezier with a random bow, eased, ~60 Hz, with a small overshoot-free settle"""
        x0, y0 = self.x, self.y
        dist = math.hypot(x - x0, y - y0)
        steps = max(8, min(45, int(dist / 18)))
        bow = random.uniform(-0.25, 0.25) * dist
        nx, ny = (-(y - y0) / dist, (x - x0) / dist) if dist else (0, 0)
        c1 = (x0 + (x - x0) * 0.3 + nx * bow, y0 + (y - y0) * 0.3 + ny * bow)
        c2 = (x0 + (x - x0) * 0.7 + nx * bow * 0.5, y0 + (y - y0) * 0.7 + ny * bow * 0.5)
        for i in range(1, steps + 1):
            t = i / steps
            t = t * t * (3 - 2 * t)  # ease in-out
            px = (1 - t) ** 3 * x0 + 3 * (1 - t) ** 2 * t * c1[0] + 3 * (1 - t) * t ** 2 * c2[0] + t ** 3 * x
            py = (1 - t) ** 3 * y0 + 3 * (1 - t) ** 2 * t * c1[1] + 3 * (1 - t) * t ** 2 * c2[1] + t ** 3 * y
            self._xdo('mousemove', int(px), int(py))
            await asyncio.sleep(random.uniform(0.008, 0.018))
        self._xdo('mousemove', int(x), int(y))
        self.x, self.y = int(x), int(y)

    async def click_xy(self, x, y, button=1):
        await self.move(x, y)
        await self.pause(0.06, 0.18)
        self._xdo('mousedown', button)
        await asyncio.sleep(random.uniform(0.05, 0.12))
        self._xdo('mouseup', button)

    async def wheel(self, ticks):
        """ticks > 0 scrolls down. ~100 CSS px per tick in Chrome on X11."""
        btn = 5 if ticks > 0 else 4
        self.emit('scroll', f"scroll {'down' if ticks > 0 else 'up'} {abs(int(ticks))}", ticks=int(ticks))
        for _ in range(abs(int(ticks))):
            self._xdo('click', btn)
            await asyncio.sleep(random.uniform(0.03, 0.09))

    # ---------- element-level ----------
    async def scroll_into_view(self, backend_id, margin=40):
        last_y = None
        for _ in range(25):
            vp = await self.b.viewport()
            r = await self.b.quad(backend_id)
            if r is None:
                return None
            top, bot, H = r[1], r[1] + r[3], vp['clientHeight']
            if (top >= 0 and bot <= H) or (top < margin and bot > H - margin):
                return r  # fully visible, or taller than the viewport and covering it
            if last_y is not None and abs(vp['pageY'] - last_y) < 1:
                return r  # page cannot scroll any further
            last_y = vp['pageY']
            need = (top + r[3] / 2) - H / 2
            if not (0 < self.x < self.b.width * self.b.dpr and 0 < self.y < H * self.b.dpr):
                await self.move(self.b.width / 2 * self.b.dpr, H / 2 * self.b.dpr)
            await self.wheel(max(1, min(8, round(abs(need) / 100))) * (1 if need > 0 else -1))
            await asyncio.sleep(0.3)
        return await self.b.quad(backend_id)

    async def click(self, node):
        be = node.get('be')
        if not be:
            raise ValueError(f"{node.get('id')} has no DOM node")
        r = await self.scroll_into_view(be)
        if not r or r[2] <= 0:
            raise ValueError(f"{node.get('id')} is not visible")
        d = self.b.dpr
        # aim inside the box, biased to the centre like a person
        x = (r[0] + r[2] * random.uniform(0.35, 0.65)) * d
        y = (r[1] + r[3] * random.uniform(0.35, 0.65)) * d
        self.emit('click', f"click {node.get('id')} \"{(node.get('name') or '')[:40]}\" at ({int(x)},{int(y)})", x=int(x), y=int(y))
        await self.click_xy(x, y)
        return r

    async def type_text(self, text, wpm=None):
        """per-character delays drawn around a typing speed, slower after spaces/punctuation"""
        cps = (wpm or random.uniform(170, 260)) * 5 / 60
        for ch in text:
            if ch == '\n':
                self._xdo('key', 'Return')
            else:
                self._xdo('type', '--delay', '0', '--', ch)
            base = 1 / cps
            await asyncio.sleep(random.uniform(0.5, 1.4) * base + (0.12 if ch in ' .,' else 0))

    async def key(self, combo):
        self._xdo('key', combo)
        await self.pause(0.1, 0.3)

    async def type_into(self, node, text, clear=True, submit=False):
        await self.click(node)
        self.emit('type', f"type \"{text[:60]}\" into {node.get('id')}" + (' + Enter' if submit else ''))
        await self.pause(0.15, 0.4)
        if clear:
            await self.key('ctrl+a')
            await self.key('BackSpace')
        await self.type_text(text)
        if submit:
            await self.pause(0.2, 0.5)
            await self.key('Return')

    # ---------- raw input from the live view (phone/desktop takeover) ----------
    async def remote(self, ev):
        t = ev.get('type')
        cb, self.on_event = self.on_event, None  # the user's own input is logged once, by the server
        try:
            await self._remote(ev, t)
        finally:
            self.on_event = cb

    async def _remote(self, ev, t):
        if t == 'tap':
            await self.click_xy(ev['x'], ev['y'])
        elif t == 'scroll':
            self._xdo('mousemove', int(ev.get('x', self.x)), int(ev.get('y', self.y)))
            await self.wheel(ev['ticks'])
        elif t == 'text':
            self._xdo('type', '--delay', '40', '--', ev['text'])
        elif t == 'key':
            self._xdo('key', ev['key'])
