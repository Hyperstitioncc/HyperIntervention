"""An LLM editor that steers manual-rl's model through the `ui` server, one token at a time (vLLM venv).

    OPENROUTER_API_KEY=... llm_editor.py --prompts LIST.jsonl --log DIR [--apply-every 8]

For each problem of the server's list, in order: open it (the server generates the model's greedy
answer); if that already grades correct, skip it. Otherwise show the editor the problem, the reference
answer and worked solution, and the model's answer with its branch points (positions whose entropy is
high) and their top candidates. The editor picks one candidate at one position; the server forks there
and the model rewrites the rest. Repeat until the answer grades correct (math_verify, as in the eval),
the editor gives up, or --max-edits. Solved problems are committed to the run's collected.jsonl (no
training); the rest are dropped. Every --apply-every solved problems the gradient is applied (0 = never).

The editor's instructions are --prompt-file, reread before every call: edit it while this runs.
Short notes go to the server's editor log (the page shows them); every call is in DIR/calls.jsonl.
Touch DIR/STOP to finish the current problem and stop.
"""
import argparse, json, math, os, re, sys, threading, time, urllib.error, urllib.request
from concurrent.futures import ProcessPoolExecutor
sys.path.insert(0, os.path.dirname(__file__))
from math_eval import grade, last_boxed, load

OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"


def post(url, body, headers=None, timeout=1800):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{url}: HTTP {e.code}: {e.read()[:500]!r}") from None


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

    def note(self, text):
        print(text, file=sys.stderr, flush=True)
        try:
            self("note", text=text)
        except Exception as e:  # noqa: BLE001 (a missing note must not stop the run)
            print(f"(note failed: {e})", file=sys.stderr)


def vis(s):
    return s.replace("\n", "⏎").replace(" ", "·")


def bits(t):
    return (t.get("entropy") or 0.0) / math.log(2)


def answer_text(d):
    return "".join(t["text"] for t in d["tokens"])


def render(d, tau, max_branch, show):
    toks = d["tokens"]
    hot = sorted((i for i, t in enumerate(toks) if bits(t) >= tau), key=lambda i: -bits(toks[i]))[:max_branch]
    hot = sorted(hot)
    marked = set(hot)
    text = "".join((f"⟦{i}⟧" if i in marked else "") + t["text"] for i, t in enumerate(toks))
    rows = []
    for i in hot:
        t = toks[i]
        cands = "  ".join(f"{k}:{vis(c['text'])!r} {c['prob']:.1%}" for k, c in enumerate(t["top"][:show]))
        rows.append(f"{i} | H={bits(t):.2f} bits | now {vis(t['text'])!r} {t['prob']:.1%} | {cands}")
    return text, "\n".join(rows) or "(none above the threshold)"


def describe(d, ok):
    boxed = last_boxed(answer_text(d))
    return (f"{len(d['tokens'])} tokens, ended by {d['ended_by']}; final boxed answer: {boxed!r}; "
            f"{'CORRECT' if ok else 'NOT correct'}")


def picks_line(d):
    if not d["picks"]:
        return "none"
    toks = d["tokens"]
    return ", ".join(f"@{p['pos']} {vis(p['original_text'])!r}→{vis(toks[p['pos']]['text'])!r}" for p in d["picks"])


def parse_action(text):
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    cand = blocks[-1] if blocks else None
    if cand is None:
        end = text.rfind("}")
        depth = 0
        for k in range(end, -1, -1):
            depth += {"}": 1, "{": -1}.get(text[k], 0)
            if depth == 0:
                cand = text[k:end + 1]
                break
    if cand is None:
        raise ValueError("no JSON object in the reply")
    return json.loads(cand)


def resolve(d, a):
    toks = d["tokens"]
    if "pos" in a:
        pos = int(a["pos"])
        if not 0 <= pos < len(toks):
            raise ValueError(f"position {pos} is outside the answer (0..{len(toks) - 1})")
        top = toks[pos]["top"]
        if "choice" in a:
            k = int(a["choice"])
            if not 0 <= k < len(top):
                raise ValueError(f"choice {k} is not in the candidate list at {pos} (0..{len(top) - 1})")
            return pos, top[k]["token"]
        new = str(a.get("new", "")).replace("·", " ").replace("⏎", "\n")
        hit = [c for c in top if c["text"] == new]
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


class Editor:
    def __init__(self, a):
        self.a = a
        self.key = os.environ.get("OPENROUTER_API_KEY")
        if not self.key:
            sys.exit("OPENROUTER_API_KEY is not set")
        self.cost = [0, 0]
        self.lock = threading.Lock()

    def system(self):
        return open(self.a.prompt_file).read()

    def ask(self, messages):
        # DeepSeek ignores reasoning.max_tokens (hard problems ran 60-120k chars and got cut off), and
        # effort "low" ran just as long, so a reply that is all reasoning retries with reasoning off.
        levels = [{"max_tokens": self.a.reasoning_tokens}, {"enabled": False}]
        level = 0
        for attempt in range(6):
            t0 = time.time()
            try:
                r = post(OPENROUTER, {"model": self.a.model, "messages": messages, "temperature": self.a.temperature,
                                      "max_tokens": self.a.max_reply, "reasoning": levels[level],
                                      # one provider, so its prompt cache holds the system prompt and each
                                      # conversation's history (unpinned, calls hopped between providers,
                                      # missing the cache, and some ran at 5-15 tok/s)
                                      "provider": {"order": self.a.providers.split(","), "allow_fallbacks": False}},
                         headers={"Authorization": f"Bearer {self.key}", "X-Title": "manual-rl editor"}, timeout=600)
                if "choices" not in r:
                    raise RuntimeError(f"OpenRouter: {str(r)[:400]}")
                u = r.get("usage") or {}
                with self.lock:
                    self.cost[0] += u.get("prompt_tokens", 0)
                    self.cost[1] += u.get("completion_tokens", 0)
                ch = r["choices"][0]
                content = ch["message"].get("content") or ""
                reasoning = ch["message"].get("reasoning") or ""
                if not content.strip():
                    if ch.get("finish_reason") == "length" and level + 1 < len(levels):
                        level += 1
                    raise RuntimeError(f"empty reply (finish_reason={ch.get('finish_reason')}, "
                                       f"{len(reasoning)} chars of reasoning)")
                info = {"provider": r.get("provider"), "secs": round(time.time() - t0, 1),
                        "prompt_tokens": u.get("prompt_tokens"), "completion_tokens": u.get("completion_tokens"),
                        "cached_tokens": (u.get("prompt_tokens_details") or {}).get("cached_tokens")}
                return content, reasoning, info
            except Exception as e:  # noqa: BLE001 (retry transient API failures)
                print(f"editor call failed ({e}); retrying with reasoning {levels[level]}", file=sys.stderr, flush=True)
                time.sleep(5 * (attempt + 1))
        raise EditorFailed("editor calls keep failing")


class EditorFailed(Exception):
    pass


class Grader:
    """math_verify times out with signals, which only work in a main thread (in a worker thread it
    quietly grades everything wrong), so grading runs in a small process pool."""

    def __init__(self):
        self.pool = ProcessPoolExecutor(max_workers=4)

    def __call__(self, answer, text):
        return self.pool.submit(grade, answer, text).result()


def solve(a, srv, ed, grader, r, idx, solution, log, log_lock, replay=None):
    def write(entry):
        with log_lock:
            log.write(json.dumps(entry) + "\n"); log.flush()

    def check(d):  # every response's draft must be this problem's: other workers share the server
        if d is None or d.get("idx") != idx:
            raise RuntimeError(f"server returned another problem's answer for {idx}")
        return d

    d = check(srv("open", idx=idx)["draft"])
    if replay:
        for f in sorted(replay, key=lambda f: f["pos"]):
            d = check(srv("pick", idx=idx, pos=f["pos"], token=f["forced"][0])["draft"])
        if grader(r["answer"], answer_text(d)):
            srv.note(f"✓ {r['id']} replayed from collected.jsonl: {picks_line(d)}")
            return "solved"
        srv.note(f"{r['id']}: replayed picks no longer solve it; steering afresh")
        for f in sorted(replay, key=lambda f: -f["pos"]):
            d = check(srv("unpick", idx=idx, pos=f["pos"])["draft"])
    if grader(r["answer"], answer_text(d)):
        srv.note(f"{r['id']}: already right ({last_boxed(answer_text(d))}), skipped")
        srv("drop", idx=idx)
        return "already"
    text, table = render(d, a.tau, a.branch, a.show)
    first = (f"Problem:\n{r['prompt']}\n\nReference answer: {r['answer']}\n\nReference solution:\n{solution}\n\n"
             f"The model's answer ({describe(d, False)}):\n<answer>\n{text}\n</answer>\n\n"
             f"Branch points (entropy ≥ {a.tau} bits, the {a.branch} highest; candidates numbered for \"choice\"):\n{table}")
    msgs = [{"role": "system", "content": ""}, {"role": "user", "content": first}]
    edits = calls = 0
    outcome = "max edits"
    while edits < a.max_edits and calls < 2 * a.max_edits + 2:
        msgs[0]["content"] = ed.system()
        calls += 1
        try:
            reply, reasoning, info = ed.ask(msgs)
        except EditorFailed as e:
            outcome = str(e)
            break
        msgs.append({"role": "assistant", "content": reply})
        entry = {"id": r["id"], "idx": idx, "call": calls, "reply": reply, "reasoning": reasoning, **info,
                 "sent": msgs[-2]["content"]}
        try:
            act = parse_action(reply)
            kind = act.get("action")
            reason = str(act.get("reason", ""))[:200]
            if kind == "give_up":
                outcome = f"gave up: {reason}"
                entry["action"] = act
                write(entry)
                break
            if kind == "undo":
                pos = int(act["pos"])
                d = check(srv("unpick", idx=idx, pos=pos)["draft"])
                did = f"undid the pick at {pos}"
            elif kind == "edit":
                pos, tok = resolve(d, act)
                old = d["tokens"][pos]
                newtext = next(c["text"] for c in old["top"] if c["token"] == tok)
                edits += 1
                srv.note(f"{r['id']} · edit {edits}: @{pos} {vis(old['text'])!r}→{vis(newtext)!r} "
                         f"(H {bits(old):.2f}b, p {next(c['prob'] for c in old['top'] if c['token'] == tok):.0%}) — {reason}")
                d = check(srv("pick", idx=idx, pos=pos, token=tok)["draft"])
                did = f"applied your edit at {pos} ({vis(old['text'])!r} → {vis(newtext)!r})"
            else:
                raise ValueError(f"unknown action {kind!r}")
            ok = grader(r["answer"], answer_text(d))
            entry.update(action=act, correct=ok, boxed=last_boxed(answer_text(d)))
            write(entry)
            if ok:
                outcome = "solved"
                break
            text, table = render(d, a.tau, a.branch, a.show)
            msgs.append({"role": "user", "content":
                f"I {did}. Picks now in force: {picks_line(d)}.\nThe model's new answer ({describe(d, False)}):\n"
                f"<answer>\n{text}\n</answer>\n\nBranch points:\n{table}\n\nEdits used: {edits}/{a.max_edits}."})
        except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
            entry.update(error=str(e))
            write(entry)
            msgs.append({"role": "user", "content": f"That could not be applied: {e}. Reply again with one valid JSON action."})
    if outcome == "solved":
        srv("commit", idx=idx)
        srv.note(f"✓ {r['id']} solved after {edits} edit(s): {picks_line(d)}")
    else:
        srv("drop", idx=idx)
        srv.note(f"✗ {r['id']} not solved ({outcome}); picks dropped")
    return outcome


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8765")
    ap.add_argument("--prompts", required=True, help="the server's problem list (for answers)")
    ap.add_argument("--prompt-file", default=os.path.join(os.path.dirname(__file__), "../experiments/math/editor_prompt.md"))
    ap.add_argument("--model", default="deepseek/deepseek-v4.1-flash")
    ap.add_argument("--providers", default="Together,BaseTen,Fireworks",
                    help="OpenRouter providers, in order (fastest that cache prompts, measured 11-07)")
    ap.add_argument("--temperature", type=float, default=0.3)
    ap.add_argument("--max-reply", type=int, default=32000, help="reply token cap, reasoning included")
    ap.add_argument("--reasoning-tokens", type=int, default=12000,
                    help="cap on the editor's reasoning, so the reply always has room (uncapped, it ran 100k+ chars and got cut off)")
    ap.add_argument("--max-edits", type=int, default=8, help="edits (forks) per problem")
    ap.add_argument("--apply-every", type=int, default=0, help="apply the gradient every N solved problems (0 = never)")
    ap.add_argument("--workers", type=int, default=1,
                    help="problems steered at once (the editor is remote; the server's one GPU model is the shared limit)")
    ap.add_argument("--tau", type=float, default=0.5, help="branch-point entropy threshold, bits")
    ap.add_argument("--branch", type=int, default=60, help="most branch points to show")
    ap.add_argument("--show", type=int, default=8, help="candidates shown per branch point")
    ap.add_argument("--start", type=int, default=0); ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--replay", default="", help="comma-separated problem ids to do before --start's problems: one "
                    "solved in --collected but never trained (lost in a server restart) gets its picks re-applied; "
                    "any other (e.g. cut off mid-steer) is steered afresh")
    ap.add_argument("--collected", help="collected.jsonl for --replay")
    ap.add_argument("--skip", help="file of problem ids to leave out (e.g. greedy-right under a vLLM pre-screen)")
    ap.add_argument("--log", required=True)
    a = ap.parse_args()
    os.makedirs(a.log, exist_ok=True)
    srv, ed, grader = Server(a.url), Editor(a), Grader()
    rows = [json.loads(l) for l in open(a.prompts)]
    solutions = {r["id"]: r["solution"] for r in load("train")}
    probs = srv("state")["problems"]
    if [p["id"] for p in probs] != [r["id"] for r in rows]:
        sys.exit("--prompts does not match the server's problem list")
    log, log_lock = open(os.path.join(a.log, "calls.jsonl"), "a"), threading.Lock()
    # solved problems from an earlier run still waiting for a gradient count toward --apply-every
    held = sum(1 for p in probs[:a.start] if p.get("picks"))
    tally = {"solved": 0, "already": 0, "failed": 0, "since_apply": held}
    end = len(rows) if not a.limit else min(len(rows), a.start + a.limit)
    # Workers take problems in order. Once --apply-every are solved, no new problem starts; the last
    # in-flight problem to finish applies the gradient (it clears every draft on the server), then
    # work resumes on the new weights.
    by_id = {r["id"]: i for i, r in enumerate(rows)}
    replays = {}
    if a.replay:
        recs = {e["episode"]["prompt"]["id"]: e["episode"]["forks"]
                for e in map(json.loads, open(a.collected))} if a.collected else {}
        replays = {by_id[i]: recs.get(i) for i in a.replay.split(",")}
    skip = {l.strip() for l in open(a.skip)} if a.skip else set()
    queue = list(replays) + [i for i in range(a.start, end) if rows[i]["id"] not in skip]
    st = {"next": 0, "in_flight": 0, "pausing": False, "done": 0}
    cond = threading.Condition()

    def apply():
        res = srv("apply")
        s = res.get("last_stats") or {}
        srv.note(f"applied gradient → round {res['round']}: forced log-prob {s.get('forced_logp_before', 0):.2f} → "
                 f"{s.get('forced_logp_after', 0):.2f} over {s.get('pairs')} picks")

    def worker():
        while True:
            with cond:
                while st["pausing"]:
                    cond.wait()
                if st["next"] >= len(queue) or os.path.exists(os.path.join(a.log, "STOP")):
                    return
                idx = queue[st["next"]]
                st["next"] += 1
                st["in_flight"] += 1
            try:
                out = solve(a, srv, ed, grader, rows[idx], idx, solutions[rows[idx]["id"]], log, log_lock,
                            replays.get(idx))
            except Exception as e:  # noqa: BLE001 (one problem's failure must not stop the others)
                out = f"error: {e}"
                srv.note(f"✗ {rows[idx]['id']} error ({e}); dropping")
                try:
                    srv("drop", idx=idx)
                except Exception:  # noqa: BLE001
                    pass
            with cond:
                st["in_flight"] -= 1
                st["done"] += 1
                key = {"solved": "solved", "already": "already"}.get(out, "failed")
                tally[key] += 1
                tally["since_apply"] += out == "solved"
                if a.apply_every and tally["since_apply"] >= a.apply_every:
                    st["pausing"] = True
                if st["pausing"] and st["in_flight"] == 0:
                    try:
                        apply()
                        tally["since_apply"] = 0
                    finally:
                        st["pausing"] = False
                srv.note(f"editor tally after {st['done']} problems: {tally['solved']} solved, {tally['failed']} not, "
                         f"{tally['already']} already right · editor tokens in/out {ed.cost[0]:,}/{ed.cost[1]:,}")
                cond.notify_all()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(a.workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(json.dumps(tally), file=sys.stderr)


if __name__ == "__main__":
    main()
