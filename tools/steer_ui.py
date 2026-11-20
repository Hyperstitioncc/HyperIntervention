"""A person at a terminal as manual-rl's decider and judge (both roles, one process).

The protocol runs over stdin/stdout (see src/external.rs); the person is reached through
/dev/tty, so run it from an interactive ssh session:

    [decider] kind = "external"  command = ["python", "steer_ui.py", "--tokenizer", MODEL_DIR]
    [judge]   the same command

The model writes its answer on its own (auto mode). At the end you see the solution with
numbered lines, the reference answer and an auto-grade, and you either take it, skip it, or
cut it at a point, type the right text from there, and let the model carry on. Step mode
shows each next token with its alternatives, for corrections finer than retyping.
"""
import argparse, json, os, sys

R, B, D, G, Y, C, M = "\033[0m", "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[36m", "\033[35m"


def tty_setup():
    proto_in = os.fdopen(os.dup(0), "r")
    proto_out = os.fdopen(os.dup(1), "w")
    tty = os.open("/dev/tty", os.O_RDWR)
    os.dup2(tty, 0)
    os.dup2(tty, 1)
    sys.stdin = open(0, "r", closefd=False)
    sys.stdout = open(1, "w", closefd=False)
    try:
        import readline  # noqa: F401 (line editing for input())
    except ImportError:
        pass
    return proto_in, proto_out, sys.stdout


ap = argparse.ArgumentParser()
ap.add_argument("--tokenizer", required=True, help="model dir holding tokenizer.json")
args = ap.parse_args()
from tokenizers import Tokenizer  # noqa: E402

tok = Tokenizer.from_file(os.path.join(args.tokenizer, "tokenizer.json"))
proto, proto_out, out = tty_setup()


def say(*a, end="\n"):
    print(*a, end=end, file=out, flush=True)


def ask(q):
    while True:
        try:
            return input(q)
        except EOFError:
            sys.exit(1)


def reply(obj):
    print(json.dumps(obj), file=proto_out, flush=True)


def grade(answer, text):
    if not answer:
        return None
    try:
        from math_verify import parse, verify
        return bool(verify(parse("$" + answer + "$"), parse(text)))
    except Exception:
        return None


def starts(ids):
    offs, prev = [0], ""
    for i in range(1, len(ids) + 1):
        s = tok.decode(ids[:i], skip_special_tokens=False)
        offs.append(len(s) if s.startswith(prev) else offs[-1])
        prev = s
    return offs


def read_text():
    say(f"{D}Type the text to force. End with a line holding only '.', or ':cancel'.{R}")
    lines = []
    while True:
        line = ask("")
        if line == ".":
            break
        if line == ":cancel":
            return None
        lines.append(line)
    return "\n".join(lines)


mode = "auto"
shown = 0
problem = None


def show_problem(p):
    extra = p.get("extra") or {}
    say(f"\n{B}{'=' * 78}{R}\n{B}{p.get('id')}{R}  {D}{extra.get('level', '')} {extra.get('type', '')}{R}\n")
    say(p.get("prompt", ""))
    say(f"\n{D}reference answer:{R} {Y}{extra.get('answer')}{R}\n{'-' * 78}")


def on_step(r):
    global mode, shown, problem
    if problem != r["prompt"].get("id"):
        problem = r["prompt"].get("id")
        show_problem(r["prompt"])
        shown = 0
    if mode == "auto":
        if r["position"] < shown:
            shown = r["position"]
        say(r["proposal"]["text"], end="")
        shown = r["position"] + 1
        return {"action": "accept"}
    say(f"\n{D}{r['text'][-400:]}{R}")
    say(f"{B}next:{R} {C}{r['proposal']['text']!r}{R} p={r['proposal']['prob']:.2f}")
    for i, t in enumerate(r["top"][:10]):
        say(f"  {i}: {t['text']!r} {D}{t['prob']:.3f}{R}")
    while True:
        k = ask(f"{M}[enter] accept  0-9 pick  t type  a auto  s stop >{R} ").strip()
        if k == "":
            return {"action": "accept"}
        if k.isdigit() and int(k) < len(r["top"][:10]):
            return {"action": "force", "tokens": [r["top"][int(k)]["token"]]}
        if k == "t":
            t = read_text()
            if t:
                return {"action": "force", "text": t}
        if k == "a":
            mode = "auto"
            shown = r["position"]
            return {"action": "accept"}
        if k == "s":
            return {"action": "stop"}


def on_judge(r):
    global mode, shown, problem
    text, ids = r["text"], r["completion"]
    p = r["prompt"]
    answer = (p.get("extra") or {}).get("answer")
    lines = text.split("\n")
    g = grade(answer, text)
    mark = {True: f"{G}CORRECT{R}", False: f"{Y}WRONG{R}", None: "ungraded"}[g]
    say(f"\n{'-' * 78}\n{B}[{p.get('id')}] ended by {r['ended_by']}, {len(ids)} tokens, "
        f"{len(r['forks'])} forks, {r['revisions']} revisions{R}  {mark}  ref {Y}{answer}{R}")
    while True:
        k = ask(f"{M}[d]one  [e]dit  [v]iew  e[x] skip  [q]uit >{R} ").strip()
        if k == "d":
            mode, problem = "auto", None
            return {"verdict": "done"}
        if k == "x":
            mode, problem = "auto", None
            return {"verdict": "abandon"}
        if k == "q":
            sys.exit(0)
        if k == "v":
            for i, line in enumerate(lines):
                say(f"{D}{i:3d}{R} {line}")
            continue
        if k == "e":
            try:
                n = int(ask("line number to cut at: "))
                assert 0 <= n < len(lines)
            except (ValueError, AssertionError):
                say("no such line")
                continue
            say(f"{D}{n:3d}{R} {lines[n]}")
            anchor = ask("cut before this text in the line (empty = start of line): ")
            col = lines[n].find(anchor) if anchor else 0
            if col < 0:
                say("text not found in that line")
                continue
            cut = sum(len(x) + 1 for x in lines[:n]) + col
            offs = starts(ids)
            if offs[-1] != len(text):
                say(f"{Y}warning: token offsets don't line up with the text; the cut may be off by a few chars{R}")
            to = max(i for i in range(len(ids) + 1) if offs[i] <= cut)
            kept = text[offs[to]:cut]
            say(f"{D}keeping:{R} ...{text[max(0, cut - 120):cut]}{C}|{R}")
            t = read_text()
            if t is None:
                continue
            m = ask("then [enter] auto-continue, or [s]tep mode: ").strip()
            mode = "step" if m == "s" else "auto"
            say(f"{D}...{text[max(0, cut - 120):cut]}{R}{C}{t}{R}", end="")
            shown = to + len(tok.encode(kept + t, add_special_tokens=False).ids)
            force = kept + t
            if force:
                return {"verdict": "revise", "to": to, "text": force}
            return {"verdict": "revise", "to": to}


for line in proto:
    req = json.loads(line)
    reply(on_step(req) if req["type"] == "step" else on_judge(req))
