"""Flow recorder and deterministic replayer.

A flow is JSON: {"name", "query", "created", "steps": [...]}. Each step records the action and a target fingerprint
(stable agentmap ID + role + name + region), so a replay resolves the same control on a changed page without an LLM.
Special steps:
  ai_check        - an LLM yes/no question about the page ("is the cart total under 500?"); replay stops when it fails
  human_takeover  - a blocker was cleared by a person; on replay, if the blocker shows up again the run hands off again
"""
import json, os, re, shutil, time, difflib

# saved flows live in the writable, persistent data dir (the app dir is read-only in the container);
# the example flows shipped with the code are copied there once
BUNDLED = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'flows')
FLOWS = os.path.expanduser('~/.agentapp/flows')
try:
    os.makedirs(FLOWS, exist_ok=True)
    if os.path.isdir(BUNDLED):
        for _f in os.listdir(BUNDLED):
            if _f.endswith('.json') and not os.path.exists(os.path.join(FLOWS, _f)):
                shutil.copy(os.path.join(BUNDLED, _f), os.path.join(FLOWS, _f))
except OSError:
    FLOWS = BUNDLED


class Flow:
    def __init__(self, name, query=''):
        self.d = dict(name=name, query=query, created=time.strftime('%Y-%m-%dT%H:%M:%S'), steps=[])

    def add(self, kind, **kw):
        step = dict(i=len(self.d['steps']), t=round(time.time(), 2), kind=kind, **kw)
        self.d['steps'].append(step)
        return step

    @staticmethod
    def target(node):
        if not node:
            return None
        return dict(id=node.get('id'), role=node.get('role'), name=node.get('name'), region=node.get('region'))

    def save(self, path=None):
        os.makedirs(FLOWS, exist_ok=True)
        path = path or os.path.join(FLOWS, re.sub(r'[^a-z0-9]+', '-', self.d['name'].lower())[:50] + '.json')
        json.dump(self.d, open(path, 'w'), indent=1, ensure_ascii=False)
        return path

    @classmethod
    def load(cls, path):
        f = cls('x')
        f.d = json.load(open(path))
        return f


def resolve(m, target):
    """stable ID first, then same role+name in the same region, then same role+name anywhere, then fuzzy name"""
    if not target:
        return None, 'none'
    n = m.by_id.get(target['id'])
    if n and n.get('role') == target['role']:
        return n, 'id'
    same = [x for x in m.by_id.values() if x.get('role') == target['role'] and x.get('name') == target['name']]
    for x in same:
        if x.get('region') == target.get('region'):
            return x, 'role+name+region'
    if same:
        return same[0], 'role+name'
    cands = [x for x in m.by_id.values() if x.get('role') == target['role'] and x.get('name')]
    best = max(cands, key=lambda x: difflib.SequenceMatcher(None, x['name'].lower(), (target['name'] or '').lower()).ratio(),
               default=None)
    if best and difflib.SequenceMatcher(None, best['name'].lower(), (target['name'] or '').lower()).ratio() > 0.75:
        return best, 'fuzzy'
    return None, 'missing'
