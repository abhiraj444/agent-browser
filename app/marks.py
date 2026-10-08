"""Set-of-marks: a copy of the viewport screenshot with a small numbered tag on every visible control, plus a legend
mapping each number to its PAGE MAP id, so a vision model can tie what it sees to an id it can act on."""
from PIL import Image, ImageDraw, ImageFont

import agentmap as AM

PRIO = {'textbox': 0, 'searchbox': 0, 'combobox': 0, 'spinbutton': 0, 'checkbox': 1, 'radio': 1, 'listbox': 1,
        'switch': 1, 'button': 2, 'tab': 3, 'menuitem': 3, 'option': 3, 'link': 4}
COLORS = [(214, 40, 40), (33, 87, 196), (20, 120, 70), (150, 60, 170), (190, 100, 0)]


def _font(px):
    for p in ('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf', '/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf',
              '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf'):
        try:
            return ImageFont.truetype(p, px)
        except OSError:
            continue
    return ImageFont.load_default()


def draw(m, out_path, limit=60, extra=()):
    """returns [(number, id)] for the tags drawn; extra = additional {id, box} dicts (e.g. fld: form fields)"""
    shot = (m.cap or {}).get('shot')
    if not shot:
        return []
    px, py, dpr = shot.get('page_x', 0), shot.get('page_y', 0), shot.get('dpr', 1) or 1
    W, H = shot['css_w'], shot['css_h']
    cands, seen = [], set()
    for n in list(m.by_id.values()) + list(extra):
        if not isinstance(n, dict) or n.get('role') not in AM.INTERACTIVE and not str(n.get('id', '')).startswith('fld:'):
            continue
        b = n.get('box')
        if not b or b[2] < 4 or b[3] < 4 or n['id'] in seen:
            continue
        x, y = b[0] - px, b[1] - py
        if x + b[2] < 0 or y + b[3] < 0 or x > W or y > H:
            continue
        seen.add(n['id'])
        cands.append((PRIO.get(n.get('role'), 2), y, x, n, (x, y, b[2], b[3])))
    cands.sort(key=lambda c: (c[0], c[1], c[2]))
    cands = sorted(cands[:limit], key=lambda c: (c[1], c[2]))
    im = Image.open(shot['path']).convert('RGB')
    dr = ImageDraw.Draw(im)
    f = _font(max(11, int(12 * dpr)))
    legend = []
    for i, (_, _, _, n, (x, y, w, h)) in enumerate(cands, 1):
        c = COLORS[i % len(COLORS)]
        X, Y, X2, Y2 = x * dpr, y * dpr, (x + w) * dpr, (y + h) * dpr
        dr.rectangle([X, Y, X2, Y2], outline=c, width=max(1, int(2 * dpr)))
        tag = str(i)
        tw, th = dr.textbbox((0, 0), tag, font=f)[2:]
        tx, ty = max(0, X), max(0, Y - th - 4) if Y - th - 4 >= 0 else Y
        dr.rectangle([tx, ty, tx + tw + 6, ty + th + 4], fill=c)
        dr.text((tx + 3, ty + 1), tag, fill=(255, 255, 255), font=f)
        legend.append((i, n['id']))
    im.save(out_path)
    return legend
