"""Read simple text-in-image captchas for the user's own personal tasks.

Only plain image captchas (distorted letters/digits in an <img> or <canvas> next to a "captcha" field) are read,
by cropping that image and asking the vision model. Interactive challenges (reCAPTCHA, hCaptcha, Cloudflare
Turnstile, puzzles, audio) are never touched: those always go to the person. One automatic try per page, then
the person is asked, so a wrong read never loops.
"""
import asyncio, base64, json, os, re

import llm

INTERACTIVE = re.compile(r'recaptcha|hcaptcha|turnstile|challenges\.cloudflare|arkoselabs|funcaptcha|geetest', re.I)


def enabled():
    return (os.environ.get('AUTO_CAPTCHA') or 'on').strip().lower() not in ('0', 'off', 'false', 'no')


FIND_JS = r"""
(() => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width >= 40 && r.height >= 15 && r.width < 700 && r.height < 300 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const frames = [...document.querySelectorAll('iframe')].map(f => f.src || '').join(' ');
  const hint = el => [el.id, el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className,
                      el.getAttribute('alt'), el.getAttribute('title'), el.getAttribute('src') && el.getAttribute('src').slice(0, 200),
                      el.getAttribute('aria-label'), el.getAttribute('name')].join(' ');
  const cands = [...document.querySelectorAll('img, canvas, svg, [style*="background-image"]')].filter(vis);
  let best = null, bestScore = 0;
  const field = [...document.querySelectorAll('input')].find(i => /captcha|security code|enter (the )?(text|characters|code shown)/i.test(
      [i.id, i.name, i.placeholder, i.getAttribute('aria-label'), (i.labels && i.labels[0] && i.labels[0].innerText) || ''].join(' ')));
  const fr = field && field.getBoundingClientRect();
  for (const el of cands) {
    let s = 0;
    if (/captcha/i.test(hint(el))) s += 5;
    if (/^data:image/i.test(el.getAttribute('src') || '')) s += 1;
    const r = el.getBoundingClientRect();
    if (r.width / r.height > 1.6 && r.width / r.height < 8) s += 1;
    if (fr) { const d = Math.hypot((r.left + r.width / 2) - (fr.left + fr.width / 2), (r.top + r.height / 2) - (fr.top + fr.height / 2));
              if (d < 350) s += 3 - d / 175; }
    if (s > bestScore) { bestScore = s; best = el; }
  }
  if (!best || bestScore < 3) return JSON.stringify({found: false, frames});
  best.scrollIntoView({block: 'center', inline: 'nearest'});
  const r = best.getBoundingClientRect();
  return JSON.stringify({found: true, frames, x: r.left, y: r.top, w: r.width, h: r.height, field: !!field});
})()
"""

PROMPT = ("This picture is (or contains, next to a captcha field) a simple text CAPTCHA from a government or utility website that the account owner is "
          "filling in themselves. Read the characters exactly as shown, left to right. Keep the case of each letter. "
          "Ignore lines, dots and noise. If it is not a text captcha or you cannot read every character, say so. "
          'Reply only JSON: {"text": "<characters, no spaces>", "confident": true|false}')


async def read(browser, log=None):
    """returns (text or None, why)"""
    if not enabled():
        return None, 'automatic captcha reading is off in Settings'
    try:
        r = await browser.cdp.send('Runtime.evaluate', timeout=8, expression=FIND_JS, returnByValue=True)
        info = json.loads(r['result']['value'])
    except Exception as e:
        return None, f'could not look for the captcha image ({e})'
    if INTERACTIVE.search(info.get('frames', '')) and not info.get('found'):
        return None, 'an interactive challenge (reCAPTCHA or similar) needs the person'
    await asyncio.sleep(0.3)
    if info.get('found'):  # crop the captcha <img>/<canvas> found through its HTML (id/class/alt/src or next to the field)
        pad = 4
        kw = dict(clip=dict(x=max(0, info['x'] - pad), y=max(0, info['y'] - pad), width=info['w'] + 2 * pad,
                            height=info['h'] + 2 * pad, scale=2 if info['w'] < 400 else 1), captureBeyondViewport=False)
    else:  # no tagged image: send the whole visible screen and let the model find the captcha in it
        kw = {}
    try:
        shot = await browser.cdp.send('Page.captureScreenshot', timeout=12, format='png', **kw)
        png = base64.b64decode(shot['data'])
    except Exception as e:
        return None, f'could not capture the captcha image ({e})'
    try:
        txt, used = await asyncio.to_thread(llm.chat, [{'role': 'user', 'content': PROMPT}], 'vision', 80, 45, png, log)
        v = llm.parse_json(txt) or {}
    except Exception as e:
        return None, f'the vision model could not read it ({str(e)[:80]})'
    text = re.sub(r'\s+', '', str(v.get('text') or ''))
    if not v.get('confident') or not re.fullmatch(r'[A-Za-z0-9@#$%&*+=?!]{3,10}', text):
        return None, 'the captcha was too hard to read with confidence'
    return text, 'read from the captcha image'
