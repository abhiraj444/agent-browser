# Agent Browser App: plan

Parts: (1) agentmap, (2) Actor (OS-level human-like input, no ChromeDriver), (3) LLM agent loop (Gemini first; OpenRouter, Groq behind one interface),
(4) flow recorder + deterministic replayer (ai_check, human_takeover steps), (5) Chrome MV3 extension (real-browser mode, teach mode, handoff),
(6) Docker cloud runner with noVNC live takeover, (7) web dashboard.

## Milestones
- M1 agentmap capture + outline: DONE (2026-10-06)
- M2 agentmap: region labels, budget folding, expand(), crop() with DPR, ID-keyed diff: DONE (2026-10-07)
- M3 Actor (xdotool/XTEST, Bezier mouse, human typing): DONE 2026-10-07
- M4 LLM agent loop (OpenRouter, free-first model chain): DONE 2026-10-07
- M5 HANDOFF: DONE 2026-10-07 (detector, live view, touch, auto-resume, human_takeover)
- M5b user control: live action stream, Pause/Take over/Resume(+note)/Stop: DONE 2026-10-07
- M6 recorder/replayer, MV3 extension, Docker runner, dashboard: first versions DONE 2026-10-07 (extension untested in real Chrome)
- NEXT: Actor OS backends for Windows/macOS, named tunnel, extension real-browser agent driving
- (old) M5 HANDOFF (core requirement, added 2026-10-07): query -> search -> agentic actions; on a bot check, hand the live session to the user, then resume
- later: recorder/replayer, MV3 extension, cloud runner, dashboard (order to confirm with Abhinav)

## M5 Handoff design (from Abhinav)
1. Runner: headed Chrome on Xvfb, persistent profile, same egress IP for the whole task.
2. Blocker detector after every snapshot: URL has google.com/sorry; text "unusual traffic"; recaptcha / hCaptcha / Turnstile iframe in AX/DOM;
   HTTP 403/429 "Access Denied"; login wall or OTP field. Ambiguous pages go to a cheap LLM classifier.
3. On block: pause agent, freeze browser as-is, notify user, open live view (noVNC now, WebRTC later), phone touch mapping
   (tap=click, drag=scroll, on-screen keyboard), highlight blocker region (agentmap crop/bbox), banner saying what is needed.
4. Resume: auto-detect clearance (URL leaves /sorry, captcha iframe gone, target content visible) or user taps Resume;
   re-snapshot, continue from the same step; log step as human_takeover in the flow.
5. Never automate captcha solving.
6. Reduce blocks: search API first (Brave Search / Serper / Google Programmable Search), browser Google as fallback; warm persistent profile; human pacing.
7. Cookies cannot be moved to the phone (Google clearance is tied to IP + fingerprint), so stream the remote browser instead.
Demo: query -> search -> forced block (google.com/sorry or a recaptcha demo page) -> live-view link -> user clears -> agent opens top result.
agentmap hooks already available: DOMSnapshot boxes + crop(id) for highlighting, diff() for detecting the blocker clearing.
