# Agent Browser: runner, agent, handoff, dashboard

## Start the runner
    ./setup.sh                                   # Debian/Ubuntu/WSL: chromium, Xvfb, x11vnc, noVNC, xdotool, cloudflared
    OPENROUTER_API_KEY=... python3 main.py --tunnel
It prints `READY https://<random>.trycloudflare.com/?k=<key>`. Open that link on your phone: the key is stored in a cookie, and every page, API call and the live screen sit behind it.
Docker: `docker compose -f app/docker-compose.yml up --build`. The profile and key live in a volume.

## Parts
| file | what it does |
|---|---|
| browser.py | Headed Chrome in kiosk mode on Xvfb with a persistent profile. One process means one IP for the whole task. Passive CDP only (Page, DOM, DOMSnapshot, Accessibility, read-only Network). Follows new tabs. |
| actor.py | Human-like OS input through XTEST (xdotool): Bezier mouse paths, eased timing, per-key typing delays, wheel scrolling into view. No ChromeDriver and no CDP Input. |
| llm.py | OpenRouter, one interface. default `openrouter/free`; plan `nvidia/nemotron-3-super-120b-a12b:free`; escalate `nvidia/nemotron-3-ultra-550b-a55b:free`; vision `google/gemma-4-31b-it:free`; paid fallback `google/gemini-3.5-flash-lite`. Override with LLM_* env vars. |
| search.py | Brave, Serper or Google Programmable Search when a key is set. Otherwise it falls back to Google in the real browser. |
| blockers.py | Runs after every snapshot. Detects google.com/sorry, "unusual traffic", reCAPTCHA, hCaptcha, Turnstile, DataDome and Arkose frames, 403/429 Access Denied, OTP fields and login walls. Ambiguous pages go to a vision classifier. It never solves a captcha. |
| agent.py | Query → search → observe/act loop. On a blocker it pauses with the browser untouched, sends a notification (ntfy, if NTFY_TOPIC is set), and opens a live view with the blocker highlighted. It resumes on auto-detect or Resume, and the step is logged as `human_takeover`. Pause, Take over, Resume (with a note) and Stop are available at any time. |
| flow.py | Every run is recorded as JSON. The replayer resolves targets by stable ID, then role+name+region, then fuzzy matching, so no LLM is needed. Steps can be `ai_check` (an LLM yes/no) or `human_takeover`. |
| server.py, web/ | Dashboard, live view (noVNC over a same-origin websocket bridge), phone touch layer (tap = click, drag = scroll, keyboard), action stream, Pause/Resume/Stop. |
| ext.py, extension/ | MV3 extension with three modes. Real-browser mode: snapshots through chrome.debugger with the same passive domains. Teach mode: records your clicks and typing as a flow. Handoff: a notification plus a Resume button in your own Chrome. |

## Phone controls (live view)
Pause & take over stops the agent before its next action, and you drive. Resume (with an optional note) restarts from whatever page you left it on. The page is re-snapshotted, and your note goes into the agent's context. Stop ends the task at once. The action stream shows every snapshot, model and thought, click (with its coordinates), keystroke, blocker, pause and resume. Typed text from the live view is not logged.

## Laptop (from ~9 Oct)
Run the same `main.py` under WSL2 or Linux. On native Windows or macOS, Actor needs its OS backend (SendInput or CGEvent), which is the next item. The other option is the extension in your everyday Chrome on your home IP. Home IPs get far fewer bot checks than this datacenter sandbox.

## Notes
- Cookies are not moved to the phone, because Google's clearance is tied to the IP and fingerprint. The remote browser is streamed to the phone instead.
- Quick-tunnel URLs change on every restart. Use a named Cloudflare tunnel for a fixed URL.
