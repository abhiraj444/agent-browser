"""Blocker detector. Runs after every snapshot. Never solves anything: it only decides whether a human is needed.

Signals: google.com/sorry URL, "unusual traffic" text, reCAPTCHA / hCaptcha / Turnstile / Cloudflare challenge iframes,
403/429 or "Access Denied", login walls and OTP fields. Ambiguous pages go to a cheap vision classifier.
"""
import re
from browser import iframes
import agentmap as AM

CAPTCHA_SRC = [
    (r'google\.com/recaptcha|recaptcha\.net|gstatic\.com/recaptcha', 'reCAPTCHA'),
    (r'hcaptcha\.com', 'hCaptcha'),
    (r'challenges\.cloudflare\.com', 'Cloudflare Turnstile'),
    (r'captcha-delivery\.com|geo\.captcha', 'DataDome captcha'),
    (r'arkoselabs|funcaptcha', 'Arkose challenge'),
]
TEXT_SIGNS = [
    (r'unusual traffic from your computer', 'google_sorry', 'Google thinks the traffic looks automated'),
    (r"i'?m not a robot|verify (that )?you are (a )?human|are you a robot|press (&|and) hold", 'captcha', 'a human check'),
    (r'checking (if the site connection is secure|your browser)|just a moment\.\.\.', 'challenge', 'a browser check'),
    (r'access denied|request blocked|you don.t have permission to access', 'denied', 'the site blocked this IP'),
]


class Blocker(dict):
    """kind, label, needs (banner text), box (viewport CSS px or None), url, auto_clear (bool)"""


def _text_of(m, limit=6000):
    out = []
    AM.walk(m.tree, lambda k, p: out.append(k.get('name', '')) if k.get('name') else None)
    s = ' '.join(out)
    return s[:limit * 4].lower()


async def detect(browser, m, llm_classify=None):
    url = m.cap['url']
    title = (m.cap.get('title') or '').lower()
    text = _text_of(m)
    status = m.cap.get('status')
    # 1. Google sorry page
    if re.search(r'google\.[a-z.]+/sorry', url) or 'unusual traffic from your computer' in text:
        box = await _captcha_box(browser, m)
        return Blocker(kind='google_sorry', label='Google bot check', url=url, box=box, auto_clear=True,
                       needs="Google wants to be sure you're human. Tick the box or solve the picture puzzle, then the agent continues on its own.")
    # 2. captcha iframes
    for src, be, ttl in iframes(m.dom):
        for pat, name in CAPTCHA_SRC:
            if re.search(pat, src or ''):
                # invisible reCAPTCHA v3 badges are not blockers: only count frames with a real size
                if 'size=invisible' in (src or ''):
                    continue  # invisible reCAPTCHA (badge / score only): nothing for a human to do
                box = await browser.quad(be) if be else None
                if not box or box[2] * box[3] < 2500 or box[2] < 60 or box[3] < 40:
                    continue  # hidden, unlaid-out or tiny frame: a challenge that isn't showing yet
                return Blocker(kind='captcha', label=name, url=url, box=box, auto_clear=True,
                               needs=f"Tick the {name} box (solve the pictures if it asks), then press the page's "
                                     "Submit / Continue button. The agent notices and carries on.")
    # 3. HTTP status / denied
    if status in (403, 429) or 'access denied' in title:
        return Blocker(kind='denied', label=f'HTTP {status or "403"} / Access Denied', url=url, box=None, auto_clear=False,
                       needs='This site blocked the cloud browser. You can try reloading, or tap Resume to let the agent try another route.')
    # 4. OTP / login wall
    ctl = [n for n in m.by_id.values() if n['role'] in AM.INTERACTIVE]
    otp = [n for n in ctl if n['role'] in ('textbox', 'spinbutton') and
           re.search(r'\b(otp|one[- ]time|verification code|enter (the )?code)\b', n['name'] or '', re.I)]
    if otp:
        box = await browser.quad(otp[0]['be']) if otp[0].get('be') else None
        return Blocker(kind='otp', label='One-time code', url=url, box=box, auto_clear=False,
                       needs='Enter the code sent to your phone or email, then tap Resume.')
    pw = [n for n in ctl if n['role'] == 'textbox' and re.search(r'password|passcode|\bpin\b', n['name'] or '', re.I)]
    if pw and len(ctl) < 40:
        box = await browser.quad(pw[0]['be']) if pw[0].get('be') else None
        return Blocker(kind='login', label='Sign-in wall', url=url, box=box, auto_clear=False,
                       needs='Sign in here (the agent never sees your password), then tap Resume.')
    # 5. text signs on small pages
    for pat, kind, why in TEXT_SIGNS:
        if re.search(pat, text) or re.search(pat, title):
            if len(ctl) < 25:
                box = await _captcha_box(browser, m)
                return Blocker(kind=kind, label=why, url=url, box=box, auto_clear=kind != 'denied',
                               needs=f'The page shows {why}. Clear it, and the agent continues.')
    # 6. ambiguous: tiny page with check-ish words -> cheap classifier
    if llm_classify and len(ctl) < 8 and re.search(r'verif|robot|human|security|blocked|captcha', text + title):
        verdict = await llm_classify(m)
        if verdict:
            return Blocker(kind='classified', label=verdict, url=url, box=None, auto_clear=False,
                           needs=f'{verdict}. Please clear it, then tap Resume.')
    return None


async def _captcha_box(browser, m):
    for src, be, ttl in iframes(m.dom):
        if any(re.search(p, src or '') for p, _ in CAPTCHA_SRC) and be:
            b = await browser.quad(be)
            if b and b[2] * b[3] >= 2500:
                return b
    # Google /sorry: the form holding the captcha
    for n in m.by_id.values():
        if n['role'] == 'form' and n.get('be'):
            return await browser.quad(n['be'])
    return None


async def still_blocked(browser, blocker):
    """cheap re-check used while the user works: URL left /sorry, captcha iframe gone, page content back"""
    m = await browser.snapshot()
    b = await detect(browser, m)
    return b, m
