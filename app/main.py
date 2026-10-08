"""Start the runner: Xvfb display -> x11vnc -> headed Chrome (persistent profile) -> agent worker -> web server
[-> Cloudflare quick tunnel so a phone can open it].

  python3 main.py [--port 8080] [--tunnel] [--display :99]
Env: OPENROUTER_API_KEY, optional BRAVE_API_KEY / SERPER_API_KEY / GOOGLE_CSE_KEY+GOOGLE_CSE_CX, NTFY_TOPIC, APP_TOKEN.
"""
import argparse, asyncio, os, re, shutil, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from aiohttp import web  # noqa: E402
from browser import Browser  # noqa: E402
from actor import Actor  # noqa: E402
from agent import Runner  # noqa: E402
from ext import ExtBridge  # noqa: E402
from server import make_app  # noqa: E402

procs = []


def spawn(args, **kw):
    p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    procs.append(p)
    return p


def display_up(display):
    return subprocess.run(['xdpyinfo', '-display', display], capture_output=True).returncode == 0 \
        if shutil.which('xdpyinfo') else os.path.exists(f'/tmp/.X11-unix/X{display.lstrip(":")}')


def start_display(display, w, h):
    if not display_up(display):
        spawn(['Xvfb', display, '-screen', '0', f'{w}x{h}x24', '-nolisten', 'tcp', '-ac'])
        for _ in range(40):
            if display_up(display):
                break
            time.sleep(0.1)
    # VNC server on localhost only; the web server bridges it to noVNC behind the token
    subprocess.run(['pkill', '-f', f'x11vnc.*{display}'], capture_output=True)
    spawn(['x11vnc', '-display', display, '-localhost', '-rfbport', os.environ.get('VNC_PORT', '5900'), '-forever',
           '-shared', '-nopw', '-quiet', '-noxdamage', '-cursor', 'arrow'])


def start_tunnel(port):
    exe = shutil.which('cloudflared') or os.path.expanduser('~/.agentapp/cloudflared')
    if not os.path.exists(exe):
        print('cloudflared not found; run setup.sh', flush=True)
        return None
    log = open('/tmp/agentapp_tunnel.log', 'w')
    procs.append(subprocess.Popen([exe, 'tunnel', '--no-autoupdate', '--protocol', 'http2', '--url', f'http://127.0.0.1:{port}'],
                                  stdout=log, stderr=subprocess.STDOUT))
    for _ in range(60):
        time.sleep(0.5)
        m = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', open('/tmp/agentapp_tunnel.log').read())
        if m:
            return m.group(0)
    return None


def stop_previous():
    """one runner per machine: stop the last one (and its Chrome/tunnel) before starting"""
    pf = os.path.expanduser('~/.agentapp/runner.pids')
    if os.path.exists(pf):
        for pid in open(pf).read().split():
            try:
                os.kill(int(pid), 15)
            except (OSError, ValueError):
                pass
        time.sleep(1.5)


def save_pids(extra):
    os.makedirs(os.path.expanduser('~/.agentapp'), exist_ok=True)
    open(os.path.expanduser('~/.agentapp/runner.pids'), 'w').write(' '.join(map(str, [os.getpid()] + extra)))


ENV_FILE = os.path.expanduser('~/.agentapp/env')


def load_env_file():
    # keys pasted in the dashboard's Settings (OPENROUTER_API_KEY, NTFY_TOPIC, ...); real env vars win
    try:
        for line in open(ENV_FILE):
            k, _, v = line.strip().partition('=')
            if k and v and not os.environ.get(k):
                os.environ[k] = v
    except FileNotFoundError:
        pass


def announce(url):
    topic = os.environ.get('NTFY_TOPIC')
    if topic:
        try:
            import urllib.request
            urllib.request.urlopen(urllib.request.Request(f'https://ntfy.sh/{topic}', data=f'Agent Browser is up: {url}'.encode(),
                                                          headers={'Title': 'Agent Browser', 'Click': url}), timeout=10)
        except Exception as e:
            print('ntfy failed:', e, flush=True)


async def main():
    stop_previous()
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=int(os.environ.get('PORT', 8080)))
    ap.add_argument('--display', default=os.environ.get('DISPLAY_NUM', ':99'))
    ap.add_argument('--tunnel', action='store_true')
    ap.add_argument('--width', type=int, default=1366)
    ap.add_argument('--height', type=int, default=900)
    a = ap.parse_args()
    load_env_file()
    os.environ.setdefault('NO_PROXY', '127.0.0.1,localhost')
    start_display(a.display, a.width, a.height)
    public = start_tunnel(a.port) if a.tunnel else f'http://127.0.0.1:{a.port}'
    browser = await Browser(display=a.display, width=a.width, height=a.height).start()
    actor = Actor(browser, a.display)
    runner = Runner(browser, actor, public or '')
    actor.on_event = lambda kind, text, **d: runner.current and runner.current.emit(kind, text, **d)
    ext = ExtBridge()
    ext.resume_cb = lambda: runner.current and runner.current.resume_evt.set()
    app, token = make_app(runner, ext)
    save_pids([p.pid for p in procs] + [browser.proc.pid])
    asyncio.create_task(runner.worker())
    rn = web.AppRunner(app)
    await rn.setup()
    await web.TCPSite(rn, '0.0.0.0', a.port).start()
    def publish(pub):
        url = f'{pub}/?k={token}'
        open(os.path.expanduser('~/.agentapp/url'), 'w').write(url)
        print(f'READY {url}', flush=True)
        announce(url)

    if public:
        publish(public)
    else:
        # quick-tunnel creation can be refused (e.g. 429 after many restarts): keep the app up and retry with backoff
        print(f'tunnel unavailable; local dashboard on port {a.port}, retrying tunnel', flush=True)

        async def retry_tunnel():
            delay = 60
            while True:
                await asyncio.sleep(delay)
                for p in list(procs):
                    if 'cloudflared' in ' '.join(getattr(p, 'args', [])) and p.poll() is not None:
                        procs.remove(p)
                pub = await asyncio.to_thread(start_tunnel, a.port)
                if pub:
                    runner.public_url = pub
                    publish(pub)
                    return
                delay = min(delay * 2, 900)
        asyncio.create_task(retry_tunnel())
    await asyncio.Event().wait()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    finally:
        for p in procs:
            p.terminate()
