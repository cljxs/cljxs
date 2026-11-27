"""
or_chat.py — one chat call to OpenRouter, and what it can cost, in one place.

Used by the Agent Room (agentroom.py) and Stan (stan.py), which both ask a
model for text and let code do everything else. Underscored filename so it
is importable; the scripts around it have hyphens and cannot be.

    prices_from / catalogue   OpenRouter's public price list - free, no key
    worst_case                the most one call can cost, before it is made
    openrouter_call           the call itself, retried only where that can help
    reply_text                (text, finish_reason, usage) from the reply
    extract_json              the JSON object in a model's reply

The credentials format, the endpoint URL, the POST helper and what a call
cost (cost_of) stay in emily-assets.py, which owned them first.

Standard library only.
"""

import importlib.util
import json
import math
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
OR_MODELS = "https://openrouter.ai/api/v1/models"
CHARS_PER_TOKEN = 3        # pessimistic: real text runs nearer 4, so worst cases read high
CALL_TIMEOUT = 300         # seconds for one model reply; a model thinking at length is slow
RETRY_WAITS = (5, 20)      # between tries of a call that failed on the network or a 5xx

# THE PRICE CEILING. OpenRouter's model list gives one price per model, but a
# model is sold by many hosts at different prices, and OpenRouter picks one:
# on 2026-10-15 deepseek-v4-pro was $0.88 to $3.83 per million output tokens
# across 15 hosts, and a reply the worst case put at $0.007 cost $0.018.
# So every call asks for the cheapest host first and REFUSES any host above
# this many times the listed price (OpenRouter's provider.max_price: "will
# prevent your request from running if the price is not available"), and the
# worst case is worked out at that ceiling - the most the call can cost.
PRICE_HEADROOM = 1.5

_ea = None


def ea():
    global _ea
    if _ea is None:
        spec = importlib.util.spec_from_file_location("or_chat_emily_assets", SCRIPTS / "emily-assets.py")
        _ea = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_ea)
    return _ea


def prices_from(data):
    """{model id: (dollars per prompt token, dollars per output token)}.

    A price that is missing, unreadable or negative (openrouter/auto
    publishes "-1") is None - and a model without both prices cannot be used,
    because its worst case cannot be known.
    """
    def price(p, k):
        try:
            v = float((p or {}).get(k))
        except (TypeError, ValueError):
            return None
        return v if v >= 0 else None

    out = {}
    for m in (data or {}).get("data") or []:
        mid = m.get("id")
        if isinstance(mid, str):
            pr = m.get("pricing") or {}
            out[mid] = (price(pr, "prompt"), price(pr, "completion"))
    return out


def catalogue(cache, fetch=None, max_age=6 * 3600):
    """prices_from() OpenRouter's public model list, cached in `cache` for a
    few hours. No key needed, free."""
    cache = Path(cache)
    data = None
    try:
        if time.time() - cache.stat().st_mtime < max_age:
            data = json.loads(cache.read_text())
    except (OSError, ValueError):
        data = None
    if data is None:
        if fetch is None:
            with urllib.request.urlopen(OR_MODELS, timeout=30) as r:
                data = json.loads(r.read())
        else:
            data = fetch()
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(data))
        except OSError:
            pass
    return prices_from(data)


def ceiling(prices, model):
    """(dollars per prompt token, per output token) that a call to `model`
    may be charged at most - the listed price times PRICE_HEADROOM - or None
    when the model's prices are not known."""
    p_in, p_out = prices.get(model, (None, None))
    if p_in is None or p_out is None:
        return None
    return p_in * PRICE_HEADROOM, p_out * PRICE_HEADROOM


def tokens_in(text):
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def worst_case(prices, model, prompt_text, max_tokens):
    """The most this call can cost, in dollars: every character of the prompt
    as a third of a token, and the whole max_tokens written - thinking
    included, which a reasoning model bills as output - at the ceiling price.
    None when the model's prices are not known - unknown is not free."""
    c = ceiling(prices, model)
    if c is None:
        return None
    return tokens_in(prompt_text) * c[0] + max_tokens * c[1]


def extract_json(text):
    """The JSON object in a model's reply, or None.

    Models wrap JSON in ```json fences, or put a sentence before it, or both.
    Tried in order: the whole reply, a fenced block, then the span from the
    first { to the last }.
    """
    if not isinstance(text, str):
        return None
    cands = [text.strip()]
    cands += re.findall(r"```(?:json)?\s*(.*?)```", text, re.S)
    a, b = text.find("{"), text.rfind("}")
    if 0 <= a < b:
        cands.append(text[a:b + 1])
    for c in cands:
        try:
            v = json.loads(c)
        except (ValueError, TypeError):
            continue
        if isinstance(v, dict):
            return v
    return None


def reply_text(data):
    """(text, finish_reason, usage) from an OpenRouter chat completion.

    BELIEVED, not verified: no real reply to these callers has been captured
    yet (no key in the sandbox where this was written). The callers save the
    first real reply per model so it can become a fixture. The shape read
    here is the one test_shop.py uses for OpenRouter's replies elsewhere.
    """
    choice = ((data or {}).get("choices") or [{}])[0] or {}
    msg = choice.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        content = "".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
    return (content or "").strip(), choice.get("finish_reason"), (data or {}).get("usage") or {}


def openrouter_call(key, model, system, user, max_tokens, post=None, wait=time.sleep, title="agentroom",
                    max_price=None):
    """One chat completion. Retries a network failure, a 429 or a 5xx twice;
    never retries a 4xx such as 402 (no credit) - asking again cannot help
    and each try is priced. max_price is ceiling()'s (per prompt token, per
    output token): the cheapest host is tried first, and none above it."""
    req_body = {"model": model, "max_tokens": max_tokens, "usage": {"include": True},
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]}
    if max_price:
        # OpenRouter takes these in dollars per million tokens.
        req_body["provider"] = {"sort": "price", "max_price": {"prompt": round(max_price[0] * 1e6, 6),
                                                               "completion": round(max_price[1] * 1e6, 6)}}
    body = json.dumps(req_body).encode()
    post = post or (lambda req: ea().post(req, timeout=CALL_TIMEOUT))
    last = None
    for attempt in range(len(RETRY_WAITS) + 1):
        req = urllib.request.Request(ea().OR_URL, data=body, headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/cljxs/cljxs", "X-Title": title})
        try:
            return post(req)
        except RuntimeError as e:
            last = e
            m = re.match(r"HTTP (\d{3})", str(e))
            code = int(m.group(1)) if m else 0
            if code and code != 429 and code < 500:
                raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = RuntimeError(f"network: {e}")
        if attempt < len(RETRY_WAITS):
            wait(RETRY_WAITS[attempt])
    raise last
