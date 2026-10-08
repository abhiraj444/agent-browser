"""LLM agent loop + handoff + replay, on one headed browser.

query -> search (API first, browser Google fallback) -> observe/act loop.
After every snapshot the blocker detector runs; on a block the task pauses with the browser untouched, the user is
notified and gets a live view; when the blocker clears (auto-detected) or the user taps Resume, the agent re-snapshots
and continues from the same step. The pause is logged as a human_takeover step in the flow.
"""
import asyncio, json, os, re, random, time, traceback, urllib.parse, urllib.request, uuid

import llm
import captcha
import facts as F
import forms
import marks
import downloads as DL
from blockers import detect
from flow import Flow, resolve
from search import api_search, google_url, results_from_map

SYSTEM = """You control a real web browser to achieve the user's GOAL. Each turn you get a compact PAGE MAP:
- controls look like [lnk:some-id] link "Name" or inline [Name](lnk:some-id); use the id exactly (e.g. lnk:some-id)
- folded regions look like ▸ [r:region-id] ... ; open one with the expand action to see inside
Reply with ONE JSON object and nothing else:
{"thought": "<one short sentence>", "action": "<click|type|fill_form|ask_user|goto|search|scroll|expand|back|key|wait|save_page_pdf|done>",
 "id": "<control or region id>", "text": "<text to type>", "submit": true|false, "url": "<url>",
 "query": "<search query>", "key": "<for key: Escape|Return|Tab|PageDown>", "direction": "<down|up>", "answer": "<final answer for done>",
 "remember": "<optional: a NEW fact worth keeping for later steps, e.g. 'notice PDF says CBT 12-15 Nov 2026'>"}
You are called fresh every step: everything you know comes from GOAL, USER INSTRUCTIONS, NOTES (facts you saved with
"remember"), PAGES VISITED and HISTORY. Use "remember" whenever you see something useful that may scroll out of view.
ANSWER FIRST, BUT VERIFIED: when the GOAL is a question (a date, a number, a fact), your job is a correct ANSWER, not browsing.
On a search results page, Google's AI Overview is only a HINT: it is often wrong or outdated, so never answer from it alone.
Read the WHOLE results list (titles, URLs and snippets of every organic result; scroll down or expand to see more) and pick
the most authoritative ones (official sites first, then reputable news or reference sites). Open at least one of them and confirm
the answer on that page before replying done; when sources disagree or the page is unclear, check a second result.
In done, give the answer and the source page you confirmed it on. If a site does not give it within 2-3 steps, go back to the
results and try the next good result instead of wandering. On the LAST STEP always reply done with your best answer so far.
USER INSTRUCTIONS, when present, come from the person watching live: follow them over your own approach, but keep the same GOAL.
ADS AND POPUPS: when an ad, cookie banner, vignette or popup covers the page, close it (click its close/X/"Close ad"/"Skip"
control, or use key Escape), or go back if a full-page ad opened. Never click ads. When a SCREENSHOT is attached, trust it over the
map for what is really on screen, but still act only with ids that exist in the PAGE MAP.
Rules: one action per turn. Prefer official and original sources (e.g. en.wikipedia.org over copies on scribd/pdf sites). Prefer clicking what is on the page over typing URLs. Never click through a bot check (reCAPTCHA,
hCaptcha, Cloudflare): a human handles those. A simple text captcha (letters in an image next to a "captcha" field) is
fine: ask_user for it with type "captcha" and the executor reads the image itself, asking the person only if it can't. When the goal is reached, use done with a short answer saying what you see.
FORMS AND PERSONAL VALUES: the user's personal values are FACTS, listed by key with sensitive ones hidden from you
(e.g. aadhaar_number = ••••9494). Never type a masked value and never invent personal data. To fill fields use
{"action": "fill_form", "fields": [{"id": "<field id from FORM FIELDS>", "key": "<fact key>"}, ...]} - one entry per field,
all fields of the visible form in one go; for a value that is not personal (a choice like "Yes" or a search word) use
{"id": ..., "value": "..."} instead of key. Use a NEW key (e.g. "date_of_birth", "full_name") when the page needs a
value you do not have: the executor asks the user for all missing ones at once. To ask yourself, use
{"action": "ask_user", "reason": "<one sentence>", "fields": [{"key": "...", "label": "<the site's label>", "id": "<field id
if any>", "type": "text|number|date|select|radio|checkbox|email|tel|password|otp|captcha", "options": ["..."],
"sensitive": true|false, "why": "<short>"}]}. ALWAYS ask_user for one-time codes (OTP) and captcha text; never guess
them yourself. In type, write {{key}} to type a fact. After fill_form read its result: fix mismatches and
the page's error messages before moving on. Steps on the way are pressed without asking: Send OTP, Get OTP, Verify,
Next, Continue, Search, Login, Download. Only a final irreversible submit (submit application, pay, place order) is
confirmed with the user, automatically by the executor.
If the user skipped a required value, do not make it up: explain in done what is missing.
FILES: DOWNLOADED FILES lists what this task saved. A completed download that matches the goal is evidence the goal is
reached: reply done and name the files. If a PDF is password-protected, say so and quote the password rule the site
states (e.g. UIDAI e-Aadhaar: first 4 letters of the name in capitals + year of birth) but never work out or write the
password itself. On a receipt or confirmation page the user may want to keep, use save_page_pdf."""


class Task:
    def __init__(self, query, goal=None, mode='agent', flow_path=None, force_block=False, max_steps=12, engine='v1'):
        self.id = uuid.uuid4().hex[:8]
        self.engine = engine if engine in ('v1', 'v2') else 'v1'   # v2 = local ranker + fast path (decide.py)
        self.facts = F.Facts()
        self._raw = (goal or query) if mode != 'replay' else ''   # read once by intake, then dropped
        self.query = F.mask_text(query)
        self.goal = F.mask_text(goal or query)
        self.search_text = ''
        self.input_req = None       # what the agent is asking you for (status needs_input)
        self.input_evt = asyncio.Event()
        self.input_result = None
        self.files = []             # downloaded / saved files (metadata only)
        self.filled_urls = set()    # pages where the agent filled a form (a final submit there needs your OK)
        self.saved_pdf_urls = set()
        self.last_target = None
        self.fields = []            # form fields read on the current page
        self.mode, self.flow_path, self.force_block, self.max_steps = mode, flow_path, force_block, max_steps
        self.status, self.answer, self.error = 'queued', None, None
        self.steps, self.logs = [], []
        self.blocker = None
        self.resume_evt = asyncio.Event()
        self.created = time.time()
        self.flow = Flow(self.goal[:60] or 'task', self.query)
        self.flow_file = None
        self.url = self.title = ''
        self.shot = None
        self.events = []            # the full action stream, shown live to the user
        self.pause_req = False      # user asked to pause / take over
        self.stop_req = False       # user asked to stop
        self.user_note = ''         # note the user typed when resuming
        self.guidance = []          # standing instructions typed live by the user (kept for the whole task)
        self.notes = []             # facts the model chose to "remember" (its working memory across calls)
        self.visited = []           # (title, url) of every page seen, in order
        self.guide_rev = 0
        self.aio = None             # the asyncio task running this Task (cancelled on Stop)
        self.acked_url = None       # user tapped Resume with the blocker still there: don't hand off again for it

    def log(self, s, kind='log', **data):
        s = self.facts.redact(s)  # personal values never reach logs, the event stream or the console
        self.logs.append(f"{time.strftime('%H:%M:%S')} {s}")
        self.events.append(dict(seq=len(self.events), t=round(time.time(), 2), kind=kind, text=s, **data))
        print(f'[{self.id}] {s}', flush=True)

    def add_guidance(self, text, emit=True):
        text = ' '.join(text.split())[:400]
        if not text:
            return
        self.guidance.append(text)
        self.guide_rev += 1
        self.flow.add('guidance', text=text)  # kept as a record; the replayer skips it
        self.emit('user', f'you told the agent: {text}' if emit else f'your resume note is now a standing instruction: {text}')

    def emit(self, kind, text, **data):
        self.log(text, kind, **data)

    def public(self):
        return dict(id=self.id, query=self.query, goal=self.facts.display(self.goal), mode=self.mode, status=self.status, answer=self.answer,
                    error=self.error, steps=self.steps[-40:], logs=self.logs[-60:], blocker=self.blocker, url=self.url,
                    title=self.title, flow=self.flow_file, created=self.created, paused=self.status == 'paused',
                    nevents=len(self.events), guidance=self.guidance, notes=self.notes, search=self.search_text,
                    input_req=self.input_req, facts=self.facts.public(), files=self.public_files(),
                    max_steps=self.max_steps, engine=self.engine, engine_stats=getattr(self, 'engine_stats', None))

    def public_files(self):
        out = []
        for f in self.files:
            if DL.resolve(self.id, f['name']):
                out.append({k: f[k] for k in ('name', 'size', 'mime', 'time', 'encrypted') if k in f})
        return out

    def add_file(self, rec):
        self.files.append(rec)
        kind = 'password-protected PDF' if rec.get('encrypted') else rec['mime']
        self.emit('file', f"saved file {rec['name']} ({kind}, {rec['size'] // 1024 + 1} KB)", file=rec['name'])

    def files_note(self):
        if not self.public_files():
            return ''
        return '\nDOWNLOADED FILES (saved for the user):\n' + '\n'.join(
            f"- {f['name']} ({f['mime']}, {f['size'] // 1024 + 1} KB{', PASSWORD-PROTECTED' if f.get('encrypted') else ''})"
            for f in self.public_files())


FINAL_BTN = re.compile(r'\b(submit|pay|payment|place (the )?order|final submit|proceed to pay|make payment|book now|buy now|'
                       r'register|apply|save and submit|lock|e-?sign)\b', re.I)
# steps on the way to a result: never worth interrupting the person for
STEP_BTN = re.compile(r'\b(otp|send (the )?code|get code|verify|validate|next|continue|search|log ?in|sign ?in|check|'
                      r'resend|refresh|download|view|show|go|captcha|get details|proceed)\b', re.I)
MONEY_BTN = re.compile(r'\b(pay|payment|place (the )?order|buy|book now|checkout|purchase)\b', re.I)
TEXTISH = {'text', 'email', 'tel', 'number', 'password', 'search', 'url', 'textarea', 'datetime-local', 'time', 'month'}


class Runner:
    def __init__(self, browser, actor, public_url=''):
        self.b, self.a, self.public_url = browser, actor, public_url
        self.tasks, self.queue = {}, asyncio.Queue()
        self.current = None
        self.restart_pending = False
        self.downloads = DL.Downloads()
        self.b.on_browser_event = self.downloads.on_event
        self.takeover_be = None     # the field the person last tapped in the live view

    def submit(self, task):
        self.tasks[task.id] = task
        self.queue.put_nowait(task)
        return task

    async def worker(self):
        while True:
            t = await self.queue.get()
            if t.status == 'cancelled':
                continue
            if self.restart_pending:  # an extension was added mid-task: restart Chrome now, between tasks
                self.restart_pending = False
                try:
                    await self.b.restart()
                except Exception as e:
                    t.log(f'browser restart failed: {e}')
            self.current = t
            t.status = 'running'
            try:  # a stopped task may have left extra tabs open (user clicks during takeover); start clean
                await self.b.reset_tabs(blank=(t.mode != 'replay'))
            except Exception as e:
                t.log(f'tab reset failed: {e}')
            try:
                self.downloads.task = t
                if not await self.b.set_download_dir(DL.task_dir(t.id)):
                    t.log('downloads are not captured for this task (no browser session)')
            except Exception as e:
                t.log(f'download setup failed: {e}')
            try:
                if t.mode == 'replay':
                    coro = self.replay(t)
                elif t.engine == 'v2':
                    import decide
                    coro = decide.run_v2(self, t)
                else:
                    coro = self.run(t)
                t.aio = asyncio.create_task(coro)
                await t.aio
            except asyncio.CancelledError:
                t.status, t.answer = 'stopped', t.answer or f'Stopped by you at {t.title or t.url}'
                t.emit('stopped', 'STOPPED by user')
            except Exception as e:
                t.status, t.error = 'failed', t.facts.redact(f'{type(e).__name__}: {e}')
                t.log(traceback.format_exc()[-600:])
            finally:
                t.input_req = None
                try:
                    if t.mode != 'replay':  # a replay never overwrites the flow it replays
                        t.flow_file = os.path.basename(t.flow.save())
                except Exception as e:
                    t.log(f'could not save the flow: {type(e).__name__}: {e}')
                await asyncio.sleep(1.5)  # a download that completes as the task ends still lands on it
                self.downloads.task = None
                t.facts.drop_ephemeral()  # OTPs, passwords and captcha answers do not outlive the task
                t._raw = ''
                if t.public_files():
                    n = len(t.public_files())
                    notify('Your files are ready', f"{n} file{'s' if n > 1 else ''} from: {t.goal[:80]}",
                           f'{self.public_url}/live/{t.id}#files')
                self.current = None

    # ---------------- user control ----------------
    async def checkpoint(self, t, step_no, history=None):
        """between actions: honour Stop, and Pause/Take over (the user drives; we resume from wherever they leave it)"""
        if t.stop_req:
            raise asyncio.CancelledError()
        if not t.pause_req:
            return False
        u0, ti0 = await self.b.location()
        t.status, t.resume_evt = 'paused', asyncio.Event()
        t.emit('paused', f'PAUSED by user at {ti0[:60]}. You have control.', url=u0)
        since = time.time()
        while not t.resume_evt.is_set():
            if t.stop_req:
                raise asyncio.CancelledError()
            try:
                await asyncio.wait_for(t.resume_evt.wait(), 1.0)
            except asyncio.TimeoutError:
                pass
        await self.b.follow_new_tab()
        u1, ti1 = await self.b.location()
        note = t.user_note.strip()
        t.pause_req, t.user_note, t.status = False, '', 'running'
        waited = round(time.time() - since, 1)
        t.flow.add('human_takeover', reason='user_pause', url=u0, left_at=u1, waited_s=waited, note=note, step=step_no)
        t.steps.append(dict(n=step_no, action='human_takeover', detail=f'you had control {waited}s, left it at {ti1[:60]}'))
        t.emit('resumed', f'RESUMED from {ti1[:60]}' + (f' with your note: {note}' if note else ''), url=u1)
        if history is not None:
            history.append(f'(the USER took over for {waited}s and left the browser at "{ti1}" <{u1[:100]}>'
                           + (f'; user note: {note}' if note else '') + '; continue from this page)')
        return True

    async def request_input(self, t, fields, reason, kind='input', summary=None):
        """status needs_input: the dashboard and live view show a form (one row per key). Waits for
        submit / skip / approve / reject, or for 'I'll fill it myself' (takeover) followed by Resume.
        Returns dict(action, values, skipped)."""
        rid = uuid.uuid4().hex[:6]
        t.input_req = dict(id=rid, kind=kind, reason=reason, fields=fields, summary=summary or [], since=time.time(),
                           url=t.url, title=t.title, live_url=f'{self.public_url}/live/{t.id}', takeover=False)
        t.input_evt, t.input_result = asyncio.Event(), None
        t.resume_evt = asyncio.Event()
        prev, t.status = t.status, 'needs_input'
        what = 'Confirm' if kind == 'confirm' else 'Needs your input'
        t.emit('needs_input', f"{what.upper()}: {reason}", fields=[f['key'] for f in fields])
        labels = ', '.join(f.get('label') or f['key'] for f in fields)[:160]
        notify(f'Agent: {what.lower()}', reason + (f' ({labels})' if labels else ''), t.input_req['live_url'])
        deadline = time.time() + 30 * 60
        res = None
        while time.time() < deadline:
            if t.stop_req:
                raise asyncio.CancelledError()
            try:
                await asyncio.wait_for(t.input_evt.wait(), 1.0)
                res = t.input_result or {}
                break
            except asyncio.TimeoutError:
                pass
            if t.input_req.get('takeover') and t.resume_evt.is_set():
                res = dict(action='takeover_done', values={}, skipped=[])
                break
        if res is None:
            t.input_req, t.status = None, prev
            raise RuntimeError('nobody answered the input request within 30 minutes')
        if t.input_req.get('takeover'):
            await self.b.follow_new_tab()
        t.input_req, t.status = None, 'running'
        for f in fields:
            k = f['key']
            if k in (res.get('values') or {}) and str(res['values'][k]).strip() != '':
                t.facts.set(k, res['values'][k], label=f.get('label', ''), sensitive=f.get('sensitive'),
                            type_=f.get('type', 'text'))
            elif k in (res.get('skipped') or []):
                t.facts.skip(k, f.get('label', ''))
        act = res.get('action', 'submit')
        got = [f['key'] for f in fields if t.facts.has(f['key'])]
        t.emit('resumed', {'takeover_done': 'you filled it yourself; the agent reads the page again',
                           'approve': 'you approved', 'reject': 'you said no'}.get(act, 'thanks: got ' + (', '.join(got) or 'nothing')))
        t.flow.add('human_input', ask=kind, keys=[f['key'] for f in fields], action=act)
        return res

    # ---------------- observe + handoff ----------------
    async def observe(self, t, step_no):
        shot = f'/tmp/agent_{t.id}.png'
        t0 = time.time()
        m = await self.b.snapshot(shot)
        t.url, t.title = m.cap['url'], m.cap['title']
        t.shot = shot
        t.emit('snapshot', f"snapshot {m.cap['title'][:50]} ({len(m.by_id)} ids, {time.time() - t0:.1f}s)", url=t.url)
        await self.capture_pdf_view(t)
        blk = await detect(self.b, m, llm_classify=lambda mm: self.classify(mm, shot, t))
        if blk and blk['url'] == t.acked_url:
            t.emit('log', f"{blk['label']} still showing; you chose to continue, so the agent works around it")
            blk = None
        if blk and blk['kind'] == 'otp' and t.mode != 'replay':
            blk = None  # the agent asks for the code with a proper form (ask_user) instead of a raw takeover
        if blk:
            await self.handoff(t, blk, step_no)
            m = await self.b.snapshot(shot)
            t.url, t.title = m.cap['url'], m.cap['title']
        return m

    async def capture_pdf_view(self, t):
        """a PDF that opened in Chrome's viewer instead of downloading: keep a copy as a file"""
        rid, mime = self.b.current_doc(t.url)
        if 'pdf' not in mime or t.url in t.saved_pdf_urls:
            return
        t.saved_pdf_urls.add(t.url)
        name = DL.safe_name(urllib.parse.unquote(urllib.parse.urlparse(t.url).path.rsplit('/', 1)[-1]) or 'document.pdf')
        if not name.lower().endswith('.pdf'):
            name += '.pdf'
        data = None
        try:
            data = await self.b.response_body(rid)
        except Exception:
            pass
        if not data or not data.startswith(b'%PDF'):
            try:
                ck = await self.b.cookie_header(t.url)
                data = await asyncio.to_thread(_fetch, t.url, ck)
            except Exception as e:
                t.log(f'could not save the PDF shown on screen: {e}')
                return
        if data and data.startswith(b'%PDF'):
            t.add_file(await asyncio.to_thread(DL.save_bytes, t.id, name, data, t.url, 'application/pdf'))

    async def classify(self, m, shot, t):
        try:
            page = t.facts.redact(m.render_focused(700, getattr(m, 'viewport', None)))
            txt, used = await asyncio.to_thread(llm.chat, [
                {'role': 'user', 'content': 'Is this web page a bot check, captcha, login wall or block page that a human '
                 'must clear before an automated agent can continue? Judge from the screenshot AND the page structure '
                 'below. Reply JSON {"blocked": true|false, "what": "<5 words>"}\n\nPAGE MAP (trimmed):\n' + page}],
                'vision', 120, 40, open(shot, 'rb').read(), t.log)
            t.log(f'blocker check sent: map {len(page)} chars + screenshot')
            v = llm.parse_json(txt)
            return v.get('what') or 'A check that needs a person' if v.get('blocked') else None
        except Exception as e:
            t.log(f'classifier skipped: {e}')
            return None

    async def handoff(self, t, blk, step_no):
        """pause with the browser exactly as it is, until the blocker is gone or the user taps Resume"""
        d = self.b.dpr
        box = blk.get('box')
        blk['screen_box'] = [round(v * d) for v in box] if box else None
        blk['screen'] = [round(self.b.width * d), round(self.b.height * d)]
        blk['since'] = time.time()
        blk['live_url'] = f"{self.public_url}/live/{t.id}"
        t.blocker, t.status = dict(blk), 'needs_you'
        t.resume_evt.clear()
        t.emit('blocked', f"BLOCKED: {blk['label']} at {blk['url'][:80]}", box=blk.get('screen_box'))
        notify(f"Agent needs you: {blk['label']}", blk['needs'], blk['live_url'])
        url0 = blk['url']
        how = 'timeout'
        deadline = time.time() + 30 * 60
        while time.time() < deadline:
            if t.stop_req:
                raise asyncio.CancelledError()
            try:
                await asyncio.wait_for(t.resume_evt.wait(), 2.5)
                how = 'user_resume'
                break
            except asyncio.TimeoutError:
                pass
            try:
                m = await self.b.snapshot()
                again = await detect(self.b, m)
            except Exception:
                continue
            if again is None and (blk.get('auto_clear') or m.cap['url'] != url0):
                how = 'auto_detected'
                break
            if again and again.get('box') != t.blocker.get('box'):
                bb = again.get('box')
                t.blocker['screen_box'] = [round(v * d) for v in bb] if bb else None
        waited = round(time.time() - blk['since'], 1)
        t.emit('resumed', f'RESUMED ({how.replace("_", " ")}) after {waited}s')
        if how == 'user_resume':
            try:
                still = await detect(self.b, await self.b.snapshot())
            except Exception:
                still = None
            t.acked_url = still['url'] if still else None
            t.pause_req = False  # Resume means "carry on", even if Pause was also tapped while blocked
        t.flow.add('human_takeover', blocker=blk['kind'], label=blk['label'], url=url0, cleared_by=how, waited_s=waited,
                   step=step_no)
        t.steps.append(dict(n=step_no, action='human_takeover', detail=f"{blk['label']} cleared ({how}, {waited}s)"))
        t.blocker, t.status = None, 'running'
        if how == 'timeout':
            raise RuntimeError('nobody cleared the blocker within 30 minutes')

    # ---------------- search ----------------
    async def do_search(self, t, q, step_no):
        q = F.scrub(t.facts.redact(q))[0] or t.search_text or 'search'  # only safe text ever reaches a search engine
        prov, res = await asyncio.to_thread(api_search, q, t.log)
        if res:
            t.log(f'search via {prov}: {len(res)} results')
            t.flow.add('search', query=q, provider=prov)
            lines = '\n'.join(f"{i + 1}. {r['title']} <{r['url']}>" for i, r in enumerate(res[:8]))
            return f'Search results for "{q}" ({prov}):\n{lines}\nUse goto with one of these URLs.'
        t.log(f'search via browser Google: "{q}"')
        await self.b.goto(google_url(q))
        t.flow.add('search', query=q, provider='browser_google')
        try:
            res = results_from_map(await self.b.snapshot(), limit=10)
        except Exception:
            res = []
        if res:
            lines = '\n'.join(f"{i + 1}. {r['title']} [{r['id']}]" for i, r in enumerate(res))
            return (f'Opened Google results for "{q}". Organic results (the AI Overview is only a hint; '
                    f'verify on one of these):\n{lines}')
        return f'Opened Google results for "{q}".'

    # ---------------- intake ----------------
    async def intake(self, t):
        """raw request -> safe search text + goal with {{key}} placeholders + facts; bad formats are confirmed first"""
        raw = t._raw
        t._raw = ''
        search, goal, found = await asyncio.to_thread(F.intake, raw, llm.chat, llm.parse_json, None)
        bad = []
        for f in found:
            k = t.facts.set(f['key'], f['value'], label=f.get('label') or '', sensitive=f.get('sensitive'), source='request')
            why = F.problem(k, t.facts.get(k))
            if why:
                bad.append(dict(key=k, label=t.facts.d[k]['label'].capitalize(), type='number' if F.FORMATS.get(k, ('', ''))[1].endswith('digits') else 'text',
                                sensitive=True, why=f"The {t.facts.d[k]['label']} you gave ({F.mask(t.facts.get(k))}) {why}. "
                                                  'Type it again, or skip to keep it.'))
        t.search_text = search
        t.goal = t.facts.redact(goal)
        t.flow.d['name'] = re.sub(r'\{\{|\}\}', '', t.goal)[:60] or 'task'
        t.flow.d['query'] = search
        t.log(f'goal: {t.goal}')
        t.log(f'search text: "{search}"' + (f"; facts kept in this task only: " +
                                            ', '.join(f"{x['key']}={x['masked']}" for x in t.facts.public()) if found else ''))
        if bad:
            res = await self.request_input(t, bad, 'Please check this before the agent starts.')
            for b in bad:
                if b['key'] in (res.get('skipped') or []):
                    t.facts.d[b['key']]['skipped'] = False  # "skip" here means keep what was given
            for b in bad:
                still = F.problem(b['key'], t.facts.get(b['key']))
                if still:
                    t.log(f"{b['label']} {still}; continuing with what you gave")

    # ---------------- agent loop ----------------
    async def run(self, t):
        await self.intake(t)
        history = []
        if t.force_block:
            # demo: meet a real reCAPTCHA before searching (Google's own demo page)
            t.log('demo: forcing a bot check (reCAPTCHA demo page)')
            await self.b.goto('https://www.google.com/recaptcha/api2/demo')
            t.flow.add('goto', url='https://www.google.com/recaptcha/api2/demo', note='forced blocker for the demo')
            await self.observe(t, 0)
        url_in_goal = re.search(r'https?://[^\s"\'<>]+', t.goal)
        if url_in_goal:
            await self.b.goto(url_in_goal.group(0))
            t.flow.add('goto', url=url_in_goal.group(0))
            history.append(f'goto {url_in_goal.group(0)} (the address given in the goal) -> ok')
            extra = ''
        else:
            note = await self.do_search(t, t.search_text, 0)
            history.append(f'search "{t.search_text}" -> {note.splitlines()[0]}')
            extra = note if 'Search results' in note else ''
        role, fails, last_sig = 'plan', 0, None
        need_vision, last_view = '', None
        for n in range(1, t.max_steps + 1):
            await self.checkpoint(t, n, history)
            m = await self.observe(t, n)
            if t.user_note.strip():
                t.add_guidance(t.user_note.strip(), emit=False)
                t.user_note = ''
            raw_page = m.render_focused(2400, getattr(m, 'viewport', None), focus_ids=[t.last_target])
            page = t.facts.redact(raw_page)
            on_screen_secret = page != raw_page
            try:
                fields, errs = forms.extract(m, getattr(m, 'viewport', None))
            except Exception as e:
                fields, errs = [], []
                t.log(f'form read failed: {type(e).__name__}: {e}')
            t.fields = fields
            form_block = forms.render(fields, errs, t.facts.redact)
            rev = t.guide_rev
            guide = ('\nUSER INSTRUCTIONS (newest last, follow them):\n' + '\n'.join(f'- {g}' for g in t.guidance[-6:])) if t.guidance else ''
            last = '\nTHIS IS THE LAST STEP: reply done with your best answer.' if n == t.max_steps else ''
            if not t.visited or t.visited[-1][1] != t.url:
                t.visited.append((t.title[:60], t.url[:120]))
            notes = ('\nNOTES (what you learned so far):\n' + '\n'.join(f'- {x}' for x in t.notes[-12:])) if t.notes else ''
            factl = t.facts.for_llm()
            facts_block = f'\nFACTS (the user\'s values; use their keys):\n{factl}' if factl else ''
            seen = '\nPAGES VISITED: ' + ' > '.join(f'{ti or u}' for ti, u in t.visited[-10:])
            older = history[:-8]
            hist = ((f'(earlier: {len(older)} steps: ' + '; '.join(h.split(' -> ')[0][:50] for h in older[-12:]) + ')\n') if older else '') + '\n'.join(history[-8:])
            msg = (f"GOAL: {t.goal}{guide}\nSTEP {n}/{t.max_steps}{last}{facts_block}{notes}{t.files_note()}{seen}\nHISTORY:\n" +
                   t.facts.redact(hist) + (f'\n\n{extra}' if extra else '') + f"\n\nPAGE MAP:\n{page}" +
                   (f"\n\nFORM FIELDS (visible inputs with their current values; ids work with fill_form, click and type):\n{form_block}"
                    if form_block else ''))
            extra = ''
            img = None
            if need_vision and t.shot and os.path.exists(t.shot):
                if on_screen_secret:
                    t.emit('log', 'not sending a screenshot: a personal value is on screen')
                else:
                    tagged = f'/tmp/agent_{t.id}_marks.png'
                    try:
                        legend = marks.draw(m, tagged)
                        img = open(tagged, 'rb').read()
                        tags = ', '.join(f'{i}={x}' for i, x in legend)
                        msg += ('\n\nSCREENSHOT ATTACHED (numbered tags on controls; TAGS -> PAGE MAP ids: ' + tags + '). '
                                'Your last attempt failed or went nowhere (' + need_vision + '). Use the screenshot AND the '
                                'PAGE MAP together: is an ad, popup or overlay in the way? Is the thing you want visible? '
                                'Then act with a PAGE MAP id (a tag number tells you which id a box is).')
                    except Exception as e:
                        t.log(f'set-of-marks failed ({e}); sending the plain screenshot')
                        img = open(t.shot, 'rb').read()
                        msg += ('\n\nSCREENSHOT ATTACHED: your last attempt failed or went nowhere (' + need_vision +
                                '). Use it with the PAGE MAP above and pick a control id that exists in the PAGE MAP.')
                    t.emit('log', f'asking the vision model for help with a screenshot ({need_vision})')
            need_vision = ''
            t.log(f'llm request: {len(msg)} chars (page map {len(page)}, {len(fields)} form fields), image: {"yes" if img else "no"}')
            try:
                try:
                    txt, used = await asyncio.to_thread(llm.chat, [{'role': 'system', 'content': SYSTEM},
                                                                   {'role': 'user', 'content': msg}], role, 900, 75, img, t.log)
                except Exception:
                    if img is None:
                        raise
                    t.log('vision help failed; continuing with text only')
                    txt, used = await asyncio.to_thread(llm.chat, [{'role': 'system', 'content': SYSTEM},
                                                                   {'role': 'user', 'content': msg}], role, 900, 75, None, t.log)
                act = llm.parse_json(txt)
            except Exception as e:
                fails += 1
                t.log(f'llm error: {e}')
                role = 'escalate' if fails >= 2 else 'plan'
                if fails >= 4:
                    raise
                continue
            if await self.checkpoint(t, n, history):
                continue  # the page changed under the user's hands: drop this plan and look again
            if t.guide_rev != rev:
                t.emit('log', 'new instruction from you arrived while thinking: replanning with it')
                continue
            t.emit('think', f"[{used.split('/')[-1]}] {str(act.get('thought', ''))[:160]}", model=used,
                   action=json.loads(t.facts.redact(json.dumps(act))))
            rem = ' '.join(str(act.get('remember') or '').split())[:240]
            if rem and rem not in t.notes:
                t.notes.append(t.facts.redact(rem))
                t.emit('log', f'remembered: {rem}')
            sig = json.dumps({k: act.get(k) for k in ('action', 'id', 'url', 'text', 'fields')})
            repeating = sig == last_sig
            last_sig = sig
            a = (act.get('action') or '').lower()
            t.last_target = act.get('id') or ((act.get('fields') or [{}])[0].get('id') if isinstance(act.get('fields'), list) and act.get('fields') and isinstance(act['fields'][0], dict) else None)
            what = act.get('id') or act.get('url') or act.get('query') or ''
            if a == 'fill_form':
                what = ', '.join(f"{x.get('id')}<-{x.get('key') or 'value'}" for x in act.get('fields') or [] if isinstance(x, dict))
            elif a == 'ask_user':
                what = ', '.join(str(x.get('key')) for x in act.get('fields') or [] if isinstance(x, dict))
            t.emit('act', f"step {n}: {a} {what}" + (f" \"{act.get('text')}\"" if a == 'type' else ''))
            result = await self.execute(t, m, act, n)
            result = t.facts.redact(result)
            t.steps.append(dict(n=n, action=a, detail=result[:200], thought=t.facts.redact(str(act.get('thought', '')))[:160], model=used))
            t.emit('result', f'  → {result[:220]}')
            history.append(f"{n}. {a} {what[:120]} -> {result[:300 if a in ('fill_form', 'ask_user') else 120]}")
            if result.startswith('EXPANDED'):
                extra = result
            if a == 'done':
                ans = t.facts.redact(str(act.get('answer') or result))
                files = t.public_files()
                if files and not all(f['name'] in ans for f in files):
                    ans += ' Files: ' + ', '.join(f['name'] + (' (password-protected)' if f.get('encrypted') else '') for f in files) + '.'
                t.answer = ans
                t.flow.add('ai_check', question=f'Is this goal achieved on this page: {t.goal}?', expect='yes', url=t.url)
                t.status = 'done'
                t.log(f'DONE: {t.answer}')
                return
            view = (t.url, m.viewport.get('pageY') if getattr(m, 'viewport', None) else None, len(m.by_id))
            if result.startswith('ERROR'):
                need_vision = 'error: ' + result[6:90]
            elif repeating:
                need_vision = 'you repeated the same action'
            elif a == 'scroll' and view == last_view:
                need_vision = 'scrolling did not change the page'
            last_view = view
            if result.startswith('ERROR'):
                fails += 1
            # stuck (repeating itself or erroring twice): the bigger model takes the next turn
            role = 'escalate' if repeating or fails >= 2 else 'plan'
            if not result.startswith('ERROR'):
                fails = 0
            await asyncio.sleep(random.uniform(0.4, 1.1))  # human pacing between actions
        t.status = 'stopped'
        t.answer = f'Stopped after {t.max_steps} steps at {t.title}'
        if t.public_files():
            t.answer += '. Files saved: ' + ', '.join(f['name'] for f in t.public_files())

    async def confirm_click(self, t, node):
        """a final submit / payment button after the agent filled a form: ask before pressing it"""
        name = (node.get('name') or '').strip()
        if node.get('role') not in ('button', 'link') or not FINAL_BTN.search(name):
            return True
        if STEP_BTN.search(name) and not MONEY_BTN.search(name):
            return True
        mode = (os.environ.get('CONFIRM_MODE') or 'final').lower()
        if mode == 'off' or (mode == 'money' and not MONEY_BTN.search(name)):
            return True
        if t.url not in t.filled_urls and not MONEY_BTN.search(name):
            return True
        key = (t.url, name)
        if key in getattr(t, 'confirmed', set()):
            return True
        summary = []
        for f in getattr(t, 'fields', None) or []:
            if f['type'] == 'checkbox':
                if f.get('checked'):
                    summary.append(dict(label=f.get('option_label') or f['label'], value='Yes'))
            elif f.get('value') not in (None, ''):
                summary.append(dict(label=(f['label'] or '')[:50], value=t.facts.redact(str(f['value']))[:60]))
        summary = summary[:20]
        where = t.title[:60] or urllib.parse.urlparse(t.url).netloc or t.url[:60]
        res = await self.request_input(t, [], f'Press "{name[:60]}" on {where}?', kind='confirm',
                                       summary=summary)
        if res.get('action') == 'approve':
            t.confirmed = getattr(t, 'confirmed', set()) | {key}
            return True
        if res.get('action') == 'takeover_done':
            return 'takeover'
        return False

    async def execute(self, t, m, act, n):
        a = (act.get('action') or '').lower()
        url0 = m.cap['url']
        try:
            if a in ('click', 'type'):
                node = m.by_id.get(act.get('id', ''))
                if node is None:
                    return f"ERROR no control {act.get('id')} on this page." + self.similar(m, act.get('id', ''))
                if a == 'click':
                    ok = await self.confirm_click(t, node)
                    if ok == 'takeover':
                        return 'the user took over at the confirmation and handed back; look at the page again'
                    if not ok:
                        t.add_guidance(f'Do not press "{node.get("name", "")[:60]}" yet: the user said no.', emit=False)
                        return f'the user did NOT approve pressing "{node.get("name", "")[:60]}"; do not press it'
                    await self.a.click(node)
                    t.flow.add('click', target=Flow.target(node), url=url0)
                else:
                    raw = str(act.get('text', ''))
                    if F.MASK in raw:
                        return 'ERROR you typed a masked value. Use {{key}} (e.g. {{aadhaar_number}}) or fill_form with the key.'
                    fld = self._field(t, node.get('id')) or dict(id=node.get('id'), type='text', label=node.get('name', ''))
                    if node.get('role') in ('textbox', 'combobox', 'spinbutton') and self._invented(t, fld, raw):
                        key = F.canonical_key(fld.get('label') or fld['id'], fld.get('label', ''))
                        t.log(f"not typing a made-up value into {(fld.get('label') or fld['id'])[:40]}: asking you instead")
                        raw = '{{' + key + '}}'
                    text, missing = t.facts.substitute(raw)
                    if missing:
                        res = await self.ask_missing(t, [dict(key=k, label=k.replace('_', ' ')) for k in missing], 'The page needs a value')
                        text, missing = t.facts.substitute(raw)
                        if missing:
                            return f'ERROR no value for {", ".join(missing)} (the user skipped it)'
                    await self.a.type_into(node, text, submit=bool(act.get('submit')))
                    t.flow.add('type', target=Flow.target(node), text=t.facts.placeholder(text), submit=bool(act.get('submit')), url=url0)
                    if raw != text:
                        t.filled_urls.add(url0)
                await self.settle(url0)
                u, ti = await self.b.location()
                return f'ok, now at {ti[:60]}' + (' (navigated)' if u != url0 else '')
            if a == 'fill_form':
                return await self.fill_form(t, m, act.get('fields') or [])
            if a == 'ask_user':
                return await self.ask_user(t, act)
            if a == 'save_page_pdf':
                data = await self.b.print_pdf()
                rec = await asyncio.to_thread(DL.save_bytes, t.id, (t.title or 'page')[:80] + '.pdf', data, t.url, 'application/pdf')
                t.add_file(rec)
                return f"ok, saved this page as {rec['name']}"
            if a == 'goto':
                await self.b.goto(act['url'])
                t.flow.add('goto', url=act['url'])
                return f'ok, opened {act["url"][:80]}'
            if a == 'search':
                return await self.do_search(t, act.get('query') or act.get('text') or t.search_text, n)
            if a == 'scroll':
                await self.a.wheel(6 if act.get('direction', 'down') == 'down' else -6)
                t.flow.add('scroll', direction=act.get('direction', 'down'))
                return 'ok, scrolled'
            if a == 'expand':
                return 'EXPANDED ' + t.facts.redact(m.expand(act.get('id', ''), 1500))
            if a == 'back':
                await self.b.back()
                t.flow.add('back')
                return 'ok, went back'
            if a == 'key':
                k = str(act.get('key') or act.get('text') or 'Escape')
                k = {'esc': 'Escape', 'enter': 'Return', 'pagedown': 'Next', 'pageup': 'Prior'}.get(k.lower(), k)
                if not re.fullmatch(r'[A-Za-z0-9_+]{1,20}', k):
                    return f'ERROR bad key {k}'
                await self.a.key(k)
                t.flow.add('key', key=k)
                await asyncio.sleep(0.8)
                return f'ok, pressed {k}'
            if a == 'wait':
                await asyncio.sleep(2)
                return 'ok, waited'
            if a == 'done':
                return act.get('answer', 'done')
            return f'ERROR unknown action {a}'
        except KeyError as e:
            return f'ERROR {e}'
        except Exception as e:
            hint = self.similar(m, act.get('id', '')) if 'DOM node' in str(e) else ''
            return f'ERROR {type(e).__name__}: {e}.{hint}'

    # ---------------- forms ----------------
    def _field(self, t, fid):
        for f in getattr(t, 'fields', None) or []:
            if f['id'] == fid or any(o.get('id') == fid for o in f.get('options') or [] if isinstance(o, dict)):
                return f
        return None

    def _ask_spec(self, f, key, label='', sensitive=None, typ=None, why=''):
        """one row of the needs-input form, shaped by the site's own field when we know it"""
        spec = dict(key=F.canonical_key(key, label), label=(label or (f or {}).get('label') or key.replace('_', ' '))[:80],
                    why=why[:160])
        if f:
            ft = f['type']
            spec['type'] = {'select': 'select', 'radio': 'radio', 'checkbox': 'checkbox', 'date': 'date', 'email': 'email',
                            'tel': 'tel', 'number': 'number', 'password': 'password', 'textarea': 'textarea'}.get(ft, 'text')
            if ft in ('select', 'radio'):
                spec['options'] = [o['text'] for o in f.get('options', []) if o.get('text') and not o.get('disabled')][:200]
            if f.get('inputmode') in ('numeric', 'decimal', 'tel'):
                spec['type'] = 'number' if f['inputmode'] != 'tel' else 'tel'
            if f.get('maxlength'):
                spec['maxlength'] = f['maxlength']
            spec['required'] = bool(f.get('required'))
            spec['field_id'] = f['id']
        if typ:
            spec['type'] = typ
        spec.setdefault('type', 'text')
        if re.search(r'otp|one.?time|verification code', f"{key} {spec['label']}", re.I):
            spec['type'] = 'otp'
        if re.search(r'captcha|security code|enter the (text|characters)', f"{key} {spec['label']}", re.I):
            spec['type'] = 'captcha'
        if spec['key'] in F.FORMATS and F.FORMATS[spec['key']][1].endswith('digits') and spec['type'] == 'text':
            spec['type'] = 'number'
        spec['sensitive'] = bool(sensitive) if sensitive is not None else bool(
            F.SENSITIVE_HINT.search(f"{key} {spec['label']}") or spec['type'] in ('password', 'otp'))
        return spec

    async def ask_missing(self, t, specs, reason):
        specs = [s if 'type' in s else self._ask_spec(None, s['key'], s.get('label', '')) for s in specs]
        specs = [s for s in specs if s['type'] in ('otp', 'captcha', 'password') or not t.facts.has(s['key'])]
        seen, uniq = set(), []
        for s in specs:
            if s['key'] not in seen:
                seen.add(s['key'])
                uniq.append(s)
        # simple text captchas: read the image ourselves (2 tries per page), harder ones go to the person
        tries = getattr(t, 'captcha_tries', None)
        if tries is None:
            tries = t.captcha_tries = {}
        for s in [s for s in uniq if s['type'] == 'captcha']:
            k = t.url.split('#')[0]
            if tries.get(k, 0) >= 2:
                t.log('captcha: already tried reading it twice on this page; asking you')
                continue
            tries[k] = tries.get(k, 0) + 1
            txt, why = await captcha.read(self.b, t.log)
            if txt:
                t.facts.set(s['key'], txt, label=s.get('label', ''), sensitive=False, type_='captcha')
                t.emit('log', f'captcha: read "{txt}" from the image (try {tries[k]} of 2; you are asked if it is wrong)')
                uniq.remove(s)
            else:
                t.log(f'captcha: {why}; asking you')
        if not uniq:
            return dict(action='submit', values={}, skipped=[])
        return await self.request_input(t, uniq, reason)

    async def ask_user(self, t, act):
        specs = []
        for x in act.get('fields') or []:
            if not isinstance(x, dict) or not x.get('key'):
                continue
            f = self._field(t, x.get('id')) if x.get('id') else None
            s = self._ask_spec(f, x['key'], x.get('label', ''), x.get('sensitive'), None if f else x.get('type'), x.get('why', ''))
            if not f and x.get('options'):
                s['options'] = [str(o)[:80] for o in x['options']][:200]
                s['type'] = x.get('type') if x.get('type') in ('radio', 'select') else 'select'
            specs.append(s)
        if not specs:
            return 'ERROR ask_user needs fields: [{"key": ..., "label": ...}]'
        res = await self.ask_missing(t, specs, str(act.get('reason') or 'The agent needs a few details')[:200])
        if res.get('action') == 'none':
            return 'you already have all of these as FACTS: use them with fill_form'
        if res.get('action') == 'takeover_done':
            return 'the user filled the page themselves and handed back: read FORM FIELDS again and continue'
        got = [s['key'] for s in specs if t.facts.has(s['key'])]
        skipped = [s['key'] for s in specs if not t.facts.has(s['key'])]
        return (f"the user gave: {', '.join(got) or 'nothing'}" + (f"; skipped: {', '.join(skipped)}" if skipped else '') +
                '. Now fill them with fill_form using these keys.')

    async def fill_form(self, t, m, items):
        items = [x for x in items if isinstance(x, dict) and x.get('id')]
        if not items:
            return 'ERROR fill_form needs fields: [{"id": "<field id>", "key": "<fact key>"}]'
        plan, missing, unknown = [], [], []
        for x in items:
            f = self._field(t, x['id'])
            if f is None:
                node = m.by_id.get(x['id'])
                if node is None or node.get('role') not in ('textbox', 'searchbox', 'combobox', 'checkbox', 'radio', 'spinbutton'):
                    unknown.append(x['id'])
                    continue
                f = dict(id=x['id'], be=node.get('be'), click_be=node.get('be'), type='text', label=node.get('name', ''),
                         value=node.get('value') or '', options=[], required=False, frame='')
            key = x.get('key')
            if key:
                key = F.canonical_key(key, f.get('label', ''))
                if not t.facts.has(key) and not (key in t.facts.d and t.facts.d[key].get('skipped')):
                    missing.append(self._ask_spec(f, key))
                plan.append((f, key, None))
            else:
                v = str(x.get('value', ''))
                if F.MASK in v:
                    return 'ERROR a masked value was given. Use "key" with the fact key instead of "value".'
                if self._invented(t, f, v):
                    # a free-text value the user never gave (e.g. "John Doe"): ask instead of making it up
                    key = F.canonical_key(x.get('key') or f.get('label') or f['id'], f.get('label', ''))
                    t.log(f"not typing a made-up value into {(f.get('label') or f['id'])[:40]}: asking you instead")
                    if not t.facts.has(key) and not (key in t.facts.d and t.facts.d[key].get('skipped')):
                        missing.append(self._ask_spec(f, key))
                    plan.append((f, key, None))
                    continue
                v, miss = t.facts.substitute(v)
                for k in miss:
                    missing.append(self._ask_spec(f, k))
                plan.append((f, None, x.get('value', '')))
        if missing:
            res = await self.ask_missing(t, missing, f'Details for the form on {t.title[:60] or "this page"}')
            if res.get('action') == 'takeover_done':
                return 'the user filled the form themselves and handed back: read FORM FIELDS again and continue'
            # the page may have moved while waiting: re-read the fields
            m = await self.b.snapshot()
            t.fields, _ = forms.extract(m, getattr(m, 'viewport', None))
            plan = [((self._field(t, f['id']) or f), k, v) for f, k, v in plan]
        report, done = [], []
        for f, key, literal in plan:
            label = (f.get('label') or f['id'])[:40]
            if key:
                val = t.facts.get(key)
                if val is None:
                    report.append(f'{label}: SKIPPED by the user' + (' (required!)' if f.get('required') else ''))
                    continue
            else:
                val, _ = t.facts.substitute(literal)
            try:
                await self.fill_one(t, m, f, val)
                done.append((f, key, val))
                t.flow.add('fill_field', target=Flow.target(m.by_id.get(f['id']) or dict(id=f['id'], role=f['type'], name=f.get('label'))),
                           ftype=f['type'], value='{{' + key + '}}' if key else t.facts.placeholder(val), url=t.url)
            except Exception as e:
                report.append(f'{label}: ERROR {type(e).__name__}: {str(e)[:80]}')
            await asyncio.sleep(random.uniform(0.15, 0.4))
        if done:
            t.filled_urls.add(t.url)
        # read back what the page now holds, and what it complains about
        await asyncio.sleep(0.6)
        m2 = await self.b.snapshot()
        fields2, errs = forms.extract(m2, getattr(m2, 'viewport', None))
        t.fields = fields2
        by_be = {g['be']: g for g in fields2}
        by_id = {g['id']: g for g in fields2}
        ok = 0
        for f, key, val in done:
            g = by_be.get(f['be']) or by_id.get(f['id'])
            label = (f.get('label') or f['id'])[:40]
            shown = F.mask(val) if (key and t.facts.d.get(key, {}).get('sensitive')) else f'"{val[:40]}"'
            if g is None:
                report.append(f'{label}: filled {shown} (field not visible any more to check)')
            elif forms.same_value(g, val):
                ok += 1
                report.append(f'{label}: ok' + (' (site marks it invalid)' if g.get('invalid') else ''))
            else:
                report.append(f'{label}: MISMATCH wanted {shown}, field shows "{t.facts.redact(str(g.get("value", "")))[:40]}"')
        if unknown:
            report.append('no such field: ' + ', '.join(unknown) + ' (use ids from FORM FIELDS)')
        if errs:
            report.append('PAGE ERRORS: ' + ' / '.join(t.facts.redact(e)[:120] for e in errs[:4]))
        return f'filled {ok}/{len(plan)} fields. ' + '; '.join(report)

    PERSONAL = re.compile(r'name|phone|mobile|tel|e-?mail|address|street|city|pin ?code|zip|birth|dob|aadh?aa?r|pan\b|'
                          r'passport|account|father|mother|gender|roll|registration|instruction|comment|message', re.I)

    def _invented(self, t, f, v):
        """True when a literal value for a free-text field did not come from the user (GOAL, request or instructions)"""
        v = str(v or '').strip()
        if not v or '{{' in v or f.get('type') in ('checkbox', 'radio', 'select', 'combobox', 'search', 'url'):
            return False
        if f.get('type') not in ('email', 'tel', 'password', 'date', 'textarea', 'number') and \
                not self.PERSONAL.search(f"{f.get('label', '')} {f.get('name', '')} {f.get('autocomplete', '')}"):
            return False
        said = ' '.join([t.goal, t.query, t.search_text] + t.guidance).lower()
        if v.lower() in said or any(x.get('value') == v for x in t.facts.d.values()):
            return False
        return True

    async def fill_one(self, t, m, f, val):
        """put one value into one field the way a person would"""
        typ = f['type']
        node = {'id': f['id'], 'be': f.get('click_be') or f['be'], 'name': f.get('label', '')}
        if f.get('disabled'):
            raise ValueError('the field is read-only')
        if typ == 'checkbox':
            if bool(f.get('checked')) != forms.truthy(val):
                await self.a.click(node)
            return
        if typ == 'radio':
            o = forms.match_option(f.get('options', []), val)
            if not o:
                raise ValueError(f'no option like "{val}" (options: {", ".join(x["text"] for x in f["options"])[:120]})')
            if not o.get('checked'):
                await self.a.click({'id': o['id'], 'be': o['be'], 'name': o['text']})
            return
        if typ == 'select':
            opts = [o for o in f.get('options', []) if not o.get('disabled')]
            o = forms.match_option(opts, val)
            if not o:
                raise ValueError(f'no option like "{val}" (options: {", ".join(x["text"] for x in opts)[:160]})')
            idx = opts.index(o)
            await self.a.click(node)          # opens the dropdown like a tap would
            await asyncio.sleep(0.35)
            self.a._xdo('key', '--delay', '12', '--repeat', str(len(opts) + 1), 'Up')
            if idx:
                self.a._xdo('key', '--delay', '25', '--repeat', str(idx), 'Down')
            await asyncio.sleep(0.15)
            await self.a.key('Return')
            return
        if typ == 'date':
            d = forms.norm_date(val)
            if not d:
                raise ValueError(f'"{val}" is not a date (use dd/mm/yyyy)')
            r = await self.a.scroll_into_view(node['be'])
            if not r:
                raise ValueError('date field not visible')
            dpr = self.b.dpr
            await self.a.click_xy((r[0] + min(14, r[2] / 6)) * dpr, (r[1] + r[3] / 2) * dpr)  # the day segment
            await asyncio.sleep(0.2)
            y, mo, dd = d.split('-')
            # segment order follows the browser locale (dd/mm/yyyy in en-IN, mm/dd/yyyy in en-US): read it from the
            # field's own spin buttons, left to right
            # (the segments live in the input's shadow DOM: no layout boxes, but the AX tree lists them in order)
            segs = [x for x in m.by_id.values() if isinstance(x, dict) and x.get('role') == 'spinbutton'
                    and re.search(r'\b(day|month|year)\b', (x.get('name') or '').lower())][:3]
            part = {'day': dd, 'month': mo, 'year': y}
            order = [next((v for k, v in part.items() if k in (s.get('name') or '').lower()), None) for s in segs]
            digits = ''.join(order) if len(order) == 3 and all(order) else dd + mo + y
            await self.a.type_text(digits)  # digits auto-advance from segment to segment
            await self.a.key('Escape')
            return
        if typ == 'combobox':  # a JS dropdown: type to filter, then pick the matching option
            await self.a.type_into(node, val)
            await asyncio.sleep(1.0)
            m2 = await self.b.snapshot()
            opts = [x for x in m2.by_id.values() if isinstance(x, dict) and x.get('role') in ('option', 'menuitem', 'treeitem')
                    and x.get('be')]
            hit = forms.match_option([dict(text=x.get('name') or '', value=x.get('name') or '', node=x) for x in opts], val)
            if hit:
                await self.a.click(hit['node'])
            else:
                await self.a.key('Down')
                await self.a.key('Return')
            return
        await self.a.type_into(node, val)  # text-like fields (cleared first)

    @staticmethod
    def similar(m, want):
        """clickable ids whose id or name shares words with the id the model asked for (it often guesses ids)"""
        words = [w for w in re.split(r'[^a-z0-9]+', want.lower().split(':', 1)[-1]) if len(w) > 1]
        if not words:
            return ''
        out = []
        for i, n in m.by_id.items():
            if i == want or not isinstance(n, dict):
                continue
            hay = (i + ' ' + str(n.get('name', ''))).lower()
            if all(w in hay for w in words):
                out.append(f'{i} "{str(n.get("name", ""))[:50]}"')
        return (' Did you mean one of: ' + '; '.join(out[:8]) + '. Use one exact id from the PAGE MAP.') if out else \
            ' Use an exact id from the PAGE MAP (region ids r:... cannot be clicked; expand them first).'

    async def settle(self, url0):
        """give a click time to start a navigation, then wait for it to finish"""
        for _ in range(12):
            await asyncio.sleep(0.25)
            if await self.b.follow_new_tab():
                return
            if self.b.loading:
                break
            u, _ = await self.b.location()
            if u != url0:
                break
        if self.b.loading:
            await self.b.wait_load(15, 1.0)
        else:
            await asyncio.sleep(0.6)

    # ---------------- deterministic replay ----------------
    async def replay(self, t):
        f = Flow.load(t.flow_path)
        t.goal = f.d.get('name')
        t.log(f"replaying {os.path.basename(t.flow_path)} ({len(f.d['steps'])} steps)")
        # a recorded form stores {{key}} placeholders, never values: ask for all of them up front
        keys = []
        for s in f.d['steps']:
            for k in re.findall(r'\{\{\s*([a-zA-Z0-9_]+)\s*\}\}', str(s.get('text', '')) + str(s.get('value', ''))):
                if k not in keys:
                    keys.append(k)
        if keys:
            await self.ask_missing(t, [self._ask_spec(None, k) for k in keys], 'This saved flow needs your details')
        for s in f.d['steps']:
            k = s['kind']
            await self.checkpoint(t, s['i'])
            m = await self.observe(t, s['i'])  # handoff happens here if a blocker shows up again
            if k == 'goto':
                await self.b.goto(s['url'])
                res = 'ok'
            elif k in ('guidance', 'human_input'):
                t.log(f'replay {s["i"]} {k} (record only)')
                continue
            elif k == 'search':
                await self.do_search(t, s['query'], s['i'])
                res = 'ok'
            elif k in ('click', 'type'):
                node, how = resolve(m, s.get('target'))
                if node is None:
                    raise RuntimeError(f"step {s['i']}: target {s['target']} not found")
                if k == 'click':
                    t.fields = forms.extract(m, getattr(m, 'viewport', None))[0]
                    if not await self.confirm_click(t, node):
                        raise RuntimeError(f'you chose not to press "{node.get("name")}"')
                    await self.a.click(node)
                else:
                    text, miss = t.facts.substitute(s.get('text', ''))
                    if miss:
                        raise RuntimeError(f'no value for {", ".join(miss)}')
                    await self.a.type_into(node, text, submit=s.get('submit', False))
                await self.settle(m.cap['url'])
                res = f'ok (resolved by {how})'
            elif k == 'fill_field':
                fields, _ = forms.extract(m, getattr(m, 'viewport', None))
                node, how = resolve(m, s.get('target'))
                fld = next((g for g in fields if node and (g['be'] == node.get('be') or g['id'] == node.get('id'))), None) or \
                    next((g for g in fields if g['id'] == (s.get('target') or {}).get('id')), None)
                if fld is None:
                    raise RuntimeError(f"step {s['i']}: field {s.get('target')} not found")
                val, miss = t.facts.substitute(s.get('value', ''))
                if miss:
                    t.log(f'replay {s["i"]}: skipped {fld["label"][:30]} (no value for {", ".join(miss)})')
                    continue
                await self.fill_one(t, m, fld, val)
                t.filled_urls.add(t.url)
                res = f'ok (filled {fld["label"][:30]})'
            elif k == 'scroll':
                await self.a.wheel(6 if s.get('direction') == 'down' else -6)
                res = 'ok'
            elif k == 'back':
                await self.b.back()
                res = 'ok'
            elif k == 'key':
                await self.a.key(s.get('key', 'Escape'))
                res = 'ok'
            elif k == 'ai_check':
                m = await self.observe(t, s['i'])
                txt, used = await asyncio.to_thread(llm.chat, [{'role': 'user', 'content':
                    f"{s['question']}\nReply JSON {{\"answer\": \"yes\"|\"no\", \"why\": \"...\"}}\n\nPAGE MAP:\n{t.facts.redact(m.render_focused(2000, getattr(m, 'viewport', None)))}"}],
                    'default', 200, 60, None, t.log)
                v = llm.parse_json(txt)
                res = f"ai_check {v.get('answer')}: {v.get('why', '')[:100]}"
                if str(v.get('answer', '')).lower() != s.get('expect', 'yes'):
                    t.steps.append(dict(n=s['i'], action=k, detail=res))
                    raise RuntimeError(f'ai_check failed: {res}')
            elif k == 'human_takeover':
                res = 'no blocker this time' if t.status == 'running' else 'handed off'
            else:
                res = 'skipped'
            t.steps.append(dict(n=s['i'], action=k, detail=t.facts.redact(res)))
            t.log(f"replay {s['i']} {k}: {res}")
            await asyncio.sleep(random.uniform(0.3, 0.8))
        t.status, t.answer = 'done', f'Replayed {len(f.d["steps"])} steps; ended at {t.title}'


def _fetch(url, cookie=''):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', **({'Cookie': cookie} if cookie else {})})
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read(60 * 1024 * 1024)


def notify(title, body, url):
    """phone push via ntfy.sh when NTFY_TOPIC is set (install the ntfy app and subscribe to the topic)"""
    topic = os.environ.get('NTFY_TOPIC')
    if not topic:
        return
    try:
        req = urllib.request.Request(f'https://ntfy.sh/{topic}', data=body.encode(), headers={
            'Title': title.encode('ascii', 'ignore').decode(), 'Click': url, 'Priority': 'high', 'Tags': 'warning'})
        urllib.request.urlopen(req, timeout=8)
    except Exception as e:
        print('ntfy failed', e)
