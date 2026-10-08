"""Personal values the user gives a task (Aadhaar number, phone, date of birth, OTPs...).

They live only in the Task object in memory. Everything that leaves the task (logs, the event stream, the history and
page map sent to the LLM, the dashboard, saved flows) sees a masked form such as ••••9494 or a {{key}} placeholder.
The executor swaps a placeholder for the real value only at the moment it types into the page.
Passwords and one-time codes are dropped as soon as the task ends and are never written anywhere.
"""
import re

MASK = '••••'
EPHEMERAL_TYPES = {'password', 'otp', 'captcha'}

# known Indian formats: key -> (regex the clean value must match, human description)
FORMATS = {
    'aadhaar_number': (r'\d{12}', '12 digits'),
    'vid': (r'\d{16}', '16 digits'),
    'pan': (r'[A-Z]{5}\d{4}[A-Z]', '10 characters like ABCDE1234F'),
    'mobile': (r'[6-9]\d{9}', '10 digits starting with 6-9'),
    'pincode': (r'[1-9]\d{5}', '6 digits'),
    'email': (r'[^@\s]+@[^@\s]+\.[A-Za-z]{2,}', 'an email address'),
    'enrolment_id': (r'\d{14}', '14 digits'),
}
ALIASES = {  # what an LLM (or a site label) may call the same thing
    'aadhaar_number': r'aadh?aa?r|uid(ai)?\b|aadhar',
    'vid': r'\bvid\b|virtual id',
    'pan': r'\bpan\b',
    'mobile': r'mobile|phone|contact no',
    'pincode': r'pin ?code|postal code',
    'email': r'e-?mail',
    'enrolment_id': r'enrol?ment',
}

SENSITIVE_HINT = re.compile(r'aadh?aa?r|uid|pan\b|mobile|phone|e-?mail|password|otp|pin\b|account|card|cvv|dob|birth|passport|vid\b|captcha', re.I)


def canonical_key(key, label=''):
    k = re.sub(r'[^a-z0-9_]+', '_', (key or '').lower()).strip('_')[:40] or 'value'
    hay = f'{k} {label}'.lower()
    for ck, pat in ALIASES.items():
        if re.search(pat, hay):
            if ck == 'pan' and 'aadh' in hay:
                continue
            return ck
    return k


def clean(key, value):
    v = str(value or '').strip()
    if key in ('aadhaar_number', 'vid', 'mobile', 'pincode', 'enrolment_id'):
        v = re.sub(r'[\s-]', '', v)
        if key == 'mobile':
            v = re.sub(r'^(\+?91|0)(?=[6-9]\d{9}$)', '', v)
    if key == 'pan':
        v = v.upper().replace(' ', '')
    return v


def problem(key, value):
    """None when the value fits the known format for key, else a short sentence saying what is wrong"""
    if key not in FORMATS or not value:
        return None
    pat, desc = FORMATS[key]
    v = clean(key, value)
    if re.fullmatch(pat, v):
        return None
    n = len(re.sub(r'\D', '', v))
    got = f'{n} digits' if n and FORMATS[key][1].endswith('digits') else 'a different format'
    return f'should be {desc}; you gave {got}'


def mask(value, sensitive=True):
    v = str(value or '')
    if not sensitive:
        return v
    if len(v) >= 8:
        return MASK + v[-4:]
    return MASK


class Facts:
    """key -> dict(key, label, value, sensitive, type, source, skipped)"""

    def __init__(self):
        self.d = {}

    def set(self, key, value, label='', sensitive=None, type_='text', source='user'):
        key = canonical_key(key, label)
        value = clean(key, value)
        if sensitive is None:
            sensitive = bool(SENSITIVE_HINT.search(f'{key} {label}')) or key in FORMATS
        old = self.d.get(key, {})
        self.d[key] = dict(key=key, label=label or old.get('label') or key.replace('_', ' '), value=value,
                           sensitive=bool(sensitive or old.get('sensitive')), type=type_ or old.get('type', 'text'),
                           source=source, skipped=False)
        return key

    def skip(self, key, label=''):
        key = canonical_key(key, label)
        self.d.setdefault(key, dict(key=key, label=label or key.replace('_', ' '), value='', sensitive=False,
                                    type='text', source='user'))['skipped'] = True
        return key

    def get(self, key):
        f = self.d.get(canonical_key(key)) or self.d.get(key)
        return f['value'] if f and not f.get('skipped') and f.get('value') != '' else None

    def has(self, key):
        return self.get(key) is not None

    def secrets(self):
        """(value, masked) pairs for redaction, longest first so 12 digits are masked before a 4-digit tail"""
        out = []
        for f in self.d.values():
            v = f.get('value') or ''
            if f.get('sensitive') and len(v) >= 3:
                out.append((v, mask(v)))
                # sites often show the same number spaced (4749 5738 4949): mask those forms too
                if v.isdigit() and len(v) >= 8:
                    out.append((' '.join(v[i:i + 4] for i in range(0, len(v), 4)), mask(v)))
                    out.append(('-'.join(v[i:i + 4] for i in range(0, len(v), 4)), mask(v)))
        return sorted(out, key=lambda p: -len(p[0]))

    def redact(self, text):
        if not text:
            return text
        s = str(text)
        for v, m in self.secrets():
            if v in s:
                s = s.replace(v, m)
        return s

    def substitute(self, text):
        """{{key}} -> real value (only at type time). Returns (text, missing_keys)"""
        missing = []

        def rep(mo):
            v = self.get(mo.group(1))
            if v is None:
                missing.append(mo.group(1))
                return mo.group(0)
            return v
        return re.sub(r'\{\{\s*([a-zA-Z0-9_]+)\s*\}\}', rep, str(text or '')), missing

    def placeholder(self, text):
        """the reverse, for saved flows: real values -> {{key}}"""
        s = str(text or '')
        for f in sorted(self.d.values(), key=lambda f: -len(f.get('value') or '')):
            if f.get('value') and len(f['value']) >= 2 and f['value'] in s:
                s = s.replace(f['value'], '{{' + f['key'] + '}}')
        return s

    def display(self, text):
        """{{key}} -> a readable masked form for people (e.g. "aadhaar number ••••9494")"""
        def rep(mo):
            f = self.d.get(canonical_key(mo.group(1))) or self.d.get(mo.group(1))
            if not f:
                return mo.group(1).replace('_', ' ')
            v = f.get('value') or ''
            return f"{f['label']} {mask(v, f['sensitive'])}".strip() if v else f['label']
        return re.sub(r'\{\{\s*([a-zA-Z0-9_]+)\s*\}\}', rep, str(text or ''))

    def for_llm(self):
        """what the model may see: keys, labels, masked sensitive values, plain non-sensitive ones"""
        lines = []
        for f in self.d.values():
            if f.get('skipped'):
                lines.append(f"- {f['key']} ({f['label']}): the user SKIPPED this; do not invent it")
            elif f.get('value') != '':
                v = mask(f['value']) + f' ({len(f["value"])} chars, hidden from you)' if f['sensitive'] else f'"{f["value"]}"'
                lines.append(f"- {f['key']} ({f['label']}): {v}")
        return '\n'.join(lines)

    def public(self):
        return [dict(key=f['key'], label=f['label'], masked=mask(f['value'], f['sensitive']) if f.get('value') else '',
                     sensitive=f['sensitive'], skipped=f.get('skipped', False)) for f in self.d.values()]

    def drop_ephemeral(self):
        for k in [k for k, f in self.d.items() if f.get('type') in EPHEMERAL_TYPES or
                  re.search(r'otp|password|captcha|one.time', f'{k} {f.get("label", "")}', re.I)]:
            self.d.pop(k, None)


# ---------------- query intake ----------------
INTAKE_PROMPT = """You prepare a user's request for a web-browsing agent. Split it into:
- "search": a short web search query that contains NO personal values (no ID numbers, phone numbers, emails, names of
  the user, dates of birth, addresses, passwords). Fix obvious spelling (e.g. aadhar -> aadhaar).
- "goal": the full task in one sentence, where every personal value is replaced by a {{key}} placeholder.
- "facts": every personal value in the request, as {"key": snake_case, "label": "human label", "value": "exact value as
  given", "sensitive": true|false}. Use these keys when they fit: aadhaar_number, vid, pan, mobile, email, pincode,
  enrolment_id, full_name, date_of_birth, roll_number, registration_number.
Reply with ONE JSON object only: {"search": "...", "goal": "...", "facts": [...]}

REQUEST: """

_BACKSTOP = [
    (r'[\w.+-]+@[\w-]+\.[\w.]{2,}', 'email'),
    (r'\b[A-Za-z]{5}\d{4}[A-Za-z]\b', 'pan'),
    (r'(?:\+?91[\s-]?)?\b[6-9]\d{4}[\s-]?\d{5}\b', 'mobile'),
    (r'\b\d(?:[\s-]?\d){5,}\b', 'number'),   # any run of 6+ digits (ids, account numbers, pincodes)
]


URL_RE = re.compile(r'https?://\S+|\b[\w-]+(\.[\w-]+)+/\S*')


def _sub_outside_urls(pat, fn, s):
    """re.sub that leaves URLs alone (a page address with digits is not a personal value)"""
    spans = [m.span() for m in URL_RE.finditer(s)]
    return re.sub(pat, lambda mo: mo.group(0) if any(a <= mo.start() < b for a, b in spans) else fn(mo), s)


def scrub(text):
    """regex backstop: strip long digit runs, phone numbers, emails and PAN-like codes from text bound for search.
    Returns (clean_text, [(kind, value)])"""
    found = []
    s = str(text or '')
    for pat, kind in _BACKSTOP:
        def rep(mo, kind=kind):
            found.append((kind, mo.group(0)))
            return ' '
        s = _sub_outside_urls(pat, rep, s)
    # drop the dangling "and my aadhar number is" left where a value was cut out
    s = re.sub(r'\b(and\s+|with\s+)?(my|mera|meri)\s+[\w ]{0,30}?\b(number|no\.?|id|is)\b(\s+is)?\s*(?=$|[,.])', '', s.strip(),
               flags=re.I)
    s = re.sub(r'\b(is|number|no\.?|my|and)\s*(?=$|[,.])', '', s.strip(), flags=re.I)
    return ' '.join(s.split()).strip(' ,.-'), found


def mask_text(text):
    """the request as shown in the UI and logs before intake has run: numbers, emails, PANs masked"""
    s = str(text or '')
    for pat, _ in _BACKSTOP:
        s = _sub_outside_urls(pat, lambda mo: mask(mo.group(0).strip()), s)
    return s


def guess_key(context, value):
    """name a value the LLM missed from the words before it"""
    c = context.lower()
    digits = re.sub(r'\D', '', value)
    for ck, pat in ALIASES.items():
        if re.search(pat, c):
            return ck
    if '@' in value:
        return 'email'
    if len(digits) in (12, 13) and not re.search(r'[A-Za-z]', value):
        return 'aadhaar_number'
    if len(digits) == 10 and digits[0] in '6789':
        return 'mobile'
    if len(digits) == 6:
        return 'pincode'
    return 'number'


def intake(query, chat, parse_json, log=None):
    """one LLM call: raw query -> (search, goal, facts list). Falls back to the regex backstop alone."""
    out = {}
    try:
        txt, _ = chat([{'role': 'user', 'content': INTAKE_PROMPT + query}], 'default', 500, 45, None, log)
        out = parse_json(txt)
    except Exception as e:
        if log:
            log(f'intake LLM skipped: {str(e)[:100]}')
    facts = []
    for f in out.get('facts') or []:
        if isinstance(f, dict) and str(f.get('value') or '').strip():
            facts.append(dict(key=str(f.get('key') or 'value'), label=str(f.get('label') or ''), value=str(f['value']).strip(),
                              sensitive=f.get('sensitive')))
    # backstop: any number/email/PAN in the raw query that the LLM did not list becomes a fact too
    _, found = scrub(query)
    have = {re.sub(r'\W', '', f['value']) for f in facts}
    for kind, v in found:
        if re.sub(r'\W', '', v) not in have:
            i = query.find(v)
            key = guess_key(query[max(0, i - 40):i], v) if kind == 'number' else kind
            facts.append(dict(key=key, label='', value=v.strip(), sensitive=True))
            have.add(re.sub(r'\W', '', v))
    search = str(out.get('search') or '').strip() or query
    for f in facts:  # whatever the LLM wrote, no fact value may reach the search engine
        search = search.replace(f['value'], ' ')
    search, _ = scrub(search)
    goal = str(out.get('goal') or '').strip() or query
    for f in facts:
        goal = goal.replace(f['value'], '{{' + canonical_key(f['key'], f['label']) + '}}')
    if not search:
        search = scrub(query)[0] or 'search'
    return search, goal, facts
