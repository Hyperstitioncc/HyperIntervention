import argparse, contextlib, json, os, re, shutil, subprocess, sys, threading, time, traceback, types
sys.path.insert(0, os.path.dirname(__file__))
from concurrent.futures import ThreadPoolExecutor
from judge_client import SPEND, Judge, JudgeFailed, Grader, Server
from math_eval import load

HERE = os.path.dirname(os.path.abspath(__file__))


def parse_actions(text):
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if not blocks and text.strip().startswith("{") and text.strip().endswith("}"):
        blocks = [text.strip()]
    if not blocks:
        raise ValueError("no action found: put each action in its own ```json block")
    return [json.loads(b) for b in blocks]


def only_blocks(text, on):
    return re.sub(r"(?ms)^<<only (\w+)\n(.*?)^>>\n", lambda m: m.group(2) if m.group(1) in on else "", text)


def hms(secs):
    s = int(secs)
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return f"{h} h {m:02d} min {s:02d} s" if h else f"{m} min {s:02d} s" if m else f"{s} s"


def hm(secs):
    m = int(round(secs / 60))
    return f"{m // 60} h {m % 60} min" if m >= 60 and m % 60 else f"{m // 60} h" if m >= 60 else f"{m} min"


NO_REPLY = "\n\n[No reply was received from you. Continue: reply with your next action, each in its own ```json block.]"
LAST_REPLY = ("\n\n[The session is being ended: this is your last reply. Hand in a version with {\"action\": \"finish\", \"version\": ...}; no other "
              "action is carried out. Without one, the version with the highest validation score is handed in.]")
LAST_REPLY_TIME = ("\n\n[The session's time is up: this is your last reply, made from what you already know. Hand in a version with "
                   "{\"action\": \"finish\", \"version\": ...}: any version you were told of, with a validation score or without one. No other "
                   "action is carried out. Without a finish, the version with the highest validation score is handed in.]")
LAST_REPLY_TOKENS = ("\n\n[Your tokens are used up: this is your last reply, made from what you already know. Hand in a version with "
                     "{\"action\": \"finish\", \"version\": ...}: any version you were told of, with a validation score or without one. No other "
                     "action is carried out. Without a finish, the version with the highest validation score is handed in.]")
CUT_BEFORE = "Not carried out: the session's time ran out before this action began."
CUT_DURING = ("The session's time ran out while this was being carried out. It was cut off: its result is not available, a version it "
              "was making does not exist, and a validation score it was taking does not count.")
now = time.time


class Student:

    def __init__(self, a, gpu=None, n=0):
        self.a, self.proc, self.gpu, self.n = a, None, gpu, n
        self.port = a.port + n
        self.url = f"http://127.0.0.1:{self.port}"
        self.env = a.env if gpu is None else dict(a.env, CUDA_VISIBLE_DEVICES=gpu)

    def start(self, model_dir, round_):
        self.model_dir = model_dir
        cfg = re.sub(r"(?m)^model = .*$", f'model = "{model_dir}"', open(self.a.config).read())
        live = os.path.join(self.a.run, "ui-live.toml" if self.n == 0 else f"ui-live-gpu{self.n}.toml")
        open(live, "w").write(cfg)
        log = open(os.path.join(self.a.run, "server.log" if self.n == 0 else f"server-gpu{self.n}.log"), "a")
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "student_server.py"), "--config", live, "--port", str(self.port),
             "--dtype", self.a.dtype, "--round", str(round_), "--gpu-mem", str(self.a.gpu_mem),
             *(["--max-num-seqs", str(self.a.max_num_seqs)] if getattr(self.a, "max_num_seqs", 0) else []),
             "--max-model-len", str(self.a.max_model_len)], stdout=log, stderr=subprocess.STDOUT, env=self.env)
        srv = Server(self.url)
        for _ in range(360):
            if self.proc.poll() is not None:
                raise RuntimeError(f"steering server died on {model_dir}; see {log.name}")
            try:
                srv("progress")
                return srv
            except Exception:
                time.sleep(5)
        raise RuntimeError("steering server did not come up")

    def kv_tokens(self):
        log = os.path.join(self.a.run, "server.log" if self.n == 0 else f"server-gpu{self.n}.log")
        sizes = re.findall(r"GPU KV cache size: ([\d,]+) tokens", open(log).read())
        if not sizes:
            raise RuntimeError(f"no KV cache size in {log}")
        return int(sizes[-1].replace(",", ""))

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(120)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None

    def down(self):
        return self.proc is None or self.proc.poll() is not None


class Seat:

    def __init__(self, student):
        self.student, self.name, self.srv, self.status, self.busy = student, None, None, {}, False

    def clear(self):
        self.student.stop()
        self.name, self.srv, self.status = None, None, {}


class Gate:

    def __init__(self):
        self.c, self.n, self.closed = threading.Condition(), 0, False

    @contextlib.contextmanager
    def shared(self):
        with self.c:
            while self.closed:
                self.c.wait()
            self.n += 1
        try:
            yield
        finally:
            with self.c:
                self.n -= 1
                self.c.notify_all()

    def close(self):
        with self.c:
            while self.closed:
                self.c.wait()
            self.closed = True
            while self.n:
                self.c.wait()

    def open(self):
        with self.c:
            self.closed = False
            self.c.notify_all()

    @contextlib.contextmanager
    def alone(self):
        self.close()
        try:
            yield
        finally:
            self.open()


class Session:
    def __init__(self, a):
        self.a = a
        os.makedirs(os.path.join(a.run, "versions"), exist_ok=True)
        self.vroot = os.path.join(a.local_dir, "versions") if getattr(a, "local_dir", "") else os.path.join(a.run, "versions")
        os.makedirs(self.vroot, exist_ok=True)
        self.archiving = []
        cfg = open(a.config).read()
        self.base = re.search(r'(?m)^model = "(.*)"$', cfg).group(1)
        self.rows = [json.loads(l) for l in open(re.search(r'(?m)^prompts = "(.*)"$', cfg).group(1))]
        self.prompts = re.search(r'(?m)^prompts = "(.*)"$', cfg).group(1)
        self.val_rows = [json.loads(l) for l in open(a.val)]
        self.counted = [r["id"] for r in self.val_rows]
        self.grader = Grader()
        self.seats = [Seat(Student(a, g, n)) for n, g in enumerate(getattr(a, "gpus", None) or [None])]
        self.flows = max(1, int(getattr(a, "workflows", 1) or 1))
        if self.flows > 1:
            if len(self.seats) < self.flows:
                raise SystemExit(f"--workflows {self.flows} needs {self.flows} GPUs and the job has {len(self.seats)}")
            self.seats = self.seats[:self.flows]
        self.solo = False
        if getattr(a, "spread", False) and self.flows == 1 and len(self.seats) > 1:
            self.flows, self.solo = len(self.seats), True
        self.tl = threading.local()
        self.gate, self.mu, self.over, self.fl, self.owner = Gate(), threading.Lock(), False, [], {}
        self.gone = set()
        self._seat = self.seats[0]
        a.wall_min = getattr(a, "wall_min", 0)
        stamp = os.path.join(a.run, "started")
        if not os.path.exists(stamp):
            open(stamp, "w").write(str(time.time()))
        self.deadline = float(open(stamp).read()) + 60 * a.wall_min if a.wall_min else None
        self.lock = threading.Lock()
        self.log = open(os.path.join(a.run, "agent.jsonl"), "a")
        self.prompt_file = os.path.join(HERE, "../experiments", getattr(a, "task", "math"), a.planner_prompt)
        self.scripted = a.model.startswith("scripted:")
        self.tok_cap = 0 if self.scripted else int(getattr(a, "token_budget", 0) or 0)
        self.time_cap = 0.0 if self.scripted else 60.0 * float(getattr(a, "time_budget_min", 0) or 0)
        self.t0 = None
        self.planner = None if self.scripted else Judge(types.SimpleNamespace(**vars(a), prompt_file=self.prompt_file))
        self.versions = {"v0": {"dir": self.base, "parent": None, "included": [], "settings": None, "episodes": 0}}
        keep = lambda k: k.startswith("MATH_") or k in ("SLURM_JOB_ID", "SLURM_JOB_NODELIST", "SLURM_JOB_PARTITION", "CUDA_VISIBLE_DEVICES", "EDITOR_COST_CAP", "REPO")
        plain = {k: v for k, v in vars(a).items() if k != "env" and isinstance(v, (str, int, float, bool, list, type(None)))}
        with open(os.path.join(a.run, "config.jsonl"), "a") as f:
            f.write(json.dumps({"at": round(time.time(), 1), "args": plain, "settings": {k: v for k, v in os.environ.items() if keep(k)},
                                "pool": len(self.rows), "validation": len(self.val_rows), "gpus": len(self.seats), "one_conversation": self.solo,
                                "conversations": 1 if self.solo else self.flows}) + "\n")
        self.live, self.status, self.left = "v0", {}, {}
        self.vnext = 1
        self.flight = []
        self.pinned = set()

    seat = property(lambda self: getattr(self.tl, "seat", None) or self._seat, lambda self, v: setattr(self, "_seat", v))
    w = property(lambda self: getattr(self.tl, "w", 0))

    def owner_of(self, i):
        w = self.owner.get(i, i % self.flows)
        return 0 if w in self.gone else w

    def mine(self, i):
        return self.flows == 1 or self.solo or self.owner_of(i) == self.w

    def prompt_on(self):
        return {"time"} if self.time_cap else {"units"}

    @property
    def capped(self):
        return bool(self.tok_cap or self.time_cap)

    def clock_begin(self):
        p = os.path.join(self.a.run, "clock_start")
        if not os.path.exists(p):
            with open(p + ".tmp", "w") as f:
                f.write(repr(now()))
            os.replace(p + ".tmp", p)
        self.t0 = float(open(p).read())

    def elapsed(self):
        return now() - self.t0 if self.t0 is not None else 0.0

    def time_over(self):
        return bool(self.time_cap) and self.elapsed() >= self.time_cap

    def used_up(self):
        return bool(self.tok_cap and SPEND["tok"] >= self.tok_cap) or self.time_over()

    def time_line(self):
        used = self.elapsed()
        return f"TIME: {hms(used)} used of {hm(self.time_cap)}; {hms(max(0.0, self.time_cap - used))} left."

    def stamp(self, msgs):
        if self.time_cap:
            m = msgs[-1]
            head, sep, tail = m["content"].rpartition("TIME: ")
            if sep and " left." in tail:
                m["content"] = head + self.time_line() + tail[tail.index(" left.") + len(" left."):]

    def snapshot(self):
        return {n: (v.get("marks"), v.get("val")) for n, v in self.versions.items()}

    def cut_off(self, snap):
        for n, v in self.versions.items():
            if n not in snap:
                v["late"] = True
            elif (v.get("marks"), v.get("val")) != snap[n]:
                for k, x in zip(("marks", "val"), snap[n]):
                    if x is None:
                        v.pop(k, None)
                    else:
                        v[k] = x
                self.val_rewrite(n)

    def val_rewrite(self, name):
        pass

    def late(self):
        return self.deadline is not None and time.time() > self.deadline

    live = property(lambda self: self.seat.name, lambda self, v: setattr(self.seat, "name", v))
    status = property(lambda self: self.seat.status, lambda self, v: setattr(self.seat, "status", v))
    srv = property(lambda self: self.seat.srv, lambda self, v: setattr(self.seat, "srv", v))
    student = property(lambda self: self.seat.student)

    def points(self, val):
        return sum(val[i] for i in self.counted)

    def spare(self, m=1):
        if len(self.seats) == 1 or self.flows > 1:
            return [self.seat][:m]
        best = self.best()
        free = [s for s in self.seats if s is not self.seat and not s.busy]
        return sorted(free, key=lambda s: (s.name is not None, s.name != "v0", s.name == best, int(s.name[1:]) if s.name else 0))[:m]

    def load(self, seat, name, model_dir=None):
        seat.clear()
        seat.name = name
        seat.srv = seat.student.start(model_dir or self.versions[name]["dir"], int(name[1:]))
        self.screen(seat)

    def catch_up(self, seat):
        pass

    def go_live(self, name):
        seat = next((s for s in self.seats if s.name == name and not s.busy), None)
        if seat is None:
            seat = (self.spare(1) or [self.seat])[0]
        if seat.name != name or seat.srv is None or seat.student.down():
            self.load(seat, name)
        self.seat = seat
        self.catch_up(seat)
        right = sum(bool(s["correct"]) for s in seat.status.values())
        return f"{name} is the live student. Pool under it: {right} right / {len(self.rows) - right} wrong."

    def sync(self):
        name, errs = self.live, []

        def one(f):
            try:
                seat = f["seat"]
                if seat.name != name or seat.srv is None or seat.student.down():
                    self.load(seat, name)
                self.catch_up(seat)
            except Exception as e:
                errs.append(e)
                f["state"] = "stopped"
                print(f"HARNESS FAILURE: workflow {f['w']} could not load {name}: {e!r}", file=sys.stderr, flush=True)
        ts = [threading.Thread(target=one, args=(f,), daemon=True) for f in self.fl[1:] if f["state"] != "stopped"]
        for t in ts:
            t.start()
        for t in ts:
            t.join()

    def stop_all(self):
        for s in self.seats:
            s.student.stop()

    def kept(self):
        p = os.path.join(self.a.run, "collected.jsonl")
        out = []
        for n, line in enumerate(open(p) if os.path.exists(p) else []):
            r = json.loads(line)
            out.append({"key": f"c{n}", "round": r.get("round", 0), "id": r["episode"]["prompt"]["id"], "line": line,
                        "forced": len(r["episode"]["forks"]) + sum(m["w"] > 0 for m in r["episode"].get("marks", [])),
                        "down": len(r["episode"].get("down", []))})
        return out

    def do_list(self, act):
        want, subj, off = act.get("status", "wrong"), act.get("subject"), int(act.get("offset", 0))
        made = {}
        for k in self.kept():
            made.setdefault(k["id"], []).append(f"kept@v{k['round']}")
        rows = []
        for i, r in enumerate(self.rows):
            if not self.mine(i):
                continue
            s = self.status[i]
            if s["correct"] is None:
                continue
            if (want == "all" or (want == "right") == s["correct"]) and (not subj or subj in r["id"]):
                rows.append(f"{i} | {r['id'].split('/')[1]} | {r.get('level', '')} | {'right' if s['correct'] else 'wrong' if s['boxed'] else 'wrong, no boxed answer'} | "
                            f"{s['tokens']} tok{' (cut off)' if s['ended'] != 'eos' else ''} | {' '.join(made.get(r['id'], [])) or '-'}")
        page = rows[off:off + 120]
        by = {}
        for i, r in enumerate(self.rows):
            if not self.mine(i) or self.status[i]["correct"] is None:
                continue
            b = by.setdefault(r["id"].split("/")[1], [0, 0])
            b[0 if self.status[i]["correct"] else 1] += 1
        head = "; ".join(f"{k} {v[0]} right/{v[1]} wrong" for k, v in sorted(by.items()))
        return (f"Pool under {self.live}, by subject: {head}\n{len(rows)} problems match (showing {off}-{off + len(page)}):\n"
                "idx | subject | level | status | answer length | corrections kept so far\n" + "\n".join(page))

    def prune(self, keep=()):
        if getattr(self.a, "keep_versions", False):
            return
        best = self.best()
        safe = {"v0", self.live, best, *keep, *self.pinned, *(s.name for s in self.seats if s.name)}
        for n, v in self.versions.items():
            if n not in safe and not v.get("pruned"):
                shutil.rmtree(v["dir"], ignore_errors=True)
                v["pruned"] = True

    def do_use(self, act):
        name = act.get("version")
        if name not in self.versions:
            raise ValueError(f"version must be one of {sorted(self.versions)}")
        if self.versions[name].get("pruned"):
            raise ValueError(f"{name}'s weights were discarded (only the live version, the best-scoring one and v0 are kept)")
        out = self.go_live(name)
        self.prune()
        return out

    def run(self):
        a = self.a
        self.validate("v0")
        state_p = os.path.join(a.run, "state.json")
        st = json.load(open(state_p)) if os.path.exists(state_p) else None
        if st:
            for n, v in st["versions"].items():
                self.versions[n] = {**v, "val": self.score_dir(v["dir"], os.path.join(a.run, "versions", f"{n}-val.jsonl"))}
            self.versions["v0"]["dir"] = self.base
            self.live, self.left = st["live"], st["left"]
            self.vnext = st.get("vnext", len(self.versions))
            for n, v in self.versions.items():
                if n != "v0" and not os.path.isdir(v["dir"]):
                    if v.get("archive") and os.path.isdir(v["archive"]):
                        v["dir"] = v["archive"]
                    else:
                        v["pruned"] = True
            if self.versions[self.live].get("pruned"):
                there = [n for n, v in self.versions.items() if not v.get("pruned") and v.get("val")]
                self.live = max(there, key=lambda n: (self.points(self.versions[n]["val"]), -int(n[1:])))
                print(f"the live version's weights are gone; carrying on from {self.live}", file=sys.stderr, flush=True)
            SPEND["usd"] += st.get("usd", 0.0)
            SPEND["tok"] += st.get("tok", 0)
            self.restore_state(st)
            for d in os.listdir(self.vroot):
                if d.endswith("-train") or d.endswith(".tmp"):
                    shutil.rmtree(os.path.join(self.vroot, d), ignore_errors=True)
            for d in os.listdir(os.path.join(a.run, "versions")):
                if d.endswith(".tmp"):
                    shutil.rmtree(os.path.join(a.run, "versions", d), ignore_errors=True)
                    continue
                if d.endswith("-train") or (re.fullmatch(r"v\d+", d) and d not in self.versions):
                    shutil.rmtree(os.path.join(a.run, "versions", d), ignore_errors=True)
            if self.scripted:
                by_id = {r["id"]: n for n, r in enumerate(self.rows)}
                self.planner.done, self.planner.trained = {by_id[k["id"]] for k in self.kept()}, len(self.versions) > 1
            print(f"resuming after action {st['turn']} on {self.live}", file=sys.stderr, flush=True)
        if self.flows > 1 and not self.solo:
            return self.run_flows(st, state_p)
        first = self.go_live(self.live)
        prompt = only_blocks(open(self.prompt_file).read(), self.prompt_on())
        for k, v in {"pool": len(self.rows), "val": len(self.val_rows), "turns": a.turns, **self.prompt_vars()}.items():
            prompt = prompt.replace("{" + k + "}", str(v))
        if self.time_cap:
            self.clock_begin()
        msgs = [{"role": "system", "content": prompt},
                {"role": "user", "content": f"The untrained student (v0) scores {self.points(self.versions['v0']['val']):.1f} of {len(self.counted)} on validation. "
                                            f"{first}\n{self.budget()} Begin."}]
        handed, turn = None, 0
        if st:
            msgs, turn, handed = st["msgs"], st["turn"], st.get("handed")
            if hasattr(self.planner, "sent"):
                self.planner.sent[id(msgs)] = len(msgs) - 1
            if not msgs[-1]["content"].endswith("Open a problem again to go on with it.]"):
                msgs[-1]["content"] += "\n\n[The job was restarted: forks and weights on problems you had not kept are gone. Open a problem again to go on with it.]"
            if st.get("flight"):
                msgs[-1]["content"] += (f"\n\n[The job was restarted. {', '.join(st['flight'])}, which were being made in the background, were "
                                        "lost with it; nothing was charged for them. Train them again if you want them.]")

        def save():
            vs = {n: {k: x for k, x in v.items() if k != "val"} for n, v in self.versions.items()}
            json.dump({"versions": vs, "live": self.live, "left": self.left, "vnext": self.vnext, "flight": self.flight,
                       "usd": SPEND["usd"], "tok": SPEND["tok"], "clock": round(self.elapsed(), 1), "msgs": msgs, "turn": turn, "handed": handed,
                       **self.saved_state()},
                      open(state_p + ".tmp", "w"))
            os.replace(state_p + ".tmp", state_p)
        acts = self.actions()
        last = False
        silent = 0
        while turn < a.turns and handed is None:
            if os.path.exists(os.path.join(a.run, "FINISH")) or self.late():
                print("FINISH file found or time up: wrapping up with the best validated version", file=sys.stderr, flush=True)
                break
            if self.used_up():
                if last:
                    break
                last = True
                msgs[-1]["content"] += LAST_REPLY_TIME if self.time_over() else LAST_REPLY_TOKENS if self.time_cap else LAST_REPLY
            turn += 1
            self.stamp(msgs)
            try:
                reply, reasoning, info = self.planner.ask(msgs)
                silent = 0
            except JudgeFailed as e:
                print(f"planner failed: {e}", file=sys.stderr, flush=True)
                silent += 1
                if "cost cap" in str(e) or silent > 5 or last:
                    break
                turn -= 1
                if not msgs[-1]["content"].endswith(NO_REPLY):
                    msgs[-1]["content"] += NO_REPLY
                time.sleep(60)
                continue
            msgs.append({"role": "assistant", "content": reply})
            entry = {"turn": turn, "at": round(time.time(), 1), "reply": reply, "reasoning": reasoning, **info, "actions": [], "results": [],
                     "took": [], "own_tokens": SPEND["tok"], "usd": round(SPEND["usd"], 4)}
            results = []
            try:
                todo = parse_actions(reply)
            except (ValueError, json.JSONDecodeError) as e:
                todo, results = [], [f"That could not be read: {e}. Reply with your actions, each in its own ```json block."
                                     + (" The session goes on: continue with your next action." if self.capped and not last else "")]
            self.took, self.failed, done = {}, [], {}
            cut = set()
            timed = bool(self.time_cap) and not last
            for n, act in enumerate(todo):
                if n:
                    turn += 1
                entry["actions"].append(act)
                t_act = time.time()
                tag = f"[{json.dumps(act)[:120]}]\n" if len(todo) > 1 else ""
                if timed and n not in done and not (isinstance(act, dict) and act.get("action") == "finish") and self.time_over():
                    results.append(tag + CUT_BEFORE)
                    entry["took"].append(0.0)
                    continue
                if n not in done and self.failed:
                    results.append(f"[{json.dumps(act)[:120]}]\nNot run: action {min(self.failed) + 1} of this reply could not be done, "
                                   "and the actions after it were planned on it.")
                    entry["took"].append(0.0)
                    continue
                snap = self.snapshot() if timed else None
                if n not in done and not last:
                    got = self.together(todo, acts, n)
                    if got and timed and self.time_over():
                        self.cut_off(snap)
                        entry.setdefault("withheld", []).extend(f"{m}: {r}" for m, r in sorted(got.items()))
                        got = {m: CUT_DURING for m in got}
                        cut.update(got)
                    done.update(got)
                try:
                    kind = act.get("action")
                    if n in done:
                        result = done[n]
                    elif kind == "finish":
                        if act.get("version") not in self.versions or self.versions[act["version"]].get("pruned") or self.versions[act["version"]].get("late"):
                            raise ValueError("version must be one whose weights are kept: "
                                             + str(sorted(n for n, v in self.versions.items() if not v.get("pruned") and not v.get("late"))))
                        why = self.finish_check(act["version"])
                        if why:
                            raise ValueError(why)
                        handed, result = act["version"], f"Handed in {act['version']}."
                    elif last:
                        raise ValueError("the session is being ended; only finish is carried out")
                    elif kind in acts:
                        result = acts[kind](act)
                    else:
                        raise ValueError(f"unknown action {kind!r}")
                except (ValueError, KeyError, TypeError) as e:
                    result = f"That could not be done: {e}."
                    self.failed.append(n)
                except (subprocess.CalledProcessError, RuntimeError, OSError) as e:
                    result = f"That failed inside the harness ({type(e).__name__}: {str(e)[:300]})."
                    self.failed.append(n)
                    print(f"HARNESS FAILURE on {json.dumps(act)[:200]}: {e}", file=sys.stderr, flush=True)
                    try:
                        if self.student.down():
                            result += " " + self.go_live(self.live)
                    except Exception as e2:
                        print(f"could not restart the live student: {e2}", file=sys.stderr, flush=True)
                        raise
                result = result.strip("\n")
                secs = self.took.get(n, time.time() - t_act)
                if timed and n not in done and kind != "finish" and self.time_over():
                    self.cut_off(snap)
                    entry.setdefault("withheld", []).append(f"{n}: {result}")
                    result = CUT_DURING
                    cut.add(n)
                if secs >= 10 and n not in cut:
                    result += f"\n[this took {int(secs // 60)} min {int(secs % 60)} s]" if secs >= 60 else f"\n[this took {int(secs)} s]"
                results.append(tag + result)
                entry["took"].append(round(secs, 2))
                print(f"--- turn {turn}: {json.dumps(act)[:300]}\n{result[:1500]}", file=sys.stderr, flush=True)
                if handed is not None or turn >= a.turns:
                    break
            entry["results"], entry["clock"] = results, round(self.elapsed(), 1)
            self.log.write(json.dumps(entry) + "\n"); self.log.flush()
            msgs.append({"role": "user", "content": "\n\n".join(results) + f"\n\n{self.budget()}" + ("" if self.capped else f" Actions left: {a.turns - turn}.")})
            self.compact(msgs)
            save()
        self.settle()
        self.stop_all()
        if handed is None:
            handed = self.best()
            print(f"no finish action: handing in {handed}", file=sys.stderr, flush=True)
        self.finish(handed, turn)

    def archive(self, name):
        if self.vroot == os.path.join(self.a.run, "versions") or not getattr(self.a, "keep_versions", False) or getattr(self.a, "no_archive", False):
            return
        src, dst = self.versions[name]["dir"], os.path.join(self.a.run, "versions", name)

        def copy():
            try:
                shutil.rmtree(dst + ".tmp", ignore_errors=True)
                shutil.copytree(src, dst + ".tmp")
                os.replace(dst + ".tmp", dst)
                self.versions[name]["archive"] = dst
            except Exception as e:
                print(f"HARNESS FAILURE: {name} could not be copied to the run folder: {e!r}", file=sys.stderr, flush=True)
        t = threading.Thread(target=copy, daemon=True)
        t.start()
        self.archiving.append(t)

    def saved_state(self):
        return {}

    def restore_state(self, st):
        pass

    PARALLEL = ()

    def seat_of(self, i):
        return self._seat

    def together(self, todo, acts, start=0):
        done, k = {}, start
        while k == start:
            j = k
            while j < len(todo) and isinstance(todo[j], dict) and todo[j].get("action") in self.PARALLEL and str(todo[j].get("idx", "")).lstrip("-").isdigit():
                j += 1
            groups = {}
            for n in range(k, j):
                groups.setdefault(int(todo[n]["idx"]), []).append(n)
            if len(groups) > 1:
                broke = []

                def one(item):
                    i, ns = item
                    self.tl.seat = self.seat_of(i)
                    try:
                        bad = False
                        for n in ns:
                            t0 = time.time()
                            if bad:
                                done[n] = f"Not run: an earlier action of this reply on problem {i} could not be done, and this one was planned on it."
                                continue
                            try:
                                done[n] = acts[todo[n]["action"]](todo[n])
                            except (ValueError, KeyError, TypeError) as e:
                                done[n], bad = f"That could not be done: {e}.", True
                                self.failed.append(n)
                            except (subprocess.CalledProcessError, RuntimeError, OSError) as e:
                                done[n], bad = f"That failed inside the harness ({type(e).__name__}: {str(e)[:300]}).", True
                                self.failed.append(n)
                                broke.append(n)
                                print(f"HARNESS FAILURE on {json.dumps(todo[n])[:200]}: {e}", file=sys.stderr, flush=True)
                            self.took[n] = time.time() - t0
                    finally:
                        del self.tl.seat
                with ThreadPoolExecutor(min(32, len(groups))) as ex:
                    list(ex.map(one, groups.items()))
                if broke and any(s.name and s.student.down() for s in self.seats):
                    done[max(broke)] += " " + self.go_live(self.live)
            k = -1
        return done

    def settle(self):
        pass

    LEAD_ONLY = ("train", "use", "validate", "finish")

    def post(self, text, to=None):
        me = self.w
        for f in self.fl:
            if f["w"] != me and (to is None or f["w"] == to) and f["state"] != "stopped":
                with self.mu:
                    f["inbox"].append(text)

    def flow_note(self, w):
        return ""

    def flow_first(self, w, first):
        return first

    def flow_wait(self, f):
        f["state"] = "wait"
        t0 = time.time()
        try:
            while not self.over and not self.late() and time.time() - t0 < 1800:
                if f["inbox"]:
                    return "News from the other workflows:"
                others = [g for g in self.fl if g is not f]
                if f["w"] == 0 and all(g["state"] != "run" for g in others):
                    return "Every other workflow is waiting for you to train or has stopped."
                if f["w"] != 0 and self.fl[0]["state"] == "stopped":
                    return "The lead has stopped."
                time.sleep(0.2)
            return "Nothing new."
        finally:
            f["state"] = "run"

    def flow_stop_check(self):
        return None

    def run_flows(self, st, state_p):
        a = self.a
        if self.time_cap:
            raise SystemExit("--time-budget-min is for a session of one conversation (--spread), not for --workflows")
        if self.scripted and not hasattr(self.planner, "for_flow"):
            raise SystemExit("--workflows needs a model editor (or scripted:replay)")
        first = self.go_live(self.live)
        self.owner = {int(k): v for k, v in (st or {}).get("owner", {}).items()}
        base = only_blocks(open(self.prompt_file).read(), self.prompt_on())
        saved = (st or {}).get("flows")
        if st and not saved:
            raise SystemExit("this run was started without --workflows; it cannot be resumed with them")
        self.fl = [{"w": w, "seat": self.seats[w], "msgs": None, "turn": 0, "inbox": [], "state": "run", "why": "",
                    "planner": self.planner.for_flow(w) if self.scripted else self.planner} for w in range(self.flows)]
        if saved:
            for f, sv in zip(self.fl, saved):
                f.update(msgs=sv["msgs"], turn=sv["turn"], state="stopped" if sv["state"] == "stopped" else "run", why=sv.get("why", ""), inbox=sv.get("inbox", []))
            self.gone = {f["w"] for f in self.fl[1:] if f["state"] == "stopped"}
        self.sync()
        for f in self.fl:
            if f["msgs"] is None:
                self.tl.w, self.tl.seat = f["w"], f["seat"]
                prompt = base.replace("{flows_note}", self.flow_note(f["w"]))
                for k, v in {"pool": len(self.rows), "val": len(self.val_rows), "turns": a.turns, **self.prompt_vars()}.items():
                    prompt = prompt.replace("{" + k + "}", str(v))
                f["msgs"] = [{"role": "system", "content": prompt},
                             {"role": "user", "content": f"The untrained student (v0) scores {self.points(self.versions['v0']['val']):.1f} of {len(self.counted)} on validation. "
                                                         f"{self.flow_first(f['w'], first)}\n{self.budget()} Begin."}]
            f["snap"] = {"msgs": [dict(m) for m in f["msgs"]], "turn": f["turn"], "state": f["state"], "why": f["why"], "inbox": list(f["inbox"])}
        del self.tl.w, self.tl.seat
        if saved and st.get("flight"):
            self.fl[0]["inbox"].append(f"[The job was restarted. {', '.join(st['flight'])}, which were being made, were lost with it; nothing was charged. Train again if you want them.]")
        if saved:
            for f in self.fl:
                if f["state"] != "stopped":
                    f["inbox"].append("[The job was restarted: forks and weights that were not kept are gone. Open a problem again to go on with it.]")
        self.handed = (st or {}).get("handed")
        acts = self.actions()

        def save(f):
            with self.mu:
                f["snap"] = {"msgs": [dict(m) for m in f["msgs"]], "turn": f["turn"], "state": f["state"], "why": f["why"], "inbox": list(f["inbox"])}
                vs = {n: {k: x for k, x in v.items() if k != "val"} for n, v in self.versions.items()}
                json.dump({"versions": vs, "live": self._seat.name, "left": self.left, "vnext": self.vnext, "flight": self.flight, "usd": SPEND["usd"],
                           "owner": self.owner, "flows": [g["snap"] for g in self.fl], "handed": self.handed, "tok": SPEND["tok"],
                           "msgs": self.fl[0]["snap"]["msgs"], "turn": self.fl[0]["snap"]["turn"]}, open(state_p + ".tmp", "w"))
                os.replace(state_p + ".tmp", state_p)

        def stop(f, why):
            f["state"], f["why"] = "stopped", why
            print(f"workflow {f['w']} stopped: {why}", file=sys.stderr, flush=True)
            if f["w"]:
                self.gone.add(f["w"])
                self.post(f"[Conversation {f['w']} has stopped: {why}. Its pool problems are yours to work on now.]", to=0)

        def loop(f):
            lead = f["w"] == 0
            if not lead:
                self.tl.w, self.tl.seat = f["w"], f["seat"]
            msgs, last = f["msgs"], False
            while f["turn"] < a.turns and f["state"] != "stopped" and not self.over:
                if lead and (os.path.exists(os.path.join(a.run, "FINISH")) or self.late()):
                    print("FINISH file found or time up: wrapping up with the best validated version", file=sys.stderr, flush=True)
                    return
                if self.tok_cap and SPEND["tok"] >= self.tok_cap:
                    if last or not lead:
                        return None if lead else stop(f, "the editor's tokens are used up")
                    last = True
                    msgs[-1]["content"] += LAST_REPLY
                f["turn"] += 1
                try:
                    reply, reasoning, info = f["planner"].ask(msgs)
                except JudgeFailed as e:
                    print(f"workflow {f['w']}: planner failed: {e}", file=sys.stderr, flush=True)
                    return stop(f, "its editor gave no reply")
                if self.over:
                    return
                msgs.append({"role": "assistant", "content": reply})
                entry = {"flow": f["w"], "turn": f["turn"], "reply": reply, "reasoning": reasoning, **info, "actions": [], "results": []}
                results = []
                try:
                    todo = parse_actions(reply)
                except (ValueError, json.JSONDecodeError) as e:
                    todo, results = [], [f"That could not be read: {e}. Reply with your actions, each in its own ```json block."]
                for n, act in enumerate(todo):
                    if n:
                        f["turn"] += 1
                    entry["actions"].append(act)
                    kind = act.get("action") if isinstance(act, dict) else None
                    try:
                        if last and kind != "finish":
                            raise ValueError("the session is being ended; only finish is carried out")
                        if kind == "wait" and not (lead and self.flight):
                            result = self.flow_wait(f)
                        elif not lead and kind in ("stop", "finish"):
                            why = self.flow_stop_check()
                            if why:
                                raise ValueError(why)
                            stop(f, "it said its work is done")
                            result = "This conversation has stopped."
                        elif not lead and (kind in self.LEAD_ONLY or (kind == "eval" and not act.get("problems"))):
                            raise ValueError(f'only the lead (conversation 0) can "{kind}"' + (" the validation set" if kind == "eval" else "")
                                             + '; you edit and keep corrections, and {"action": "wait"} waits for its next training run')
                        elif lead and kind in ("train", "use", "finish"):
                            with self.gate.alone():
                                if kind == "finish":
                                    if act.get("version") not in self.versions or self.versions[act["version"]].get("pruned"):
                                        raise ValueError("version must be one whose weights are kept: " + str(sorted(n for n, v in self.versions.items() if not v.get("pruned"))))
                                    why = self.finish_check(act["version"])
                                    if why:
                                        raise ValueError(why)
                                    self.handed, result = act["version"], f"Handed in {act['version']}."
                                else:
                                    was = self.live
                                    try:
                                        result = acts[kind](act)
                                    finally:
                                        if not (kind == "train" and self.flight):
                                            self.sync()
                                    if self.live != was:
                                        self.post(f"[The lead ran {json.dumps(act)[:200]}]\n{result}\n{self.live} is now the live student on your GPU too. "
                                                  "Forks and weights you had not kept were dropped with the old version.")
                        elif kind in acts:
                            with self.gate.shared():
                                result = acts[kind](act)
                        else:
                            raise ValueError(f"unknown action {kind!r}")
                    except (ValueError, KeyError, TypeError, AttributeError) as e:
                        result = f"That could not be done: {e}."
                    except (subprocess.CalledProcessError, RuntimeError, OSError) as e:
                        result = f"That failed inside the harness ({type(e).__name__}: {str(e)[:300]})."
                        print(f"HARNESS FAILURE (workflow {f['w']}) on {json.dumps(act)[:200]}: {e}", file=sys.stderr, flush=True)
                        if self.over:
                            return
                        try:
                            if f["seat"].student.down():
                                with self.gate.alone():
                                    if lead:
                                        result += " " + self.go_live(self.live)
                                        self.sync()
                                    else:
                                        self.load(f["seat"], self.live)
                                        self.catch_up(f["seat"])
                                        result += f" The student on your GPU was started again ({self.live}); forks and weights not kept are gone."
                        except Exception as e2:
                            print(f"could not restart the student of workflow {f['w']}: {e2}", file=sys.stderr, flush=True)
                            if lead:
                                raise
                            return stop(f, "its student could not be started again")
                    results.append((f"[{json.dumps(act)[:120]}]\n" if len(todo) > 1 else "") + result)
                    print(f"--- workflow {f['w']} turn {f['turn']}: {json.dumps(act)[:300]}\n{result[:1500]}", file=sys.stderr, flush=True)
                    if self.handed is not None or f["turn"] >= a.turns or f["state"] == "stopped":
                        break
                with self.mu:
                    news, f["inbox"] = f["inbox"], []
                entry["results"], entry["news"] = results, news
                with self.mu:
                    self.log.write(json.dumps(entry) + "\n"); self.log.flush()
                msgs.append({"role": "user", "content": "\n\n".join(results + news) + f"\n\n{self.budget()}" + ("" if self.tok_cap else f" Actions left: {a.turns - f['turn']}.")})
                self.compact(msgs)
                save(f)
                if lead and self.handed is not None:
                    return
            if f["state"] != "stopped":
                stop(f, "it is out of actions" if f["turn"] >= a.turns else "the session ended")
                save(f)

        def helper(f):
            try:
                loop(f)
            except Exception as e:
                traceback.print_exc()
                stop(f, f"it failed inside the harness ({type(e).__name__})")

        for f in self.fl[1:]:
            if f["state"] != "stopped":
                threading.Thread(target=helper, args=(f,), daemon=True).start()
        try:
            if self.fl[0]["state"] != "stopped" and self.handed is None:
                loop(self.fl[0])
        finally:
            self.over = True
            self.fl[0]["state"] = "stopped"
        self.gate.close()
        self.settle()
        self.stop_all()
        handed = self.handed
        if handed is None:
            handed = self.best()
            print(f"no finish action: handing in {handed}", file=sys.stderr, flush=True)
        self.finish(handed, sum(f["turn"] for f in self.fl))

    def finish(self, handed, turns):
        a = self.a
        ks = self.kept()
        result = {"editor": a.model, "student": self.base, "handed_in": handed, "turns": turns, "precision": a.dtype,
                  "editor_tokens": {"in": self.planner.cost[0], "out": self.planner.cost[1]},
                  "editor_usd": round(SPEND["usd"], 2),
                  "kept": {"corrections": len(ks), "trained_tokens": sum(k["forced"] for k in ks)},
                  "versions": {n: {"parent": v["parent"], "settings": v["settings"], "episodes": v["episodes"],
                                   "validation_correct": self.points(v["val"]), "validation_n": len(self.counted),
                                   "validation_all": sum(v["val"].values()), "validation_all_n": len(v["val"]),
                                   "stats": v.get("stats")} for n, v in self.versions.items()}}
        if handed != "v0":
            allf = os.path.join(a.run, "kept-all.jsonl")
            open(allf, "w").writelines(k["line"] for k in ks)
            tag = f"{os.path.basename(os.path.normpath(a.run))}-{handed}"
            env = dict(a.env, MATH_EVAL_DTYPE=a.dtype)
            if os.environ.get("FINAL_GREEDY", "1") != "0":
                subprocess.run([sys.executable, os.path.join(HERE, "eval_watch.py"), "one", "--ckpt", self.versions[handed]["dir"],
                                "--tag", tag, "--episodes", allf, "--base", self.base, "--near", self.prompts, "--out", a.eval_out,
                                "--gpu-mem", str(a.gpu_mem)], check=True, env=env)
                rep = json.load(open(os.path.join(a.eval_out, f"{tag}.json")))
                arms = [x for x in ("trained", "control") if x in rep["arms"]]
                for key in ("test", "test_by_subject", "test_base_boxed", "test_base_unboxed"):
                    result[key] = {arm: rep["arms"][arm][key] for arm in arms}
                t = result["test"]["trained"]
                result["score"] = {arm: result["test"][arm]["acc"] - result["test"][arm]["base"] for arm in arms}
                result["score"]["of"] = t["n"]
                print(f"TEST (greedy), {handed} against base ({t['base']}/{t['n']}): {t['acc'] - t['base']:+d} (gained {t['up']} / lost {t['down']})"
                      + "".join(f"; random control of the same size: {result['test']['control']['acc'] - result['test']['control']['base']:+d}" for _ in arms[1:]),
                      file=sys.stderr, flush=True)
            if a.sample_k:
                import sample_eval
                files = {}
                top_p = float(os.environ.get("MATH_TOP_P") or 0)
                cut = f"-p{round(top_p * 100)}" if top_p else ""
                for name, d in (("base", self.base), (handed, self.versions[handed]["dir"])):
                    files[name] = os.path.join(a.eval_out, f"base-test-sampled-k{a.sample_k}{cut}.jsonl" if name == "base" else f"{tag}-test-sampled-k{a.sample_k}{cut}.jsonl")
                    if not os.path.exists(files[name]):
                        subprocess.run([sys.executable, os.path.join(HERE, "sample_eval.py"), "score", "--model", d, "--set", "test",
                                        "--k", str(a.sample_k), "--out", files[name], "--gpu-mem", str(a.gpu_mem), "--texts",
                                        "--gpus", ",".join(getattr(a, "gpus", None) or [])], check=True, env=env)
                r = sample_eval.compare(files["base"], files[handed])
                result["test_sampled"] = {**r, "k": a.sample_k, "temperature": 0.7}
                print(f"TEST (sampled, {a.sample_k} answers per problem): expected solved {r['base']:.1f} -> {r['acc']:.1f}, "
                      f"change {r['change']:+.1f} (paired standard error {r['se']:.1f})", file=sys.stderr, flush=True)
        else:
            print("handed in the untrained student: nothing to score", file=sys.stderr, flush=True)
        json.dump(result, open(os.path.join(a.run, "result.json"), "w"), indent=1)
        print(json.dumps({k: result[k] for k in ("handed_in", "kept", "editor_usd") if k in result}))


def parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="the steering server's config for the base student (model, prompts, template)")
    ap.add_argument("--val", required=True, help="held-out validation problems (id, prompt, answer per line)")
    ap.add_argument("--run", required=True, help="the run directory (the config's run_dir)")
    ap.add_argument("--eval-out", required=True, help="where the cached base test results are (eval_watch.py)")
    ap.add_argument("--model", default="deepseek/deepseek-v4.1-flash")
    ap.add_argument("--providers", default="Together,BaseTen,Fireworks")
    ap.add_argument("--temperature", type=float, default=0.3)
    ap.add_argument("--max-reply", type=int, default=32000); ap.add_argument("--reasoning-tokens", type=int, default=12000)
    ap.add_argument("--provider-sort", default="", help="throughput or latency: among the providers the account may use, try the fastest first "
                                                         "(only when --providers pins none)")
    ap.add_argument("--call-deadline", type=int, default=1200, help="seconds an editor call may take in all before it is given up and tried again")
    ap.add_argument("--reasoning-effort", default="", help="high, medium or low: the editor's reasoning as a named level, the same for every "
                                                           "model (in place of the --reasoning-tokens budget, which some providers ignore)")
    ap.add_argument("--turns", type=int, default=120)
    ap.add_argument("--dtype", default="float32", help="the precision versions are served and scored at")
    ap.add_argument("--gpu-mem", type=float, default=0.8); ap.add_argument("--max-model-len", type=int, default=10240)
    ap.add_argument("--max-num-seqs", type=int, default=0, help="answers a student server writes at once at most (0: vLLM's own default)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--sample-k", type=int, default=4, help="sampled answers per test problem in the final score (0 = greedy only)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--token-budget", type=int, default=0,
                    help="the editor's own tokens, every input token counted once plus all output, over all its conversations: the session "
                         "lasts until they are used (finish is accepted from 95%%), then the editor has one reply to hand in (0: no such limit)")
    ap.add_argument("--train-gpus", type=int, default=1,
                    help="--spread: a training run is laid over this many of the job's GPUs (a student too large for one); their servers "
                         "are stopped for it, and every GPU takes the new version afterwards as always")
    ap.add_argument("--no-archive", action="store_true",
                    help="--local-dir with --keep-versions: do not copy every version to the run folder (a 30-billion-weight version "
                         "is 122 GB); the versions stay on the node's own disk")
    ap.add_argument("--local-dir", default="",
                    help="a folder on the node's own disk for the versions' weights while the session works (loading 30 GB from a "
                         "shared filesystem takes minutes, from a local disk seconds). With --keep-versions each version is also copied to "
                         "the run folder in the background, and a session resumed on another node goes on from those copies")
    ap.add_argument("--keep-versions", action="store_true",
                    help="keep the weights of every version (about 30 GB each): the editor can go back to any of them, and each can be "
                         "scored afterwards. Without it only v0, the live one and the best validated one are kept")
    ap.add_argument("--time-budget-min", type=float, default=0,
                    help="the session's length in minutes of real time, from the moment the editor is first sent its instructions; the editor "
                         "is shown the time used and left with every message. When it is up nothing more comes back from the GPUs and the "
                         "editor has one reply to hand in a version (0: no such limit). The other budgets stay as guards and are not shown")
    ap.add_argument("--wall-min", type=int, default=0,
                    help="a last guard on the job's GPUs: this many minutes after the session first started it wraps up with the version "
                         "that has the highest validation score (0: no such limit)")
    ap.add_argument("--spread", action="store_true",
                    help="one conversation, and every GPU of the job holds the live version: the pool's answers and validation are written by "
                         "all of them, and the actions of one reply that are on different pool problems are carried out at the same time")
    ap.add_argument("--workflows", type=int, default=1,
                    help="conversations of the same editor working at once, one per GPU, every GPU holding the live version "
                         "(1: one conversation; the other GPUs then hold candidate versions)")
    return ap


def main(ap, session):
    a = ap.parse_args()
    a.url, a.log = f"http://127.0.0.1:{a.port}", a.run
    gpus = [g for g in os.environ.get("CUDA_VISIBLE_DEVICES", "").split(",") if g]
    a.gpus = gpus
    a.env = dict(os.environ, CUDA_VISIBLE_DEVICES=gpus[0]) if gpus else dict(os.environ)
    s = session(a)
    try:
        s.run()
    finally:
        s.stop_all()
