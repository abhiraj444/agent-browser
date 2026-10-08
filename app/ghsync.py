"""Upload this app's source to a private GitHub repo with a personal access token (Git Data API; no git binary needed).
Token comes from GITHUB_TOKEN (set in dashboard Settings), never from the code. Usage: python3 ghsync.py [owner/repo]"""
import base64, json, os, sys, time, urllib.request, urllib.error

API = 'https://api.github.com'
SKIP_DIRS = {'files', 'downloads', '.hark', '.git', '__pycache__', 'out', 'out_m1', 'node_modules', '.venv', 'ubol'}  # ubol = vendored uBlock Origin Lite (900+ third-party files)
SKIP_FILES = ('_key', '.pem', '.env', 'token')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def call(method, path, body=None, token=None):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                                          'User-Agent': 'agent-browser-sync', 'Content-Type': 'application/json'})
    for attempt in range(4):   # retry dropped connections and GitHub's secondary rate limit
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read() or b'{}')
        except urllib.error.HTTPError as e:
            body = json.loads(e.read() or b'{}')
            if e.code in (403, 429, 502, 503) and 'rate limit' in str(body.get('message', '')).lower() or e.code in (502, 503):
                time.sleep(5 * (attempt + 1)); continue
            return e.code, body
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
            time.sleep(3 * (attempt + 1))
    return 599, {'message': 'network error talking to GitHub after retries'}


def files():
    for d, dirs, fs in os.walk(ROOT):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith('.')]
        for f in fs:
            p = os.path.join(d, f)
            rel = os.path.relpath(p, ROOT)
            if f.endswith(SKIP_FILES) or f.startswith('oci_') or os.path.getsize(p) > 5_000_000:
                continue
            yield rel, p


def sync(repo=None, token=None, message='Sync from agent-browser server'):
    token = token or os.environ.get('GITHUB_TOKEN')
    if not token:
        return {'ok': False, 'error': 'add a GitHub token in Settings first'}
    s, me = call('GET', '/user', token=token)
    if s != 200:
        return {'ok': False, 'error': f'token rejected ({s}): {me.get("message")}'}
    repo = repo or os.environ.get('GITHUB_REPO') or f"{me['login']}/agent-browser"
    owner, name = repo.split('/', 1)
    s, info = call('GET', f'/repos/{repo}', token=token)
    if s == 404:
        s, info = call('POST', '/user/repos', {'name': name, 'private': True, 'auto_init': True,
                                               'description': 'Agent browser app'}, token=token)
        if s >= 300:
            return {'ok': False, 'error': f'could not create repo ({s}): {info.get("message")}'}
    branch = info.get('default_branch', 'main')
    s, ref = call('GET', f'/repos/{repo}/git/ref/heads/{branch}', token=token)
    if s != 200:   # empty repo (created without a README): the Git Data API refuses blobs until a first commit exists
        s, r = call('PUT', f'/repos/{repo}/contents/README.md',
                    {'message': 'Initialize repository', 'branch': branch,
                     'content': base64.b64encode(b'# agent-browser\n').decode()}, token=token)
        if s >= 300:
            return {'ok': False, 'error': f'could not initialize empty repo ({s}): {r.get("message")}'}
        s, ref = call('GET', f'/repos/{repo}/git/ref/heads/{branch}', token=token)
    parent = ref['object']['sha'] if s == 200 else None
    tree, n = [], 0
    for rel, p in files():
        s, b = call('POST', f'/repos/{repo}/git/blobs', {'content': base64.b64encode(open(p, 'rb').read()).decode(),
                                                         'encoding': 'base64'}, token=token)
        if s >= 300:
            return {'ok': False, 'error': f'upload failed on {rel} ({s}): {b.get("message")}'}
        tree.append({'path': rel.replace(os.sep, '/'), 'mode': '100755' if os.access(p, os.X_OK) else '100644',
                     'type': 'blob', 'sha': b['sha']})
        n += 1
    s, t = call('POST', f'/repos/{repo}/git/trees', {'tree': tree}, token=token)   # full snapshot: deleted files disappear
    s, c = call('POST', f'/repos/{repo}/git/commits', {'message': message, 'tree': t['sha'],
                                                       'parents': [parent] if parent else []}, token=token)
    if parent:
        s, r = call('PATCH', f'/repos/{repo}/git/refs/heads/{branch}', {'sha': c['sha'], 'force': True}, token=token)
    else:
        s, r = call('POST', f'/repos/{repo}/git/refs', {'ref': f'refs/heads/{branch}', 'sha': c['sha']}, token=token)
    if s >= 300:
        return {'ok': False, 'error': f'branch update failed ({s}): {r.get("message")}'}
    return {'ok': True, 'repo': f'https://github.com/{repo}', 'files': n, 'commit': c['sha'][:7]}


if __name__ == '__main__':
    a = sys.argv[1:]
    msg = a[a.index('-m') + 1] if '-m' in a else 'Sync from agent-browser server'
    repo = next((x for x in a if '/' in x and x != msg), None)
    print(json.dumps(sync(repo, message=msg)))
