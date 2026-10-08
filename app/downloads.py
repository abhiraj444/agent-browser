"""Files the agent downloads for a task (an e-Aadhaar PDF, a receipt, an admit card).

Chrome saves every download as <guid> under ~/.agentapp/downloads/<task_id>/ (CDP Browser.setDownloadBehavior
allowAndName); when it completes we rename it to the site's suggested name, sanitised and de-duplicated, and record it
on the task. PDFs that open in Chrome's viewer instead of downloading are saved from the response, and any page can be
saved with Page.printToPDF. Files are private: served only behind the dashboard token, never logged by content, never
sent to the LLM (only name, size and type), never synced to GitHub, and deleted after FILE_RETENTION_HOURS (24 h).
"""
import asyncio, hashlib, mimetypes, os, re, shutil, time

ROOT = os.path.expanduser('~/.agentapp/downloads')


def retention_hours():
    try:
        return max(1, min(24 * 30, float(os.environ.get('FILE_RETENTION_HOURS') or 24)))
    except ValueError:
        return 24


def task_dir(task_id):
    if not re.fullmatch(r'[a-f0-9]{8}', task_id or ''):
        raise ValueError('bad task id')
    return os.path.join(ROOT, task_id)


def safe_name(name, fallback='download'):
    name = os.path.basename(str(name or '').replace('\\', '/'))
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', '_', name).strip(' .')
    name = re.sub(r'\s+', ' ', name)[:120]
    return name or fallback


def unique(d, name):
    base, ext = os.path.splitext(name)
    p, k = os.path.join(d, name), 2
    while os.path.exists(p):
        p, k = os.path.join(d, f'{base} ({k}){ext}'), k + 1
    return p


def describe(path, url=''):
    data_hash = hashlib.sha256()
    with open(path, 'rb') as f:
        head = f.read(2048)
        data_hash.update(head)
        for chunk in iter(lambda: f.read(1 << 20), b''):
            data_hash.update(chunk)
    mime = mimetypes.guess_type(path)[0] or ('application/pdf' if head.startswith(b'%PDF') else 'application/octet-stream')
    info = dict(name=os.path.basename(path), size=os.path.getsize(path), mime=mime, url=url[:300],
                sha256=data_hash.hexdigest(), time=round(time.time(), 1))
    if head.startswith(b'%PDF'):
        info['encrypted'] = pdf_encrypted(path)
    return info


def pdf_encrypted(path):
    """password-protected PDFs carry an /Encrypt entry in their trailer (usually near the end of the file)"""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            if size > 4096:
                f.seek(size - 4096)
            tail = f.read()
            if b'/Encrypt' in tail:
                return True
            f.seek(0)
            return b'/Encrypt' in f.read(min(size, 2 << 20))
    except OSError:
        return False


def save_bytes(task_id, name, data, url='', mime=''):
    d = task_dir(task_id)
    os.makedirs(d, exist_ok=True)
    nm = safe_name(name)
    if '.' not in nm and mime:
        nm += mimetypes.guess_extension(mime.split(';')[0].strip()) or ''
    p = unique(d, nm)
    with open(p, 'wb') as f:
        f.write(data)
    return describe(p, url)


def resolve(task_id, name):
    """absolute path of a task's file, or None (no path traversal: the name must be a plain file in the task dir)"""
    try:
        d = os.path.realpath(task_dir(task_id))
    except ValueError:
        return None
    if not name or name != os.path.basename(name) or name.startswith('.'):
        return None
    p = os.path.realpath(os.path.join(d, name))
    if os.path.dirname(p) != d or not os.path.isfile(p):
        return None
    return p


def list_files(task_id):
    try:
        d = task_dir(task_id)
    except ValueError:
        return []
    if not os.path.isdir(d):
        return []
    return [f for f in sorted(os.listdir(d)) if not f.startswith('.') and os.path.isfile(os.path.join(d, f))
            and not re.fullmatch(r'[0-9a-f-]{36}', f)]  # unfinished guid files are not deliverable yet


def cleanup(now=None):
    """delete files older than the retention period; returns how many went"""
    now, gone = now or time.time(), 0
    limit = retention_hours() * 3600
    if not os.path.isdir(ROOT):
        return 0
    for t in os.listdir(ROOT):
        d = os.path.join(ROOT, t)
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            p = os.path.join(d, f)
            try:
                if now - os.path.getmtime(p) > limit:
                    os.remove(p)
                    gone += 1
            except OSError:
                pass
        try:
            if not os.listdir(d):
                os.rmdir(d)
        except OSError:
            pass
    return gone


class Downloads:
    """routes Chrome's download events to the running task"""

    def __init__(self):
        self.task = None
        self.pending = {}   # guid -> dict(url, name, task)

    def on_event(self, d):
        m, p = d.get('method'), d.get('params') or {}
        if m == 'Browser.downloadWillBegin':
            t = self.task
            self.pending[p['guid']] = dict(url=p.get('url', ''), name=p.get('suggestedFilename') or 'download', task=t)
            if t:
                t.emit('download', f"download started: {safe_name(p.get('suggestedFilename'))}")
        elif m == 'Browser.downloadProgress':
            st = p.get('state')
            if st == 'completed':
                asyncio.get_event_loop().create_task(self._finish(p['guid']))
            elif st == 'canceled':
                info = self.pending.pop(p['guid'], None)
                if info and info['task']:
                    info['task'].emit('download', f"download cancelled: {safe_name(info['name'])}")

    async def _finish(self, guid):
        info = self.pending.pop(guid, None)
        if not info or not info['task']:
            return
        t = info['task']
        d = task_dir(t.id)
        src = os.path.join(d, guid)
        for _ in range(20):  # the progress event can beat the final rename/flush by a moment
            if os.path.exists(src):
                break
            await asyncio.sleep(0.1)
        if not os.path.exists(src):
            t.emit('download', f"download finished but the file is missing: {safe_name(info['name'])}")
            return
        dst = unique(d, safe_name(info['name']))
        shutil.move(src, dst)
        rec = await asyncio.to_thread(describe, dst, info['url'])
        t.add_file(rec)
