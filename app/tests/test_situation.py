"""v2 situation detector check: python3 app/tests/test_situation.py (needs chromium)"""
import sys, asyncio, glob, os
 H = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, os.path.join(H, '..', '..', 'agentmap')); sys.path.insert(0, os.path.join(H, '..'))
import agentmap as AM, forms, decide, websockets
async def cap(url):
    p, ws_url = AM.launch()
    try:
        async with websockets.connect(ws_url, max_size=None) as ws:
            c = AM.CDP(ws); rt = asyncio.create_task(c.reader())
            await c.send('Page.enable'); await c.send('Page.navigate', url=url); await asyncio.sleep(1.2)
            doc = await c.send('DOM.getDocument', depth=-1, pierce=True)
            ax = (await c.send('Accessibility.getFullAXTree'))['nodes']
            snap = await c.send('DOMSnapshot.captureSnapshot', computedStyles=[], includeDOMRects=True); snap['_scale']=1.0
            rt.cancel()
            m = AM.AgentMap(dict(url=url, title='t', ax=ax, snap=snap, shot=None)); m.dom = doc; m.viewport={'pageY':0,'clientHeight':900}
            return m
    finally: p.kill()
async def main():
    for f in sorted(glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures_v2', '*.html'))):
        m = await cap('file://'+f)
        fields, _ = forms.extract(m, m.viewport)
        cards = decide.build_cards(m, fields)
        s = decide.situation(m, fields, cards)
        print(os.path.basename(f).ljust(18), s['kind'].ljust(8), s['why'], (s['card']['id'] if s.get('card') else s.get('fields')))
asyncio.run(main())
