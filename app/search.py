"""Search: API first (fewer bot checks), browser Google as the fallback.

Providers are tried in order when their key is set: Brave Search (BRAVE_API_KEY), Serper (SERPER_API_KEY),
Google Programmable Search (GOOGLE_CSE_KEY + GOOGLE_CSE_CX). With none set, or with SEARCH_MODE=browser,
the agent opens google.com in the real browser, which is also how the handoff demo meets a bot check.
"""
import json, os, urllib.parse, urllib.request


def _get(url, headers=None, data=None):
    req = urllib.request.Request(url, headers=headers or {}, data=data)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def brave(q, n=8):
    d = _get('https://api.search.brave.com/res/v1/web/search?' + urllib.parse.urlencode({'q': q, 'count': n}),
             {'X-Subscription-Token': os.environ['BRAVE_API_KEY'], 'Accept': 'application/json'})
    return [dict(title=r['title'], url=r['url'], snippet=r.get('description', '')) for r in d.get('web', {}).get('results', [])]


def serper(q, n=8):
    d = _get('https://google.serper.dev/search', {'X-API-KEY': os.environ['SERPER_API_KEY'],
                                                 'Content-Type': 'application/json'}, json.dumps({'q': q, 'num': n}).encode())
    return [dict(title=r['title'], url=r['link'], snippet=r.get('snippet', '')) for r in d.get('organic', [])]


def google_cse(q, n=8):
    d = _get('https://www.googleapis.com/customsearch/v1?' + urllib.parse.urlencode(
        {'q': q, 'num': n, 'key': os.environ['GOOGLE_CSE_KEY'], 'cx': os.environ['GOOGLE_CSE_CX']}))
    return [dict(title=r['title'], url=r['link'], snippet=r.get('snippet', '')) for r in d.get('items', [])]


PROVIDERS = [('brave', 'BRAVE_API_KEY', brave), ('serper', 'SERPER_API_KEY', serper), ('google_cse', 'GOOGLE_CSE_KEY', google_cse)]


def api_search(q, log=None):
    if os.environ.get('SEARCH_MODE') == 'browser':
        return None, None
    for name, env, fn in PROVIDERS:
        if os.environ.get(env):
            try:
                res = fn(q)
                if res:
                    return name, res
            except Exception as e:
                if log:
                    log(f'search {name} failed: {e}')
    return None, None


def google_url(q):
    return 'https://www.google.com/search?' + urllib.parse.urlencode({'q': q, 'hl': 'en', 'gl': 'in'})


def results_from_map(m, limit=8):
    """organic results from a Google results page: links whose name starts with an h3 title"""
    import agentmap as AM
    out, seen = [], set()

    def visit(n, p):
        if n['role'] == 'link' and n.get('kids') is not None:
            heads = [k['name'] for k in n.get('kids', []) if k['role'] == 'heading']
            if heads and n['id'] not in seen:
                seen.add(n['id'])
                out.append(dict(title=heads[0], id=n['id'], name=n['name']))
    AM.walk(m.tree, visit)
    return out[:limit]
