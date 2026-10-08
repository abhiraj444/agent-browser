"""Form understanding: the visible fields of a page with their labels, types, options and current values.

Built from what the snapshot already captured (pierced DOM tree, DOMSnapshot form state, accessibility names); no page
scripts run. Same-process iframes are included because the pierced DOM walks into them; their fields get fld: ids.
"""
import re
import difflib

import agentmap as AM

FIELD_TAGS = {'INPUT', 'SELECT', 'TEXTAREA'}
SKIP_TYPES = {'hidden', 'submit', 'button', 'reset', 'image', 'file'}
ERR_CLASS = re.compile(r'(^|[\s_-])(error|err|invalid|is-invalid|text-danger|validation|field-validation-error|help-block)'
                       r'($|[\s_-])', re.I)
ERR_TEXT = re.compile(r'required|invalid|please (enter|select|provide|fill)|must be|not valid|incorrect|enter (a )?valid|'
                      r'can.?t be (blank|empty)|mandatory', re.I)


def _attrs(n):
    a = n.get('attributes') or []
    return dict(zip(a[0::2], a[1::2]))


def _text(n, limit=200):
    """visible text under a DOM node (shallow but enough for labels and error lines)"""
    out = []

    def walk(k):
        if sum(len(x) for x in out) > limit:
            return
        if k.get('nodeType') == 3:
            t = (k.get('nodeValue') or '').strip()
            if t:
                out.append(t)
        elif k.get('nodeName') not in ('SCRIPT', 'STYLE', 'NOSCRIPT', 'OPTION', 'SELECT', 'TEXTAREA'):
            for c in k.get('children') or []:
                walk(c)
    walk(n)
    return ' '.join(' '.join(out).split())[:limit]


def _snap_state(snap):
    """backendNodeId -> {value, checked, selected} from DOMSnapshot rare data (the live values, not the attributes)"""
    st, strings = {}, snap.get('strings', [])
    for d in snap.get('documents', []):
        nd = d['nodes']
        be = nd.get('backendNodeId', [])
        for rare in ('inputValue', 'textValue'):  # inputValue: <input>; textValue: <textarea>
            iv = nd.get(rare) or {}
            for i, s in zip(iv.get('index', []), iv.get('value', [])):
                if i < len(be):
                    st.setdefault(be[i], {})['value'] = strings[s] if 0 <= s < len(strings) else ''
        for name in ('inputChecked', 'optionSelected'):
            for i in (nd.get(name) or {}).get('index', []):
                if i < len(be):
                    st.setdefault(be[i], {})[name] = True
    return st


def kind_of(tag, a, role=''):
    t = (a.get('type') or '').lower()
    if tag == 'SELECT':
        return 'select'
    if tag == 'TEXTAREA':
        return 'textarea'
    if tag == 'INPUT':
        return t if t in ('checkbox', 'radio', 'date', 'email', 'tel', 'number', 'password', 'search', 'url',
                          'datetime-local', 'month', 'time') else 'text'
    if a.get('contenteditable') in ('', 'true', 'plaintext-only'):
        return 'text'
    return {'combobox': 'combobox', 'checkbox': 'checkbox', 'radio': 'radio', 'textbox': 'text', 'listbox': 'combobox',
            'switch': 'checkbox', 'spinbutton': 'number'}.get(role, 'text')


def extract(m, viewport=None):
    """list of field dicts in document order. Radio buttons sharing a name become one 'radio' field with options."""
    snap = m.cap['snap']
    state = _snap_state(snap)
    boxes = AM.boxes_from_snapshot(snap)
    by_be = {n['be']: n for n in m.by_id.values() if isinstance(n, dict) and n.get('be') and n.get('role') in AM.INTERACTIVE}
    labels_for, fields, errors = {}, [], []
    ctx = dict(last_text='', frame='')
    raw = []

    def walk(n, label_anc, frame):
        nm = n.get('nodeName', '')
        a = _attrs(n) if n.get('nodeType') == 1 else {}
        if n.get('nodeType') == 3:
            t = ' '.join((n.get('nodeValue') or '').split())
            if len(t) > 1 and re.search(r'\w', t):
                ctx['last_text'] = t[-80:]
        if nm == 'LABEL':
            txt = _text(n, 120)
            if a.get('for'):
                labels_for[(frame, a['for'])] = (txt, n.get('backendNodeId'))
            label_anc = (txt, n.get('backendNodeId'))
        if nm in ('SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE'):
            return
        b = n.get('backendNodeId')
        if n.get('nodeType') == 1 and (a.get('role') == 'alert' or ERR_CLASS.search(a.get('class', '') + ' ' + a.get('id', ''))):
            t = _text(n, 160)
            bx = boxes.get(b)
            if t and bx and bx[2] > 0 and bx[3] > 0 and (a.get('role') == 'alert' or ERR_TEXT.search(t)):
                errors.append(t)
        editable = a.get('contenteditable') in ('', 'true', 'plaintext-only') and nm not in FIELD_TAGS
        if (nm in FIELD_TAGS and (a.get('type') or '').lower() not in SKIP_TYPES) or editable:
            raw.append(dict(node=n, a=a, tag=nm, be=b, label_anc=label_anc, near=ctx['last_text'], frame=frame))
            if editable:
                return  # its inner text is the value, not more fields
        for c in n.get('children') or []:
            walk(c, label_anc, frame)
        if n.get('contentDocument'):
            fa = a.get('src') or a.get('title') or 'frame'
            walk(n['contentDocument'], None, fa)
        for c in n.get('shadowRoots') or []:
            walk(c, label_anc, frame)

    walk(m.dom['root'], None, '')

    used_ids = set(m.by_id)
    groups = {}
    for r in raw:
        n, a, tag, be = r['node'], r['a'], r['tag'], r['be']
        amap = by_be.get(be)
        role = amap['role'] if amap else ''
        kind = kind_of(tag, a, role)
        bx = boxes.get(be)
        visible = bool(bx and bx[2] > 1 and bx[3] > 1) and a.get('aria-hidden') != 'true'
        lab, lab_be = labels_for.get((r['frame'], a.get('id')), (None, None)) if a.get('id') else (None, None)
        if not lab and r['label_anc']:
            lab, lab_be = r['label_anc']
        name = (amap or {}).get('name') if amap and amap.get('name') not in (None, '', '(no label)') else ''
        label = name or lab or a.get('aria-label') or a.get('placeholder') or a.get('title') or r['near'] or a.get('name') or ''
        label = ' '.join(str(label).split()).rstrip(' :')[:90]
        if kind in ('checkbox', 'radio') and not visible:
            lb = boxes.get(lab_be) if lab_be else None
            if lb and lb[2] > 1 and lb[3] > 1:
                visible, bx = True, lb  # custom-styled control: the visible label is what a person clicks
            else:
                continue
        if not visible:
            continue
        st = state.get(be, {})
        f = dict(be=be, click_be=be if (boxes.get(be) or [0, 0, 0, 0])[2] > 1 else (lab_be or be), tag=tag, type=kind,
                 label=label, box=bx, frame=r['frame'], name=a.get('name', ''), html_id=a.get('id', ''),
                 required='required' in a or a.get('aria-required') == 'true' or ('*' in (lab or '')[-3:]),
                 disabled='disabled' in a or 'readonly' in a, invalid=a.get('aria-invalid') == 'true' or
                 bool(amap and 'invalid' in (amap.get('state') or [])),
                 placeholder=a.get('placeholder', ''), maxlength=a.get('maxlength', ''), inputmode=a.get('inputmode', ''),
                 pattern=a.get('pattern', ''), autocomplete=a.get('autocomplete', ''), min=a.get('min', ''), max=a.get('max', ''))
        if kind == 'select':
            opts = []
            for o in _options(n):
                ob = o.get('backendNodeId')
                oa = _attrs(o)
                opts.append(dict(text=_opt_text(o), value=oa.get('value', _opt_text(o)),
                                 selected=bool(state.get(ob, {}).get('optionSelected')), disabled='disabled' in oa))
            if opts and not any(o['selected'] for o in opts):
                opts[0]['selected'] = True  # nothing marked: the browser shows the first option
            f['options'] = opts
            cur = next((o for o in opts if o['selected']), None)
            f['value'] = cur['text'] if cur else ''
            f['multiple'] = 'multiple' in a
        elif kind in ('checkbox', 'radio'):
            f['checked'] = bool(st.get('inputChecked'))
            f['value'] = a.get('value', 'on')
            f['option_label'] = lab or name or a.get('value') or r['near']
        elif tag in FIELD_TAGS:
            f['value'] = st.get('value', a.get('value', ''))
        else:
            f['value'] = _text(n, 300)
        # an id the agent can use: the PAGE MAP id when the control is in the map, else a stable fld: id
        if amap:
            f['id'] = amap['id']
        else:
            base = 'fld:' + AM.slug(label or a.get('name') or kind)
            fid, k = base, 2
            while fid in used_ids:
                fid, k = f'{base}-{k}', k + 1
            used_ids.add(fid)
            f['id'] = fid
            m.by_id[fid] = dict(id=fid, role={'select': 'combobox', 'checkbox': 'checkbox', 'radio': 'radio'}.get(kind, 'textbox'),
                                name=label, be=f['click_be'], box=bx, region='r:form', kids=[], state=[])
        if kind == 'radio':
            gk = (r['frame'], a.get('name') or f['id'])
            if gk in groups:
                g = groups[gk]
                g['options'].append(dict(text=f['option_label'], value=f['value'], id=f['id'], be=f['click_be'], checked=f['checked']))
                if f['checked']:
                    g['value'] = f['option_label']
                g['required'] = g['required'] or f['required']
                continue
            f['options'] = [dict(text=f['option_label'], value=f['value'], id=f['id'], be=f['click_be'], checked=f['checked'])]
            f['label'] = _group_label(f, r)
            f['value'] = f['option_label'] if f['checked'] else ''
            groups[gk] = f
        fields.append(f)
    if viewport:
        top, h = viewport.get('pageY', 0), viewport.get('clientHeight', 900)
        for f in fields:
            b = f.get('box')
            f['in_view'] = bool(b and b[1] + b[3] > top and b[1] < top + h)
    return fields, list(dict.fromkeys(errors))[:8]


def _options(sel):
    out = []
    for c in sel.get('children') or []:
        if c.get('nodeName') == 'OPTION':
            out.append(c)
        elif c.get('nodeName') == 'OPTGROUP':
            out += [k for k in c.get('children') or [] if k.get('nodeName') == 'OPTION']
    return out


def _opt_text(o):
    return ' '.join(''.join((k.get('nodeValue') or '') for k in o.get('children') or [] if k.get('nodeType') == 3).split())


def _group_label(f, r):
    # a radio group's question is usually the text before its first option (legend, td, div)
    near = r['near']
    if near and near != f['option_label']:
        return near[:90]
    return (f.get('name') or 'choice').replace('_', ' ')


def render(fields, errors, redact=lambda s: s, limit=40):
    """FORM FIELDS block for the LLM (ids are usable with fill_form, click and type)"""
    if not fields:
        return ''
    lines = []
    for f in fields[:limit]:
        req = ' *required' if f['required'] else ''
        dis = ' (read-only)' if f['disabled'] else ''
        inv = ' INVALID' if f['invalid'] else ''
        fr = f" (in frame {f['frame'][:40]})" if f['frame'] else ''
        if f['type'] == 'select':
            opts = [o['text'] for o in f.get('options', []) if o['text']]
            shown = ' | '.join(opts[:12]) + (f' | …(+{len(opts) - 12})' if len(opts) > 12 else '')
            v = f'= "{redact(f["value"])}"; options: {shown}'
        elif f['type'] == 'radio':
            v = '; choose one of: ' + ' | '.join(o['text'] for o in f['options']) + \
                (f' (now: {f["value"]})' if f['value'] else ' (none chosen)')
        elif f['type'] == 'checkbox':
            v = f'"{f["option_label"]}" ' + ('checked' if f['checked'] else 'unchecked')
        else:
            v = f'= "{redact(str(f["value"])[:60])}"' if f.get('value') else '= (empty)'
            hints = [h for h in (f['placeholder'] and f'placeholder "{f["placeholder"][:30]}"',
                                 f['maxlength'] and f'max {f["maxlength"]} chars', f['pattern'] and 'has a pattern') if h]
            if hints:
                v += ' [' + ', '.join(hints) + ']'
        lines.append(f"[{f['id']}] {f['type']} \"{f['label'][:70]}\"{req}{dis}{inv}{fr} {v}")
    if len(fields) > limit:
        lines.append(f'… +{len(fields) - limit} more fields further down (scroll to see them)')
    if errors:
        lines.append('PAGE SHOWS THESE ERRORS: ' + ' / '.join(redact(e) for e in errors))
    return '\n'.join(lines)


def match_option(options, want):
    """best option for a wanted text/value: exact text > exact value > prefix > contains > fuzzy (>=0.75)"""
    w = str(want or '').strip().lower()
    if not w:
        return None
    opts = [o for o in options if not o.get('disabled')]
    for test in (lambda o: o['text'].lower() == w, lambda o: str(o.get('value', '')).lower() == w,
                 lambda o: o['text'].lower().startswith(w), lambda o: w in o['text'].lower(),
                 lambda o: o['text'].lower() in w and len(o['text']) > 2):
        hit = [o for o in opts if test(o)]
        if hit:
            return hit[0]
    best = max(opts, key=lambda o: difflib.SequenceMatcher(None, o['text'].lower(), w).ratio(), default=None)
    if best and difflib.SequenceMatcher(None, best['text'].lower(), w).ratio() >= 0.75:
        return best
    return None


def truthy(v):
    return str(v).strip().lower() in ('1', 'true', 'yes', 'y', 'on', 'checked', 'tick', 'agree', 'accept')


def same_value(field, want):
    """does a field now hold what we meant to put there? (formatting-tolerant)"""
    t = field['type']
    if t == 'checkbox':
        return field['checked'] == truthy(want)
    if t in ('select', 'radio'):
        o = match_option(field.get('options', []), want)
        return bool(o) and (field['value'] or '').strip().lower() == o['text'].strip().lower()
    got, w = str(field.get('value') or ''), str(want or '')
    if got == w:
        return True
    if t == 'date':
        return norm_date(w) == got  # the DOM value of a date input is always yyyy-mm-dd
    dg, dw = re.sub(r'\D', '', got), re.sub(r'\D', '', w)
    if dw and len(dw) >= 4 and dg == dw and not re.search(r'[A-Za-z]', w):
        return True  # masks like "4749 5738 4949" or "+91 98765-43210"
    return ' '.join(got.lower().split()) == ' '.join(w.lower().split())


def norm_date(v):
    """yyyy-mm-dd from yyyy-mm-dd, dd/mm/yyyy, dd-mm-yyyy, dd.mm.yyyy (Indian day-first order)"""
    v = str(v or '').strip()
    mo = re.fullmatch(r'(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})', v)
    if mo:
        return f'{mo[1]}-{int(mo[2]):02d}-{int(mo[3]):02d}'
    mo = re.fullmatch(r'(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})', v)
    if mo:
        return f'{mo[3]}-{int(mo[2]):02d}-{int(mo[1]):02d}'
    return ''
