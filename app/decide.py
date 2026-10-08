"""Engine v2: rank first, reason second.

v1 sends the LLM a ~2400-token page map every step and lets it find the control. v2 instead:
  1. turns every control on the page into a short "card" (role, label, value, field label, region, on-screen or not);
  2. ranks the cards for the current sub-goal with small local models (bge-small embeddings + a MiniLM cross-encoder,
     ONNX on CPU, ~50-150 ms; plain word matching when those models are not installed);
  3. FAST PATH: the LLM also plans the next 1-3 obvious actions ("next": type To, then click Search). Each is grounded
     on the new page by the ranker alone and runs with no LLM call when the match is confident; anything unsure goes back
     to the LLM. Final/money buttons never run on the fast path (and the executor's confirmation still applies);
  4. when the LLM is asked, it gets the top-ranked controls plus a short page outline instead of the full map, and can
     ask for the full map with {"action": "full_map"} (costs no step);
  5. every decision is logged to ~/.agentapp/v2_steps.jsonl (sub-goal, chosen card, local or LLM, ok or error) as
     training data for a learned ranker later.

v1 (Runner.run) is untouched; a task runs on v2 only when it was started with engine="v2".
"""
import asyncio, json, os, random, re, threading, time, urllib.parse

import agentmap as AM
import forms
import llm
import marks

MODELS_DIR = os.environ.get('FASTEMBED_CACHE_PATH') or os.path.expanduser('~/.agentapp/models')
LOG_PATH = os.path.expanduser('~/.agentapp/v2_steps.jsonl')
EMB_MODEL = os.environ.get('V2_EMBED_MODEL', 'BAAI/bge-small-en-v1.5')
CE_MODEL = os.environ.get('V2_RERANK_MODEL', 'Xenova/ms-marco-MiniLM-L-6-v2')

ROLE_WORD = {'link': 'link', 'button': 'button', 'textbox': 'text input', 'searchbox': 'search input', 'combobox': 'dropdown',
             'listbox': 'dropdown', 'checkbox': 'checkbox', 'radio': 'radio option', 'menuitem': 'menu item', 'tab': 'tab',
             'option': 'option', 'switch': 'switch', 'slider': 'slider', 'spinbutton': 'number input', 'treeitem': 'tree item',
             'menuitemcheckbox': 'menu item', 'menuitemradio': 'menu item'}
TYPEABLE = {'textbox', 'searchbox', 'combobox', 'spinbutton'}
CLICKABLE = AM.INTERACTIVE - {'textbox', 'searchbox', 'spinbutton'}
STOP = set('a an the to of in on for and or with by at is are be this that it its my your from as into click press tap type '
           'enter fill select choose open go button link field input box text option page'.split())
SYN = {'from': ['departure', 'source', 'origin', 'leaving'], 'to': ['destination', 'arrival', 'going'],
       'search': ['find', 'go', 'submit', 'lookup'], 'login': ['signin', 'sign', 'log'], 'dob': ['birth', 'date of birth'],
       'mobile': ['phone', 'contact'], 'email': ['mail', 'e-mail'], 'next': ['continue', 'proceed'], 'name': ['full'],
       'date': ['journey', 'day'], 'close': ['dismiss', 'x', 'cancel', 'skip']}


def _words(s):
    w = [x for x in re.findall(r'[a-z0-9]+', (s or '').lower()) if x not in STOP]
    out = set(w)
    for x in w:
        for k, vs in SYN.items():
            if x == k or x in vs:
                out.add(k)
                out.update(v for v in vs if ' ' not in v)
    return out


def _norm(s):
    return ' '.join(re.findall(r'[a-z0-9]+', (s or '').lower()))


class Ranker:
    """Lazy, thread-safe holder of the two local models. Falls back to word matching when they are unavailable."""

    def __init__(self):
        self.emb = self.ce = None
        self.state, self.err = 'cold', ''
        self.lock = threading.Lock()
        self.cache = {}

    def load(self):
        with self.lock:
            if self.state != 'cold':
                return self.state
            self.state = 'loading'
            try:
                os.makedirs(MODELS_DIR, exist_ok=True)
                os.environ.setdefault('HF_HUB_DISABLE_TELEMETRY', '1')
                from fastembed import TextEmbedding
                from fastembed.rerank.cross_encoder import TextCrossEncoder
                t = time.time()
                self.emb = TextEmbedding(EMB_MODEL, cache_dir=MODELS_DIR, threads=2)
                self.ce = TextCrossEncoder(CE_MODEL, cache_dir=MODELS_DIR, threads=2)
                list(self.emb.embed(['warm up', 'button: Search'])), list(self.ce.rerank('warm up', ['button: Search']))
                self.state = 'ready'
                print(f'[v2] local ranker ready in {time.time() - t:.1f}s ({EMB_MODEL} + {CE_MODEL})', flush=True)
            except Exception as e:
                self.state, self.err = 'lexical', f'{type(e).__name__}: {e}'[:200]
                print(f'[v2] local models unavailable, using word matching: {self.err}', flush=True)
            return self.state

    def warm(self):
        threading.Thread(target=self.load, daemon=True).start()

    def _vecs(self, texts):
        import numpy as np
        need = [x for x in dict.fromkeys(texts) if x not in self.cache]
        if need:
            for x, v in zip(need, self.emb.embed(need, batch_size=64)):
                self.cache[x] = v
            if len(self.cache) > 30000:
                self.cache.clear()
        return np.array([self.cache[x] for x in texts])

    def rank(self, query, cards, action=None, k=15):
        """cards -> [(score, card)] best first. Scores: cross-encoder logit (or word score) + rule bonuses."""
        if not cards:
            return []
        if self.state == 'cold':
            self.load()
        qw = _words(query)
        qn = _norm(query)

        def lex(c):
            cw = _words(c['label'])
            if not cw or not qw:
                return 0.0
            hit = len(qw & cw) + 0.5 * sum(1 for a in qw for b in cw if a != b and len(a) > 3 and len(b) > 3 and a[:4] == b[:4])
            return hit / (len(cw) ** 0.5 * len(qw) ** 0.5)

        def rule(c):
            b = 0.0
            if action == 'type':
                b += 1.0 if c['role'] in TYPEABLE else -3.0
            elif action == 'click':
                b += 0.3 if c['role'] in CLICKABLE else -1.5
            if c['onscreen']:
                b += 0.5
            nl = _norm(c['name'])
            if nl and (nl == qn or (len(nl) > 2 and re.search(r'\b' + re.escape(nl) + r'\b', qn))):
                b += 2.0  # the control's own label appears in the request
            return b

        pre = [(lex(c), c) for c in cards]
        if len(pre) > 120:  # big pages (Wikipedia has ~2000 links): embed only plausible candidates
            keep = {id(c) for _, c in sorted(pre, key=lambda x: -x[0])[:60]}
            keep |= {id(c) for _, c in pre if c['role'] in TYPEABLE}
            keep |= set([id(c) for _, c in pre if c['onscreen'] and id(c) not in keep][:80])
            pre = [x for x in pre if id(x[1]) in keep]
        if self.state == 'ready':
            try:
                import numpy as np
                qv = list(self.emb.query_embed([query]))[0]
                sims = self._vecs([c['text'] for _, c in pre]) @ qv
                pre = [(0.6 * float(s) + 0.4 * l, c) for s, (l, c) in zip(sims, pre)]
                pre.sort(key=lambda x: -(x[0] + 0.1 * rule(x[1])))
                top = [c for _, c in pre[:30]]
                logits = list(self.ce.rerank(query, [c['text'] for c in top]))
                out = [(float(s) + 3.0 * lex(c) + rule(c), c) for s, c in zip(logits, top)]
                out.sort(key=lambda x: -x[0])
                return out[:k]
            except Exception as e:
                self.state, self.err = 'lexical', f'{type(e).__name__}: {e}'[:200]
        out = [(4.0 * l + rule(c), c) for l, c in pre]
        out.sort(key=lambda x: -x[0])
        return out[:k]


R = Ranker()


def build_cards(m, fields):
    vp = getattr(m, 'viewport', None) or {}
    top, h = vp.get('pageY', 0), vp.get('clientHeight', 900)
    flabel = {f['id']: f.get('label') or '' for f in fields or [] if f.get('id')}
    cards = []
    for i, n in m.by_id.items():
        if not isinstance(n, dict) or n.get('role') not in AM.INTERACTIVE or 'disabled' in (n.get('state') or []):
            continue
        name = (n.get('name') or '').strip()
        lab = flabel.get(i, '')
        reg = m.by_id.get(n.get('region') or n.get('region_of') or '', {})
        region = (reg.get('name') or '') if isinstance(reg, dict) else ''
        b = n.get('box')
        onscreen = bool(b and b[1] < top + h and b[1] + b[3] > top)
        label = ' '.join(x for x in dict.fromkeys([name, lab]) if x and x != '(no label)')
        text = f"{ROLE_WORD.get(n['role'], n['role'])}: {label or '(no label)'}"
        if n.get('value') not in (None, ''):
            text += f" = {str(n['value'])[:40]}"
        if region and region.lower() not in label.lower():
            text += f" (in {region[:50]})"
        cards.append(dict(id=i, role=n['role'], name=name, label=label or name, text=text[:200], onscreen=onscreen, node=n))
    return cards


def card_line(c, redact):
    n = c['node']
    s = f"[{c['id']}] {n['role']} \"{AM.short(c['label'], 70)}\""
    if n.get('value') not in (None, ''):
        s += f" = \"{AM.short(str(n['value']), 40)}\""
    if n.get('state'):
        s += ' {' + ','.join(n['state']) + '}'
    if not c['onscreen']:
        s += ' (off-screen)'
    return redact(s)


def ground(R_, plan, cards, final_re):
    """A planned action from the LLM -> an executable action with a real id, or (None, why)."""
    a = (plan.get('action') or '').lower()
    if a in ('key', 'scroll', 'wait', 'back'):
        return {k: v for k, v in plan.items() if k != 'target'}, f'{a} (planned)'
    if a not in ('click', 'type'):
        return None, f'{a} is not a fast-path action'
    target = str(plan.get('target') or '').strip()
    if not target:
        return None, 'no target'
    ranked = R_.rank(target, cards, action=a, k=8)
    if not ranked:
        return None, 'no controls on the page'
    s1, c1 = ranked[0]
    # same control repeated (nav + body, or a hidden shortcut copy): treat as one target, prefer on-screen and shorter
    n1 = _norm(c1['name'])
    same = [(s, c) for s, c in ranked if s >= s1 - 1.5 and n1 and (_norm(c['name']) == n1 or
            _norm(c['name']).startswith(n1 + ' ') or n1.startswith(_norm(c['name']) + ' ')) and c['role'] == c1['role']]
    if len(same) > 1:
        s1, c1 = max(same, key=lambda x: (x[1]['onscreen'], -len(x[1]['name']), x[0]))
        s1 = max(s for s, _ in same)
    grp = {id(c) for _, c in same} | {id(c1)}
    rest = [s for s, c in ranked if id(c) not in grp]
    s2 = rest[0] if rest else -99
    exact = _norm(c1['name']) and (_norm(c1['name']) in _norm(target)) and \
        sum(1 for _, c in ranked if _norm(c['name']) == _norm(c1['name'])) == 1
    strong = (s1 >= 1.0 and s1 - s2 >= 2.5) if R_.state == 'ready' else (s1 >= 3.0 and s1 - s2 >= 1.5)
    if not (strong or (exact and s1 - s2 >= 1.0)):
        return None, f'unsure about "{target[:40]}" (best "{c1["label"][:30]}" {s1:.1f} vs {s2:.1f})'
    if a == 'click' and final_re.search(c1['name'] or ''):
        return None, f'"{c1["name"][:30]}" looks like a final button'
    act = dict(action=a, id=c1['id'], thought=f'{a} "{c1["label"][:50]}" (matched "{target[:40]}", {s1:.1f} vs {s2:.1f})')
    if a == 'type':
        act['text'] = plan.get('text', '')
        act['submit'] = bool(plan.get('submit'))
    return act, act['thought']


def outline(m, budget=1100):
    try:
        return m.render_focused(budget, getattr(m, 'viewport', None))
    except Exception:
        return m.header()


def log_row(**d):
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, 'a') as f:
            f.write(json.dumps(d, ensure_ascii=False)[:2000] + '\n')
    except Exception:
        pass


V2_RULES = """
ENGINE V2 (fast mode): instead of the whole PAGE MAP you get RELEVANT CONTROLS (ranked for your SUBGOAL by a local
model, best first) and a short PAGE OUTLINE. Use ids exactly as listed. If what you need is not there, reply
{"action": "full_map"} to get the complete page map (it costs no step). Add two more keys to every reply:
"subgoal": a short phrase for what you will want to do on the NEXT page view (e.g. "type destination station"), used to
rank controls for you; and "next": up to 3 actions you are sure come right after this one, e.g.
[{"action": "type", "target": "text input To station", "text": "New Delhi"}, {"action": "click", "target": "button Search"}].
"target" is the control's visible label as the site words it, with its kind. A local matcher runs these WITHOUT asking you
when it finds the control confidently, so only list obvious steps; use [] when unsure or when the next page is unknown.
Never put fill_form, ask_user, goto, done or a final submit/pay/place-order button in "next"."""


async def run_v2(self, t):
    """Runner.run with the v2 decision layer. `self` is the Runner."""
    import agent as A
    await self.intake(t)
    t.engine_stats = dict(local=0, llm=0, full_map=0, chars=0)
    if R.state == 'cold':
        await asyncio.to_thread(R.load)
    t.emit('log', f'engine v2: local ranker {R.state}' + (f' ({R.err[:80]})' if R.state == 'lexical' and R.err else ''))
    history = []
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
    queue, subgoal = [], t.goal
    system = A.SYSTEM + V2_RULES
    for n in range(1, t.max_steps + 1):
        await self.checkpoint(t, n, history)
        m = await self.observe(t, n)
        if t.user_note.strip():
            t.add_guidance(t.user_note.strip(), emit=False)
            t.user_note = ''
            queue = []
        try:
            fields, errs = forms.extract(m, getattr(m, 'viewport', None))
        except Exception as e:
            fields, errs = [], []
            t.log(f'form read failed: {type(e).__name__}: {e}')
        t.fields = fields
        cards = build_cards(m, fields)
        if not t.visited or t.visited[-1][1] != t.url:
            t.visited.append((t.title[:60], t.url[:120]))
        act, used, src = None, 'local-ranker', 'llm'
        # ---------- fast path: ground the next planned action with the local ranker, no LLM call ----------
        if queue and not need_vision and n < t.max_steps:
            plan = queue.pop(0)
            t0 = time.time()
            act, why = await asyncio.to_thread(ground, R, plan, cards, A.FINAL_BTN)
            if act:
                src = 'local'
                t.engine_stats['local'] += 1
                t.emit('think', f'[local ranker {1000 * (time.time() - t0):.0f} ms] {why}', model='local-ranker',
                       action=json.loads(t.facts.redact(json.dumps(act))))
            else:
                queue = []
                t.emit('log', f'fast path handed back to the LLM: {why}')
        if act is None:
            rev = t.guide_rev
            ranked = await asyncio.to_thread(R.rank, subgoal or t.goal, cards, None, 18)
            redact = t.facts.redact
            full = False
            for attempt in range(2):
                if full:
                    raw_page = m.render_focused(2400, getattr(m, 'viewport', None), focus_ids=[t.last_target])
                    page_block = f'PAGE MAP:\n{redact(raw_page)}'
                else:
                    raw_page = outline(m)
                    ctl = '\n'.join(card_line(c, redact) for _, c in ranked)
                    page_block = (f'RELEVANT CONTROLS for "{redact(subgoal or t.goal)[:80]}" ({len(ranked)} of {len(cards)}, best first):\n{ctl}'
                                  f'\n\nPAGE OUTLINE (short; reply {{"action": "full_map"}} for everything):\n{redact(raw_page)}')
                on_screen_secret = redact(raw_page) != raw_page
                form_block = forms.render(fields, errs, redact)
                guide = ('\nUSER INSTRUCTIONS (newest last, follow them):\n' + '\n'.join(f'- {g}' for g in t.guidance[-6:])) if t.guidance else ''
                last = '\nTHIS IS THE LAST STEP: reply done with your best answer.' if n == t.max_steps else ''
                notes = ('\nNOTES (what you learned so far):\n' + '\n'.join(f'- {x}' for x in t.notes[-12:])) if t.notes else ''
                factl = t.facts.for_llm()
                facts_block = f'\nFACTS (the user\'s values; use their keys):\n{factl}' if factl else ''
                seen = '\nPAGES VISITED: ' + ' > '.join(f'{ti or u}' for ti, u in t.visited[-10:])
                older = history[:-8]
                hist = ((f'(earlier: {len(older)} steps: ' + '; '.join(h.split(' -> ')[0][:50] for h in older[-12:]) + ')\n') if older else '') + '\n'.join(history[-8:])
                msg = (f"GOAL: {t.goal}{guide}\nSTEP {n}/{t.max_steps}{last}{facts_block}{notes}{t.files_note()}{seen}\nHISTORY:\n" +
                       redact(hist) + (f'\n\n{extra}' if extra else '') + f"\n\n{page_block}" +
                       (f"\n\nFORM FIELDS (visible inputs with their current values; ids work with fill_form, click and type):\n{form_block}"
                        if form_block else ''))
                img = None
                if need_vision and t.shot and os.path.exists(t.shot) and not on_screen_secret:
                    tagged = f'/tmp/agent_{t.id}_marks.png'
                    try:
                        legend = marks.draw(m, tagged)
                        img = open(tagged, 'rb').read()
                        msg += ('\n\nSCREENSHOT ATTACHED (numbered tags; TAGS -> ids: ' + ', '.join(f'{i}={x}' for i, x in legend) +
                                '). Your last attempt failed or went nowhere (' + need_vision + '). Act with an id that exists on the page.')
                    except Exception as e:
                        t.log(f'set-of-marks failed ({e})')
                    t.emit('log', f'asking the vision model for help with a screenshot ({need_vision})')
                t.log(f'llm request (v2{" full map" if full else ""}): {len(msg)} chars, {len(ranked)} ranked controls, image: {"yes" if img else "no"}')
                t.engine_stats['chars'] += len(msg)
                try:
                    try:
                        txt, used = await asyncio.to_thread(llm.chat, [{'role': 'system', 'content': system},
                                                                       {'role': 'user', 'content': msg}], role, 1000, 75, img, t.log)
                    except Exception:
                        if img is None:
                            raise
                        t.log('vision help failed; continuing with text only')
                        txt, used = await asyncio.to_thread(llm.chat, [{'role': 'system', 'content': system},
                                                                       {'role': 'user', 'content': msg}], role, 1000, 75, None, t.log)
                    act = llm.parse_json(txt)
                except Exception as e:
                    act = None
                    fails += 1
                    t.log(f'llm error: {e}')
                    role = 'escalate' if fails >= 2 else 'plan'
                    if fails >= 4:
                        raise
                    break
                t.engine_stats['llm'] += 1
                if (act.get('action') or '').lower() == 'full_map' and not full:
                    full = True
                    t.engine_stats['full_map'] += 1
                    t.emit('log', 'the LLM asked for the full page map')
                    continue
                break
            need_vision = ''
            extra = ''
            if act is None:
                continue
            if await self.checkpoint(t, n, history):
                continue
            if t.guide_rev != rev:
                t.emit('log', 'new instruction from you arrived while thinking: replanning with it')
                continue
            if (act.get('action') or '').lower() == 'full_map':
                act = dict(action='wait', thought='asked for the full map twice')
            t.emit('think', f"[{used.split('/')[-1]}] {str(act.get('thought', ''))[:160]}", model=used,
                   action=json.loads(t.facts.redact(json.dumps(act))))
            nx = act.get('next') if isinstance(act.get('next'), list) else []
            queue = [x for x in nx[:3] if isinstance(x, dict) and (x.get('action') or '').lower() in ('click', 'type', 'key', 'scroll')]
            subgoal = str(act.get('subgoal') or act.get('thought') or t.goal)[:160]
            if queue:
                t.emit('log', 'planned next: ' + '; '.join(f"{x.get('action')} {x.get('target') or x.get('key') or ''}"[:50] for x in queue))
            rem = ' '.join(str(act.get('remember') or '').split())[:240]
            if rem and rem not in t.notes:
                t.notes.append(t.facts.redact(rem))
                t.emit('log', f'remembered: {rem}')
        # ---------- execute (identical to v1) ----------
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
        t.emit('act', f"step {n}: {a} {what}" + (f" \"{act.get('text')}\"" if a == 'type' else '') + (' (local)' if src == 'local' else ''))
        result = await self.execute(t, m, act, n)
        result = t.facts.redact(result)
        t.steps.append(dict(n=n, action=a, detail=result[:200], thought=t.facts.redact(str(act.get('thought', '')))[:160],
                            model='local-ranker' if src == 'local' else used))
        t.emit('result', f'  → {result[:220]}')
        history.append(f"{n}. {a} {what[:120]} -> {result[:300 if a in ('fill_form', 'ask_user') else 120]}" + (' [fast path]' if src == 'local' else ''))
        card = next((c for c in cards if c['id'] == act.get('id')), None)
        log_row(ts=round(time.time()), task=t.id, host=urllib.parse.urlsplit(t.url or '').hostname, src=src, action=a,
                subgoal=t.facts.redact(subgoal)[:160], card=t.facts.redact(card['text']) if card else None,
                ok=not result.startswith('ERROR'))
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
            _summary(t)
            t.log(f'DONE: {t.answer}')
            return
        view = (t.url, m.viewport.get('pageY') if getattr(m, 'viewport', None) else None, len(m.by_id))
        if result.startswith('ERROR'):
            need_vision = 'error: ' + result[6:90]
            queue = []
        elif repeating:
            need_vision = 'you repeated the same action'
            queue = []
        elif a == 'scroll' and view == last_view:
            need_vision = 'scrolling did not change the page'
        last_view = view
        if result.startswith('ERROR'):
            fails += 1
        role = 'escalate' if repeating or fails >= 2 else 'plan'
        if not result.startswith('ERROR'):
            fails = 0
        await asyncio.sleep(random.uniform(0.2, 0.5) if src == 'local' else random.uniform(0.4, 1.1))
    t.status = 'stopped'
    t.answer = f'Stopped after {t.max_steps} steps at {t.title}'
    if t.public_files():
        t.answer += '. Files saved: ' + ', '.join(f['name'] for f in t.public_files())
    _summary(t)


def _summary(t):
    s = getattr(t, 'engine_stats', None)
    if s:
        tot = s['local'] + s['llm']
        t.emit('log', f"engine v2: {s['local']} of {tot} decisions made locally, {s['llm']} LLM calls "
                      f"({s['full_map']} asked for the full map), {s['chars'] // 1000}k chars sent")
