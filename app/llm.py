"""One LLM interface over several providers: OpenRouter, Google Gemini (AI Studio), Groq, and any OpenAI-compatible API
(Cerebras, Mistral, DeepSeek, Together, a local Ollama...). Keys come from env or the dashboard's Settings.
Order: OpenRouter free models -> Gemini -> Groq -> custom -> OpenRouter paid fallback. A provider without a key is skipped.

Roles map to models; free models first, paid fallback on rate limits or failures.
Model IDs were checked against https://openrouter.ai/api/v1/models on 2026-10-07. Override any with env vars.
"""
import base64, json, os, re, time, urllib.error, urllib.request

MODELS = {
    'default': os.environ.get('LLM_DEFAULT', 'openrouter/free'),
    'plan': os.environ.get('LLM_PLAN', 'nvidia/nemotron-3-super-120b-a12b:free'),
    'escalate': os.environ.get('LLM_ESCALATE', 'nvidia/nemotron-3-ultra-550b-a55b:free'),
    'vision': os.environ.get('LLM_VISION', 'google/gemma-4-31b-it:free'),
    'fallback': os.environ.get('LLM_FALLBACK', 'google/gemini-3.5-flash-lite'),
}
URL = os.environ.get('OPENROUTER_URL', 'https://openrouter.ai/api/v1/chat/completions')


class LLMError(Exception):
    pass


# OpenAI-compatible providers. Model ids are discovered from each provider's own /models list (never hard-coded guesses);
# set GEMINI_MODEL / GROQ_MODEL / CUSTOM_LLM_MODEL to pin one.
PROVIDERS = {
    'gemini': dict(key='GEMINI_API_KEY', base='https://generativelanguage.googleapis.com/v1beta/openai', model_env='GEMINI_MODEL',
                   prefer=[r'gemini-[\d.]+-flash(?!.*(image|tts|live|audio|preview-tts))', r'gemini-[\d.]+-flash', r'gemini']),
    'groq': dict(key='GROQ_API_KEY', base='https://api.groq.com/openai/v1', model_env='GROQ_MODEL',
                 prefer=[r'gpt-oss-120b', r'llama-4.*maverick', r'llama-3\.3-70b', r'qwen', r'llama']),
    'custom': dict(key='CUSTOM_LLM_KEY', base_env='CUSTOM_LLM_BASE_URL', model_env='CUSTOM_LLM_MODEL', prefer=[r'.']),
}
_model_cache = {}


def _base(p):
    return (os.environ.get(p.get('base_env', ''), '') or p.get('base', '')).rstrip('/')


def provider_model(name):
    p = PROVIDERS[name]
    if not os.environ.get(p['key']) or not _base(p):
        return None
    if os.environ.get(p['model_env']):
        return os.environ[p['model_env']]
    if name in _model_cache:
        return _model_cache[name]
    try:
        req = urllib.request.Request(_base(p) + '/models', headers={'Authorization': f"Bearer {os.environ[p['key']]}"})
        ids = [m['id'].split('/')[-1] for m in json.load(urllib.request.urlopen(req, timeout=15)).get('data', [])]
    except Exception:
        return None  # retried on the next call
    pick = next((i for pat in p['prefer'] for i in sorted(ids, reverse=True) if re.search(pat, i)), None)
    _model_cache[name] = pick
    return pick


def available():
    out = {'openrouter': bool(os.environ.get('OPENROUTER_API_KEY'))}
    for n in PROVIDERS:
        out[n] = provider_model(n)
    return out


def _post(model, messages, max_tokens, timeout, temperature=0.2):
    prov, _, mid = model.partition('::')  # 'gemini::gemini-x-flash' -> a direct provider; plain ids go to OpenRouter
    if not mid:
        prov, mid = 'openrouter', model
    if prov == 'openrouter':
        url, key, extra = URL, os.environ.get('OPENROUTER_API_KEY', ''), {
            'HTTP-Referer': 'https://github.com/abhinav/agent-browser', 'X-Title': 'Agent Browser'}
    else:
        p = PROVIDERS[prov]
        url, key, extra = _base(p) + '/chat/completions', os.environ.get(p['key'], ''), {}
    body = json.dumps(dict(model=mid, messages=messages, max_tokens=max_tokens, temperature=temperature)).encode()
    req = urllib.request.Request(url, data=body, headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json', **extra})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        raise LLMError(f'{model}: HTTP {e.code} {e.read()[:200]!r}')
    except Exception as e:
        raise LLMError(f'{model}: {e}')
    if 'error' in d:
        raise LLMError(f"{model}: {d['error']}")
    msg = d['choices'][0]['message']
    text = msg.get('content') or ''
    if not text.strip():
        raise LLMError(f'{model}: empty reply')
    return text, d.get('model', model), d.get('usage', {})


def chat(messages, role='default', max_tokens=900, timeout=60, image_png=None, log=None):
    """Try the role's model, then (for plan) the escalation model, then the paid fallback. Returns (text, model)."""
    if image_png is not None:
        role = 'vision'
        b64 = base64.b64encode(image_png).decode()
        last = messages[-1]
        messages = messages[:-1] + [dict(role=last['role'], content=[
            {'type': 'text', 'text': last['content']},
            {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{b64}'}}])]
    has_or = bool(os.environ.get('OPENROUTER_API_KEY'))
    chain = [MODELS[role]] if has_or else []
    if role == 'escalate' and has_or:
        chain = [MODELS['escalate'], MODELS['plan']]
    for n in PROVIDERS:  # Gemini / Groq / custom keys the user added, all OpenAI-compatible
        pm = provider_model(n)
        if pm and not (image_png is not None and n == 'groq'):  # most Groq models are text-only
            chain.append(f'{n}::{pm}')
    if has_or:
        chain.append(MODELS['fallback'])
    if not chain:
        raise LLMError('no LLM key set: add one in the dashboard Settings')
    errs = []
    for m in chain:
        t = time.time()
        try:
            text, used, usage = _post(m, messages, max_tokens, timeout)
            if log:
                log(f'llm {used} {time.time() - t:.1f}s')
            return text, used
        except LLMError as e:
            errs.append(str(e)[:160])
            if log:
                log(f'llm fail {str(e)[:120]}')
    raise LLMError(' | '.join(errs))


def parse_json(text):
    """first JSON object in a reply (tolerates fences, prose and trailing commas)"""
    t = re.sub(r'```(?:json)?', '', text)
    t = re.sub(r'<think>.*?</think>', '', t, flags=re.S)
    start = t.find('{')
    while start != -1:
        depth, instr, esc = 0, False, False
        for i in range(start, len(t)):
            c = t[i]
            if instr:
                if esc:
                    esc = False
                elif c == '\\':
                    esc = True
                elif c == '"':
                    instr = False
            elif c == '"':
                instr = True
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    s = re.sub(r',\s*([}\]])', r'\1', t[start:i + 1])
                    try:
                        return json.loads(s)
                    except Exception:
                        break
        start = t.find('{', start + 1)
    raise LLMError(f'no JSON in reply: {text[:200]!r}')
