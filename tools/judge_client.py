import json, math, os, sys, threading, time, urllib.error, urllib.request
from concurrent.futures import ProcessPoolExecutor
sys.path.insert(0, os.path.dirname(__file__))
from math_eval import grade

OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"


def post(url, body, headers=None, timeout=1800):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{url}: HTTP {e.code}: {e.read()[:500]!r}") from None


def within(seconds, fn, *args, **kw):
    import threading
    box = {}

    def run():
        try:
            box["r"] = fn(*args, **kw)
        except BaseException as e:
            box["e"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise RuntimeError(f"no reply after {int(seconds // 60)} min; the request was given up")
    if "e" in box:
        raise box["e"]
    return box["r"]


class Server:
    def __init__(self, url):
        self.url = url.rstrip("/")

    def __call__(self, path, **body):
        for _ in range(3600):
            try:
                return post(self.url + "/api/" + path, body)
            except RuntimeError as e:
                if "HTTP 409" not in str(e):
                    raise
                time.sleep(1)
        raise RuntimeError("server stayed busy for an hour")


def vis(s):
    return s.replace("\n", "⏎").replace(" ", "·")


def bits(t):
    return (t.get("entropy") or 0.0) / math.log(2)


def answer_text(d):
    return "".join(t["text"] for t in d["tokens"])


def resolve(d, a):
    toks = d["tokens"]
    if "pos" in a:
        pos = int(a["pos"])
        if not 0 <= pos < len(toks):
            raise ValueError(f"position {pos} is outside the answer (0..{len(toks) - 1})")
        top = toks[pos]["top"]
        if "choice" in a and str(a["choice"]).strip().lstrip("-").isdigit():
            k = int(a["choice"])
            named = [c for c in top if c["text"].strip() == str(a["choice"]).strip()]
            if not 0 <= k < len(top) and len(named) == 1 and "new" not in a:
                return pos, named[0]["token"]
            if not 0 <= k < len(top):
                raise ValueError(f"choice {k} is not in the candidate list at {pos} (0..{len(top) - 1})")
            return pos, top[k]["token"]
        new = str(a.get("new", a.get("choice", ""))).replace("·", " ").replace("⏎", "\n")
        hit = [c for c in top if c["text"] == new]
        if not hit:
            near = [c for c in top if c["text"].strip() == new.strip()]
            hit = near if len(near) == 1 else []
        if not hit:
            raise ValueError(f"{new!r} is not a candidate at {pos}; candidates: " +
                             ", ".join(f"{k}:{c['text']!r}" for k, c in enumerate(top)))
        return pos, hit[0]["token"]
    before = str(a.get("before", "")).replace("·", " ").replace("⏎", "\n")
    current = str(a.get("current", "")).replace("·", " ").replace("⏎", "\n")
    new = str(a.get("new", "")).replace("·", " ").replace("⏎", "\n")
    starts, n = [], 0
    for t in toks:
        starts.append(n)
        n += len(t["text"])
    full = answer_text(d)
    at = {s: i for i, s in enumerate(starts)}
    found = []
    k = full.find(before)
    while k >= 0:
        i = at.get(k + len(before))
        if i is not None and (not current or toks[i]["text"].strip() == current.strip()):
            found.append(i)
        k = full.find(before, k + 1)
    if not found:
        raise ValueError(f"no token boundary right after {before!r}" + (f" holding {current!r}" if current else "") +
                         " (quote the text exactly, ending where the token starts)")
    if len(found) > 1:
        raise ValueError(f"{before!r} occurs {len(found)} times at a token boundary; quote more text before it")
    return resolve(d, {"pos": found[0], "new": new})


SPEND = {"usd": 0.0, "tok": 0, "cap": float(os.environ.get("EDITOR_COST_CAP") or 0), "lock": threading.Lock()}


def cacheable(messages):
    out, users = [], [i for i, m in enumerate(messages) if m["role"] == "user"][-2:]
    for i, m in enumerate(messages):
        if (m["role"] == "system" or i in users) and isinstance(m["content"], str) and m["content"]:
            out.append({"role": m["role"], "content": [{"type": "text", "text": m["content"], "cache_control": {"type": "ephemeral"}}]})
        else:
            out.append(m)
    return out


class Judge:
    def __init__(self, a):
        self.a = a
        self.key = os.environ.get("OPENROUTER_API_KEY")
        if not self.key:
            sys.exit("OPENROUTER_API_KEY is not set")
        self.cost = [0, 0]
        self.lock = threading.Lock()
        self.no_reasoning = set()
        self.sent = {}

    def system(self):
        return open(self.a.prompt_file).read()

    def ask(self, messages):
        effort = getattr(self.a, "reasoning_effort", "") or ""
        levels = ([{"effort": effort}, {"effort": "low"}] + ([] if getattr(self, "must_reason", False) else [{"enabled": False}])
                  if effort else [{"max_tokens": self.a.reasoning_tokens}, {"enabled": False}])
        level = 1 if id(messages) in self.no_reasoning and not effort else 0
        for attempt in range(14):
            if SPEND["cap"] and SPEND["usd"] >= SPEND["cap"]:
                raise JudgeFailed(f"editor cost cap reached (${SPEND['usd']:.2f} of ${SPEND['cap']:.2f})")
            t0 = time.time()
            try:
                body = {"model": self.a.model, "messages": cacheable(messages) if self.a.model.startswith("anthropic/") else messages,
                        "temperature": self.a.temperature,
                        "max_tokens": self.a.max_reply, "reasoning": levels[level], "usage": {"include": True}}
                if getattr(self.a, "provider_sort", "") and not getattr(self.a, "providers", ""):
                    body["provider"] = {"sort": self.a.provider_sort}
                if getattr(self.a, "providers", ""):
                    body["provider"] = {"order": self.a.providers.split(","), "allow_fallbacks": False}
                r = within(getattr(self.a, "call_deadline", 0) or 1200, post, OPENROUTER, body,
                           headers={"Authorization": f"Bearer {self.key}", "X-Title": "judge-bench editor"}, timeout=600)
                if "choices" not in r:
                    raise RuntimeError(f"OpenRouter: {str(r)[:400]}")
                u = r.get("usage") or {}
                with self.lock:
                    self.cost[0] += u.get("prompt_tokens", 0)
                    self.cost[1] += u.get("completion_tokens", 0)
                with SPEND["lock"]:
                    SPEND["usd"] += float(u.get("cost") or 0)
                ch = r["choices"][0]
                content = ch["message"].get("content") or ""
                reasoning = ch["message"].get("reasoning") or ""
                if not content.strip():
                    if ch.get("finish_reason") == "length" and level + 1 < len(levels):
                        level += 1
                        self.no_reasoning.add(id(messages))
                    raise RuntimeError(f"empty reply (finish_reason={ch.get('finish_reason')}, "
                                       f"{len(reasoning)} chars of reasoning)")
                size = lambda m: len(m["content"]) if isinstance(m["content"], str) else sum(len(p.get("text", "")) for p in m["content"])
                seen = self.sent.get(id(messages), 0)
                new_in = round(int(u.get("prompt_tokens") or 0) * sum(size(m) for m in messages[seen:]) / max(1, sum(size(m) for m in messages)))
                self.sent[id(messages)] = len(messages)
                with SPEND["lock"]:
                    SPEND["tok"] += new_in + int(u.get("completion_tokens") or 0)
                info = {"provider": r.get("provider"), "secs": round(time.time() - t0, 1), "new_input_tokens": new_in,
                        "prompt_tokens": u.get("prompt_tokens"), "completion_tokens": u.get("completion_tokens"),
                        "cached_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens")}
                return content, reasoning, info
            except JudgeFailed:
                raise
            except Exception as e:
                if "cannot be disabled" in str(e) and levels[level] == {"enabled": False}:
                    self.must_reason = True
                    levels, level = levels[:-1], len(levels) - 2
                print(f"editor call failed ({e}); retrying with reasoning {levels[level]}", file=sys.stderr, flush=True)
                time.sleep(min(60, 5 * (attempt + 1)))
        raise JudgeFailed("editor calls keep failing")


class JudgeFailed(Exception):
    pass


class Grader:

    def __init__(self):
        self.pool = ProcessPoolExecutor(max_workers=min(32, max(4, (os.cpu_count() or 8) // 4)))

    def __call__(self, answer, text):
        return self.pool.submit(grade, answer, text).result()
