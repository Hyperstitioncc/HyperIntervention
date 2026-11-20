import json, math, os, random, re, shutil, subprocess, sys, time, types
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(__file__))
import threading
import session_base
from session_base import Session, hm, hms
from judge_client import SPEND, answer_text, bits, resolve, vis


class Script:
    cost = [0, 0]

    def __init__(self, session):
        self.s, self.rnd, self.queue = session, random.Random(session.a.seed), []
        self.done, self.trained, self.made, self.tries = set(), False, 0, 0
        self.cur = None

    def for_flow(self, w):
        if w == 0:
            return self
        self.s.gone.add(w)
        return types.SimpleNamespace(cost=[0, 0], ask=lambda msgs: ('idle\n```json\n{"action": "stop"}\n```', "", {"provider": "scripted"}))

    def ask(self, msgs):
        while not self.queue:
            self.plan()
        return "scripted\n```json\n" + json.dumps(self.queue.pop(0)) + "\n```", "", {"provider": "scripted"}

    def fork(self, i):
        s, a = self.s, self.s.a
        toks = s.call("open", idx=i)["draft"]["tokens"]
        hot = sorted(sorted((k for k, t in enumerate(toks) if bits(t) >= a.tau), key=lambda k: -bits(toks[k]))[:60])
        if not hot:
            return None
        pos = self.rnd.choice(hot)
        cands = [(k, c) for k, c in enumerate(toks[pos]["top"][:a.show]) if c["token"] != toks[pos]["id"]]
        if not cands:
            return None
        k = self.rnd.choices([k for k, _ in cands], weights=[max(c["prob"], 1e-6) for _, c in cands])[0]
        return {"action": "fork", "idx": i, "pos": pos, "choice": k, "write": "all"}

    def plan(self):
        s, a = self.s, self.s.a
        keyed = a.model.endswith("keyed")
        if self.cur is not None:
            i, pos, n = self.cur
            d = s.call("open", idx=i)["draft"]
            if s.grader(s.rows[i]["answer"], answer_text(d)):
                self.queue.append({"action": "keep", "idx": i})
                self.made, self.cur = self.made + 1, None
                return
            self.queue.append({"action": "undo", "idx": i, "pos": pos})
            nxt = self.fork(i) if n < 8 and s.left["gen"] > 1500 else None
            if nxt is None:
                self.queue.append({"action": "discard", "idx": i})
                self.cur = None
            else:
                self.queue.append(nxt)
                self.cur = (i, nxt["pos"], n + 1)
            return
        wrong = [i for i, st in s.status0.items() if not st["correct"] and i not in self.done]
        if not self.trained and self.made < a.script_edits and wrong and s.left["gen"] > 1500:
            i = self.rnd.choice(wrong)
            self.done.add(i)
            f = self.fork(i)
            if f is not None:
                self.queue.append(f)
                if keyed:
                    self.cur = (i, f["pos"], 1)
                else:
                    self.queue.append({"action": "keep", "idx": i})
                    self.made += 1
            return
        kept = sorted({k["id"] for k in s.kept()})
        by_id = {r["id"]: n for n, r in enumerate(s.rows)}
        if not kept:
            self.queue.append({"action": "finish", "version": "v0"})
        elif a.model.endswith("control"):
            order = [by_id[k["id"]] for k in s.kept()]
            base = {"action": "train", "start": "base", "data": "all", "lr": 2e-7, "hold": "end", "validate": 4}
            self.trained, self.tries = True, self.tries + 1
            two = lambda a_, b_: self.queue.append(dict(base, runs=[a_, b_]))
            if self.tries == 1:
                two({"only": order[:4], "optimizer": "adam", "steps": 8}, {"only": order, "optimizer": "adam", "steps": 8})
            elif self.tries == 2:
                two({"only": order[4:8], "optimizer": "adam", "steps": 8}, {"only": order, "optimizer": "adam", "steps": "auto"})
            elif self.tries == 3:
                two({"only": order[:4], "optimizer": "sgd", "embeddings": "freeze", "lr": 1e-7, "steps": 6},
                    {"only": order, "optimizer": "sgd", "embeddings": "freeze", "steps": 20})
            else:
                self.queue.append({"action": "finish", "version": s.best()})
        elif not self.trained:
            self.trained = True
            self.queue.append({"action": "train", "lr": 2e-7, "steps": "auto", "start": "base", "data": "all", "validate": True})
        elif not a.model.endswith("blind") and s.left["val"] > 0 and s.left["tr"] > 0 and self.tries < a.script_tries:
            self.tries += 1
            half = self.rnd.sample(kept, max(1, len(kept) // 2))
            self.queue.append({"action": "train", "lr": self.rnd.choice([1e-7, 2e-7, 4e-7]), "steps": "auto", "start": "base", "data": "all",
                               "only": sorted(by_id[i] for i in half), "validate": True})
        else:
            self.queue.append({"action": "finish", "version": s.best()})


class Replay:
    cost = [0, 0]

    def __init__(self, session):
        self.s = session
        log = os.path.join(session.a.run, "agent.jsonl")
        done = sum(1 for _ in open(log)) if os.path.exists(log) else 0
        self.queue = [json.loads(l) for l in open(session.a.replay) if l.strip()][done:]

    def for_flow(self, w):
        one = Replay.__new__(Replay)
        one.s, one.queue = self.s, [{k: v for k, v in act.items() if k != "flow"} for act in self.queue if isinstance(act, dict) and act.get("flow", 0) == w]
        return one

    def ask(self, msgs):
        act = self.queue.pop(0) if self.queue else {"action": "wait"} if self.s.job else {"action": "finish", "version": self.s.best()}
        acts = act if isinstance(act, list) else [act]
        return "replay\n" + "\n".join("```json\n" + json.dumps(x) + "\n```" for x in acts), "", {"provider": "replay"}


SWEEP_MAX = 6
NAMES = {"gen": "generation units", "tr": "training units", "val": "validation answers", "chk": "problem checks"}


class Direct(Session):
    def __init__(self, a):
        a.planner_prompt = "instructions.md"
        super().__init__(a)
        if self.scripted:
            self.planner = Replay(self) if a.model == "scripted:replay" else Script(self)
        self.kmax = max(a.eval_k, getattr(a, "val_k_max", 0) or 0)
        self.total = {"gen": a.gen_budget, "tr": a.train_units, "val": a.val_looks * len(self.val_rows) * a.eval_k, "chk": a.problem_checks}
        self.left = dict(self.total)
        p0 = os.path.join(a.run, "status0.json")
        self.have0 = os.path.exists(p0)
        self.status0 = {int(k): v for k, v in json.load(open(p0)).items()} if self.have0 else {}
        if self.K and a.own_pool:
            raise SystemExit("--pool-k is for a given pool (with or without --pose); --own-pool keeps one most likely answer per problem")
        if a.own_pool and self.scripted and a.model != "scripted:replay":
            raise SystemExit("a scripted editor needs a pool with an answer key; --own-pool is for model editors")
        self.posing = a.own_pool or getattr(a, "pose", False)
        want = getattr(a, "candidates", -1)
        self.parallel = max(0, min(len(self.seats) - 1, len(self.seats) - 1 if want < 0 else want))
        if self.flows > 1:
            self.parallel = ((max(len(self.seats) - 1, SWEEP_MAX) if want < 0 else want)
                             if self.solo and want != 0 and getattr(a, "train_gpus", 1) <= 1 else 0)
            if self.posing and not self.solo:
                raise SystemExit("--workflows works on a given pool; posing problems (--pose, --own-pool) is for one conversation")
        self.job, self.reserved = None, {}

    K = property(lambda self: getattr(self.a, "pool_k", 0) or 0)

    def pool_line(self, st):
        live = self.live
        g = [s for i, s in st.items() if self.labelled(i) and s.get("ver", live) == live]
        old = sum(1 for i, s in st.items() if self.labelled(i) and s.get("ver", live) != live)
        every, never = sum(s["right"] == s["k"] for s in g), sum(s["right"] == 0 for s in g)
        return (f"Pool under it, {self.K} sampled answers per problem at temperature {self.a.eval_temperature:g}: "
                f"{sum(s['right'] for s in g)} of {sum(s['k'] for s in g)} answers right; {every} problems right every time, "
                f"{len(g) - every - never} some of the time, {never} never."
                + (f" That is the {len(g)} problems it has answered; the other {old} are answered by it when you open them." if old else ""))

    def labelled(self, i):
        return not self.rows[i]["id"].startswith("own/")

    def posed(self):
        return [i for i in range(len(self.rows)) if not self.labelled(i)]

    @staticmethod
    def fin(s):
        return repr(s["final"]) if s.get("final") is not None else "none"

    @staticmethod
    def fins(s):
        return ", ".join(repr(x) if x is not None else "none" for x in s.get("finals") or [s.get("final")])

    threads = property(lambda self: int(getattr(self.a, "gen_threads", 0) or 32))

    def charge(self, kind, n, what):
        with self.mu:
            self.left[kind] -= int(n)
        if self.time_cap:
            return ""
        return f"[{int(n):,} {NAMES[kind]} for {what}; {max(0, self.left[kind]):,} left]"

    def need(self, kind, n, what):
        have = self.left[kind] - self.reserved.get(kind, 0)
        if have < n:
            raise ValueError(f"{what} needs about {int(n):,} {NAMES[kind]} and {max(0, have):,} are left")

    def budget(self):
        made = self.vnext - 1
        if self.time_cap:
            cap = getattr(self.a, "grad_budget", 0)
            return (self.time_line() + (f" TRAINED TOKENS: {self.versions[self.live].get('grads', 0)} of {cap} in {self.live}." if cap else "")
                    + (f" YOUR TOKENS: {SPEND['tok']:,} used of {self.tok_cap:,}." if self.tok_cap else ""))
        return ("STILL AVAILABLE: " + "; ".join(f"{NAMES[k]}: {max(0, self.left[k]):,} left (of {self.total[k]:,}; {max(0, self.total[k] - self.left[k]):,} used)" for k in NAMES)
                + (f"; training runs: {max(0, self.a.max_trainings - made)} left (of {self.a.max_trainings}; {made} made)" if getattr(self.a, "max_trainings", 0) else "")
                + ("." if self.tok_cap else f". Editor spend so far: ${SPEND['usd']:.2f}" + self.cap_left() + "."))

    @staticmethod
    def cap_left():
        usd, cap = SPEND["usd"], SPEND["cap"]
        if not cap:
            return ""
        note = f" of a ${cap:.2f} cap (${max(0.0, cap - usd):.2f} left)"
        if usd >= 0.8 * cap:
            note += (". WARNING: at the cap the session ends at once and the version with the highest validation score is handed in; "
                     "finish with the version you want before then")
        return note

    def prompt_vars(self):
        k = self.a.eval_k
        own, both = self.a.own_pool, self.posing and not self.a.own_pool
        v = {"gen_budget": f"{self.a.gen_budget:,}", "train_units": f"{self.a.train_units:,}", "val_looks": self.a.val_looks,
                "problem_checks": self.a.problem_checks, "call_cost": self.a.call_cost, "sample_k": self.a.eval_k, "gen_t": self.a.gen_temperature,
                "sample_t": self.a.eval_temperature,
                "pool_text": ((
                    f"- A pool that starts EMPTY, and that you build: you write the problems (the \"pose\" action, up to {self.a.max_own}),\n"
                    "  the student answers each one, and from then on a problem you posed is a pool problem like any other:\n"
                    "  you read its answer token by token, fork it, weight it, keep it and train on it. You pose a problem\n"
                    "  for whatever reason you have: it needs no reference answer, and nothing in the pool is labelled right\n"
                    "  or wrong. You are shown what the student writes and the final answer it reaches, before and after\n"
                    "  every training run; what that means is yours to judge. What the student is scored on in the end is\n"
                    "  competition mathematics in seven subjects (algebra, counting and probability, geometry, intermediate\n"
                    "  algebra, number theory, prealgebra, precalculus) at five levels of difficulty, each problem with one\n"
                    "  final answer. You never see those problems, and a problem that reproduces one of them is refused.")
                    if own else (
                    f"- A pool of {len(self.rows) - len(self.posed())} problems with the student's own answer to each (already written; reading them is free).\n"
                    + ("  You are told which ones it gets wrong, and you are given the reference final answer of every pool\n"
                       "  problem: it is shown with the problem when you open it. You are not given worked solutions. After a fork\n"
                       "  or continuation you are told the final answer it now ends with and whether that matches the reference."
                       if getattr(self.a, "key", False) else
                       "  You are told which ones it gets wrong. You are NOT given reference answers or solutions, and nobody\n"
                       "  tells you whether an answer you have edited is right: you have to know the mathematics yourself.")
                    + ((
                        f"\n- Problems of your own, if you want them: you can add problems to the pool (the \"pose\" action, up to {self.a.max_own}).\n"
                        "  The student answers each one, and from then on a problem you posed is a pool problem like any other:\n"
                        "  you read its answer token by token, fork it, weight it, keep it and train on it. You pose a problem\n"
                        "  for whatever reason you have: it needs no reference answer, and it is never labelled right or wrong.\n"
                        "  For a problem you posed you are shown what the student writes and the final answer it reaches, before\n"
                        "  and after every training run; what that means is yours to judge. A problem that reproduces one of the\n"
                        "  held-out problems the student is scored on is refused.") if both else ""))),
                "pose_action": ((
                    '\n  {"action": "pose", "problem": "...", "subject": "algebra"}\n'
                    "      add a problem of your own to the pool: its text as a competition problem is written (LaTeX allowed),\n"
                    "      without its solution or answer. The student writes its answer to it (its single most likely one),\n"
                    "      which you are shown with the final answer it reaches.\n"
                    + ("      It takes the seconds the student needs to write its answer." if self.time_cap else
                     f"      Costs {self.a.call_cost} units plus one per token the student writes.")) if self.posing else ""),
                "list_action": ('  {"action": "list", "subject": "algebra", "offset": 0}\n'
                                "      the pool under the live student: every problem with the final answer the student reaches."
                                if own else
                                '  {"action": "list", "status": "wrong", "subject": "algebra", "offset": 0}\n'
                                "      pool problems under the live student (status: wrong | right | all | posed). A problem you posed has\n"
                                "      no right or wrong: \"posed\" lists yours, each with the final answer the student reaches."
                                if both else
                                '  {"action": "list", "status": "wrong", "subject": "algebra", "offset": 0}\n'
                                "      pool problems under the live student (status: wrong | right | all)."),
                "eval_pool": ("      k sampled answers to each of these pool problems, with the final answer of each: how steadily does\n"
                              "      the student now end where it does."
                              if own else
                              "      the same on pool problems, with the count for each: how reliably does it now solve them."
                              + ("\n      For a problem you posed: the final answer of each of its k sampled answers." if both else "")),
                "after_training": ((
                    "- for every problem the version was trained on, the final answer the student now reaches BY ITSELF (its\n"
                    f"  single most likely answer, before and now, and the final answers of {k} sampled answers), which other pool\n"
                    "  problems now end with a different final answer (by index: open them and read what changed), and whether\n"
                    "  its answers got longer or stopped ending. Every problem in the pool is answered again by the new version,\n"
                    "  so this is already a reading of what the run did beyond the problems it was trained on; the validation\n"
                    "  set is the held-out check on it.")
                    if own else (
                    "- which of the problems the version was trained on the student now answers correctly BY ITSELF (its single\n"
                    f"  most likely answer, and how many of {k} sampled answers; the share of those sampled answers that are\n"
                    "  right, over all trained problems, is given as one number: how well the corrections HELD), which\n"
                    "  other pool problems became right or wrong (by index: open them and read what changed), and whether its\n"
                    f"  answers got longer or stopped ending. The pool is {len(self.rows) - len(self.posed())} problems, so this is already a broad reading\n"
                    "  of what the run did; the validation set is the held-out check on it."
                    + ((
                        "\n- for every problem you posed that the version was trained on, the final answer the student now reaches by\n"
                        f"  itself (its single most likely answer, when you posed it and now, and the final answers of {k} sampled\n"
                        "  answers), and which of the other problems you posed now end with a different final answer.") if both else ""))),
                "read_note": ("- A single badly chosen correction can damage the student. After a training run, read what it did: where\n"
                              "  the student now ends on the trained problems by itself, and what else changed."
                              if own else
                              "- A single badly chosen correction can damage the student. After a training run, read what it did: which\n"
                              "  trained problems the student now solves by itself, and what else became wrong."),
                "labels_note": (" The final answers you are shown for pool problems are read the same way." if own else
                                " The pool's right / wrong labels are made the same way."
                                + ("\n  So are the final answers you are shown for problems you posed." if both else "")),
                "pose_step": ("\n7. When batches from the pool stop adding (two or three in a row within their give or take), the student has\n"
                              "   taken what those answers had to teach it. A correction made once teaches one answer. If you have seen the\n"
                              "   student fail one kind of step, pose problems of your own that turn on that same step (\"pose\": several of\n"
                              "   them, each different in its numbers and its setting) and correct it on each, so that it meets the decision\n"
                              "   more than once. A posed problem has no reference answer: you judge its answer yourself. It is forked,\n"
                              "   weighted, kept and trained like any pool problem." if self.posing else ""),
                "repeat_n": "8" if self.posing else "7",
                "sweep_how": ('as several runs in one action ("runs": a few learning rates and step counts, each validated)'
                              if self.parallel and self.solo and len(self.seats) - 1 < self.parallel else
                              'as several runs at once ("runs": a few learning rates and step counts, each validated)' if self.parallel else
                              'a few times over, each from the untrained student ("start": "base", "data": "all") with another learning\n'
                              '   rate or step count, each validated'),
                "train_many": "" if not self.parallel else (
                    '\n  {"action": "train", "runs": [{"lr": 1e-7}, {"lr": 2e-7}, {"lr": 1e-7, "optimizer": "adam"}, {"lr": 1e-7, "only": [3, 17]}],\n'
                    '   "validate": 4}\n'
                    f"      a sweep: several training runs in one action (up to {self.parallel}), each with its own settings. This session has\n"
                    f"      {len(self.seats)} GPUs: the live student keeps one, and the runs are made on the other {len(self.seats) - 1}, side by side while there\n"
                    "      is a GPU for each and taking turns when there are more runs than that. A run that waits its turn adds\n"
                    "      its whole time (training, then its validation score on one GPU) to the sweep.\n"
                    '      A run takes everything a single train takes, and what you give outside "runs" holds for\n'
                    "      all of them. Each result is a new version, a CANDIDATE: you are told what the run did to your tokens and\n"
                    '      to the weights, and with "validate" its validation score. The pool is NOT answered by a candidate, and\n'
                    '      the live student does not change: "use" the candidate you want, and every GPU takes it and answers the\n'
                    "      pool. Each run counts as one version."
                    if self.solo and len(self.seats) - 1 < self.parallel else
                    '\n  {"action": "train", "runs": [{"lr": 1e-7}, {"lr": 2e-7}, {"lr": 1e-7, "optimizer": "adam"}, {"lr": 1e-7, "only": [3, 17]}],\n'
                    '   "validate": 4}\n'
                    f"      a sweep: several training runs at once (up to {self.parallel}), each with its own settings, made side by side in the time\n"
                    '      one run takes. A run takes everything a single train takes, and what you give outside "runs" holds for\n'
                    "      all of them. Each result is a new version, a CANDIDATE, held on a GPU of its own: you are told what the\n"
                    '      run did to your tokens and to the weights, and with "validate" its validation score (each candidate\n'
                    "      is scored on its own share of the GPUs, so a sweep's scores take longer the more runs it has). The pool is NOT\n"
                    '      answered by a candidate, and the live student does not change: "use" the candidate you want, and every\n'
                    "      GPU takes it and answers the pool. Each run is charged as a single run is, and counts as one version."
                    if self.solo else
                    '\n  {"action": "train", "runs": [{"lr": 1e-7}, {"lr": 2e-7}, {"lr": 1e-7, "only": [3, 17]}], "validate": true,\n'
                    '   "background": false}\n'
                    f"      several training runs at once (up to {self.parallel}), each with its own settings: a run takes everything a single\n"
                    '      train takes, and what you give outside "runs" holds for all of them. Each result is a new version,\n'
                    "      a CANDIDATE: the pool is answered by it and you are told what it did there (and its validation score,\n"
                    '      with "validate": true) exactly as after a single run, all of them in the time one run takes. The live\n'
                    '      student does NOT change: "use" the candidate you want to go on from. Each run is charged as a single\n'
                    "      run is. A candidate stays loaded, so \"use\" on it is immediate, until its place is needed by a later\n"
                    "      training run; then, as always, only the live version, the best validated one and v0 keep their weights.\n"
                    '      "background": true returns at once: you go on working on the live student while the candidates are\n'
                    "      made, and their reports come with the result of your first action after they are done.\n"
                    '  {"action": "wait"}\n'
                    "      wait for the candidates being made in the background and read their reports. While candidates are\n"
                    "      being made no other training run can start."),
                "tries_action": "" if self.a.no_tries else (
                    '\n      "tries": 4 (with "write": "all", up to 6) continues the same fork that many times and lists the final\n'
                    '      answer of each: how often the fork leads the student to the same answer. Each extra continuation is\n'
                    '      charged like a fork. The first continuation is the one that becomes the answer.')
                   .replace("Each extra continuation is\n      charged like a fork.", "Each extra continuation takes\n      the time of a fork." if self.time_cap else "Each extra continuation is\n      charged like a fork."),
                "tries_advice": ("   - fork the same token again once or twice: if the student gets there each time, the fork is doing the work;"
                                 if self.a.no_tries else
                                 '   - continue the same fork several times ("tries" on the fork) and read the final answers: if the student\n'
                                 '     gets there each time, the fork is doing the work;'),
             "sampled_note": ("- The student's cached answer to a pool problem is its single most likely answer; everything else you see\n"
                              "  (continuations after a fork, validation, problem checks) is sampled.")}
        v["flows_note"] = ""
        same = os.path.basename(os.path.normpath(self.base)).lower() in ("qwen2.5-7b", "model")
        v["rec_student"] = "this student" if same else "another student, Qwen2.5-7B base (7.6 billion weights; nothing here has been measured on this one yet)"
        v["most_new"] = f"{self.most_new():,}"
        ps = getattr(self.a, "pool_sample", 0)
        v["pool_note"] = "" if not (self.solo and ps and self.K) else (
            f'\n      pool (add "pool": 500 or "pool": "all" to a train action): how many pool problems the new version answers right after\n'
            f"      the run, for the report: {ps} unless you say otherwise, always the same ones, plus the problems it was trained on\n"
            f"      and the ones you posed. Answering takes time ({len(self.rows) - len(self.posed())} problems are several minutes; the result says how long).\n"
            '      Every other problem is answered by the live student when you open it, so what you fork is always its own\n'
            '      answer; until then "list" shows such a problem\'s count with the version it comes from.')
        v["end_note"] = (
            ("A finish is accepted only as point 4 says. If the session ends without one, the version with the highest\n"
             "validation score is handed in, whatever number of answers each score has.") if self.tok_cap else
            (f"You can always finish. If you run out of actions ({self.a.turns}) without finishing, the version with the highest\n"
             "validation score is handed in, whatever number of answers each score has."))
        v["use_note"] = ("Every version keeps its weights: you can go\n      back to any of them, or train from it." if getattr(self.a, "keep_versions", False) else
                         "Only v0, the\n      live version and the best validated version keep their weights; any other version keeps its scores but\n"
                         "      cannot be used or trained from.")
        cap = getattr(self.a, "max_trainings", 0)
        v["runs_note"] = f" A session can make at most {cap} versions (training runs) in all." if cap else ""
        g = len(self.seats)
        v["parallel_note"] = (
            " Several actions in one reply: those that each work on one pool problem (open, look, fork,\n"
            "continue, weight, undo, discard, keep) are carried out AT THE SAME TIME when they are on different problems"
            + (f", the student\nbeing held on {g} GPUs" if self.solo else "") + "; actions on the same problem run in the order you give them. So one reply can fork,\n"
            "continue or read as many problems as you like at once. Any other action (list, " + ("pose, " if self.posing else "") + "train, validate, eval, compare,\n"
            "use, status, finish) is carried out alone, in its place in the order. If an action cannot be done, the later actions of\n"
            "that reply on the same problem, and every action after that group, are not run: they were planned on it.")
        v["token_note"] = "" if not self.tok_cap else (
            "\n4. The session's length is not yours to set and you are not told it. The session is ended from outside; you are\n"
            "   then told so and have one last reply, in which only finish is carried out. A finish before that is not accepted"
            + (", nor is\n   another conversation's stop" if self.flows > 1 and not self.solo else "") + ": the session goes on.")
        v["own_note"] = ("The" if self.tok_cap else
                         "They do cost you\nyour own tokens: every answer you open stays in this conversation, so open what you mean to work on, and\nknow that the")
        v["goal_note"] = ": there is no point at which the student is good enough" if self.capped else ""
        v["record_note"] = "" if self.tok_cap else " Your own tokens and dollars are recorded and reported beside your score."
        v["finish_note"] = ("hand in one version: accepted in your last reply, when the session's time is up." if self.time_cap else
                            "hand in one version: accepted in your last reply (budgets, point 4)." if self.tok_cap else "hand in one version and end the session.")
        v["time_limit"] = hm(self.time_cap)
        v["sweep_cost_note"] = ("\n   A sweep takes the time its runs and their validation take. It is worth most at the start, when it settles the\n"
                                "   settings every later run uses." if self.time_cap else " A sweep costs time on the GPUs, not effort of yours.")
        v["using_head"] = "Taking time on the GPUs:" if self.time_cap else "Using a budget:"
        v["status_note"] = "" if self.time_cap else ", budget"
        nv4, free = 4 * len(self.val_rows), max(1, g - 1)
        val_min, run_min = round(nv4 / (g * 4.5) / 60), round(4 + nv4 / 4.5 / 60)
        waves = lambda n: run_min * -(-n // free)
        nv_ = len(self.val_rows)
        per_all, per_one = nv_ / (g * 4.5) / 60, nv_ / 4.5 / 60
        w1 = lambda n: round(4 + per_one) * -(-n // free)
        ga_ = lambda k: math.sqrt(nv_ * (0.0045 + 0.172 / k))
        v["time_costs"] = (
            "What things take, on record (8 GPUs):\n"
            "- open, look, weight, undo, discard, keep, status: nothing to speak of.\n"
            "- a fork with its continuations: seconds. Forks on different problems in one reply run at the same time.\n"
            "- one training run without validation: about 5 minutes, of which the training is about 2 and the rest is the\n"
            "  GPUs taking the new version and answering the pool.\n"
            "- a validation score at 4 answers a problem: about 6 minutes more, and more answers in proportion.\n"
            "- several runs in one train action, each validated at 4: about 17 minutes for two, 24 for three, and 35 to 40\n"
            "  for four to six." if g == 8 else
            f"What things take with this session's {g} GPUs, from sessions like it (every result says what it really took):\n"
            "- open, look, weight, undo, discard, keep, status: nothing to speak of.\n"
            "- a fork with its continuations: seconds. Forks on different problems in one reply run at the same time.\n"
            "- one training run without validation: the training (about a minute and a half, and 0.6 seconds more per\n"
            "  correction per step: 6 minutes for 40 corrections and 10 steps) and about 4 minutes for the GPUs to take the\n"
            "  new version and answer the pool.\n"
            f"- a validation score: about {per_all:.1f} minutes for every answer per problem you ask for ({val_min} minutes at 4 answers,\n"
            f"  {round(8 * per_all)} at 8, {round(16 * per_all)} at 16; {round(per_all)} at 1).\n"
            f"- several runs in one train action: each run is trained and then scored on ONE GPU, {free} run{'s' if free > 1 else ''} at a time, so its score\n"
            f"  takes about {per_one:.1f} minutes for every answer per problem. A run with a score at 4 answers is about {run_min} minutes\n"
            f"  ({waves(2)} for two runs, {waves(3)} for three, {waves(6)} for six); at 1 answer about {round(4 + per_one)} ({w1(2)} for two, {w1(3)} for three, {w1(6)} for six).\n"
            "- so the time a sweep or a score takes is yours to set, by the number of runs and the answers per problem, and\n"
            "  what you give up for speed is precision: with fewer answers the give or take of a difference is larger\n"
            f"  (about {ga_(1):.0f} problems of {nv_:,} at 1 answer per problem, {ga_(2):.0f} at 2, {ga_(4):.1f} at 4, {ga_(8):.1f} at 8, {ga_(16):.1f} at 16).")
        cap = getattr(self.a, "grad_budget", 0)
        v.update({f"loop_ga{k}": f"{ga_(k):.1f}" for k in (1, 4, 16)})
        v["grad_note"] = "" if not cap else (
            f"What you train is limited too: a version can have at most {cap} trained tokens in it. That counts, over every\n"
            "correction trained into the version, each forked token that is trained up, each token a fork replaced that is\n"
            "pushed down, and each token you gave a weight; the end tokens \"hold\" adds are not counted. A training run\n"
            "that would make a version with more is refused. The live version's count is shown with the time.\n")
        v["grad_note"] += "" if not (self.time_cap and self.tok_cap) else (
            f"Your own tokens are limited as well: {self.tok_cap:,} in all. Every token you are sent counts once (a result counts\n"
            "when it first reaches you, not again as the conversation grows), and every token you write counts, reasoning\n"
            "included. On record, an opened answer is about 4,000 tokens and the result of a fork 2,000 to 4,000. When they\n"
            "are used up the session ends as it does when the time is up: you have one last reply in which to hand in a\n"
            "version. Your count is shown with the time.\n")
        tests, nv = len(session_base.load("test")), len(self.val_rows)
        ga = lambda k: math.sqrt(nv * (0.0045 + (0.172 / k if k else 0.0)))
        v.update({
            "gen_forks": f"{self.total['gen'] // (self.a.call_cost + 450):,}", "opt_default": getattr(self.a, "default_optimizer", "sgd"), "emb_default": "freeze" if getattr(self.a, "default_optimizer", "sgd") == "sgd" else "train",
            "kmax": self.kmax, "auto_cap": self.a.auto_cap, "val_answers": f"{self.total['val']:,}", "test_n": f"{tests:,}", "test_k": self.a.sample_k,
            "optimizer_text": (
                f'      optimizer "{getattr(self.a, "default_optimizer", "sgd")}" is the default. "adam": AdamW (betas 0.9 and 0.999, no weight decay), on the gradient scaled down to length 1\n'
                "      when it is longer. On its first step EVERY weight moves by lr, in the direction of its own gradient's\n"
                "      sign, whether that gradient is large or tiny; later steps are about that size while a weight's gradient\n"
                "      keeps its sign. How far the weights move is set by lr and the number of steps, not by how many\n"
                "      corrections you train or how strongly they pull.\n"
                '      optimizer "sgd": every step moves the weights along the gradient itself, each weight in proportion to its\n'
                "      own gradient, so weights that matter little to your tokens hardly move. The step is made as long as it\n"
                "      takes to lower the loss by what adam's first step at the same lr would (to first order): the same lr\n"
                '      asks for the same progress on your tokens from either optimizer. "momentum" (0 to 0.95, default 0; sgd\n'
                "      only): each step also carries that fraction of the step before it, and is itself shortened to\n"
                "      (1 - momentum) of its length, so the first steps are short and build up.\n"
                '      "embeddings": "freeze" (the default with sgd) or "train" (the default with adam): frozen, the token embeddings and the output layer (14% of the\n'
                "      weights) stay where the run started. They carry about three quarters of the gradient's squared length,\n"
                "      so an sgd step that trains them is mostly a move of theirs; adam moves every weight alike either way.\n"
                "      lr: 1e-9 to 1e-3. On record: the 182 training runs model editors made on this student before were all adam,\n"
                "      with lr from 1e-8 to 3e-6 and half of them at 1e-7 or below."),
            "noise_record": (
                "- On record for this student, with 500 validation problems and 4 answers each: the give or take of a version\n"
                "  against the untrained student was 4.4 problems (median of 218 versions), and against the version it\n"
                "  was trained from 3.9. Of 73 training runs in four sessions, 39 read more than one give-or-take away from\n"
                "  the version they started from and 9 more than two. The 19 runs among them that started from the untrained\n"
                "  student with one to four corrections changed validation by +1.6 problems of 500 on average, give or take 1.1;\n"
                "  the run of 43 corrections in the table above changed its held-out score by +5.7 of 100.\n"
                "- More answers per problem make the give or take smaller, down to a floor. Measured on this student over 1,494\n"
                "  held-out problems (a version trained on 43 corrections against the untrained one): the variance of one\n"
                "  problem's difference was 0.176 with 1 answer per problem, 0.091 with 2, 0.046 with 4 and 0.027 with 8, which\n"
                "  fits 0.0045 + 0.172 / k. For P problems the give or take is the square root of P times that: for the\n"
                f"  {nv} validation problems about {ga(4):.1f} at k = 4, {ga(8):.1f} at k = 8, {ga(16):.1f} at k = 16, {ga(64):.1f} at k = 64, and {ga(0):.1f} however\n"
                "  many answers (versions that differ less have a lower floor)."),
            "v0_note": ("" if not getattr(self.a, "min_corrections", 0) else
                        f"\n- The untrained student cannot be handed in until you have kept {self.a.min_corrections} corrections or used half of the\n"
                        "  generation budget."),
            "floor_note": ("" if not getattr(self.a, "min_kept", 0) else
                           f"\n- No version at all can be handed in before {self.a.min_kept} corrections are kept or half of the generation budget is used."),
            "cap_note": ("" if not SPEND["cap"] or self.tok_cap else
                         f" When your own spend reaches ${SPEND['cap']:.0f} the session ends as if you had run out of actions; \"status\" shows the spend."),
            "test_record": (" In earlier sessions its give or take was 19 to 23 problems of 5,000." if tests == 5000 else ""),
            "test_sampling": ("sampled exactly as validation is" if abs(float(os.environ.get("MATH_TOP_P") or 0) - 0.95) < 1e-9 else
                              f"temperature {self.a.eval_temperature:g}, with no cut-off of unlikely tokens")})
        if self.K:
            v.update(self.prompt_vars_sampled(both))
        if v["rec_student"] != "this student":
            for key in ("noise_record", "optimizer_text"):
                v[key] = (v[key].replace("for this student, with 500", f"for {v['rec_student']}, with 500")
                          .replace("Measured on this student over", f"Measured on {v['rec_student']} over")
                          .replace("model editors made on this student before", f"model editors made on {v['rec_student']} before")
                          .replace("times that: for the", "times that: on that student, for the"))
        return v

    def prompt_vars_sampled(self, both):
        K, T, k, n = self.K, self.a.eval_temperature, self.a.eval_k, len(self.rows) - len(self.posed())
        v = {"pool_text": (
                f"- A pool of {n} problems, each answered {K} times by the student: sampled answers at temperature {T:g}, as the\n"
                f"  final test samples them (already written; reading them is free). For every problem you are told how many\n"
                f"  of the {K} are right. The answer you open and work on is one of those sampled answers: the first wrong one,\n"
                "  or the first of them when none is wrong.\n"
                + ("  You are given the reference final answer of every pool problem: it is shown with the problem when you\n"
                   "  open it. You are not given worked solutions. After a fork or continuation you are told the final answer\n"
                   "  it now ends with and whether that matches the reference."
                   if getattr(self.a, "key", False) else
                   "  You are NOT given reference answers or solutions, and nobody tells you whether an answer you have\n"
                   "  edited is right: you have to know the mathematics yourself.")
                + ((
                    f"\n- Problems of your own, if you want them: you can add problems to the pool (the \"pose\" action, up to {self.a.max_own}).\n"
                    f"  The student answers each one {K} times, and from then on a problem you posed is a pool problem like any\n"
                    "  other: you read its answer token by token, fork it, weight it, keep it and train on it. You pose a\n"
                    "  problem for whatever reason you have: it needs no reference answer, and it is never labelled right or\n"
                    "  wrong. For a problem you posed you are shown what the student writes and the final answers it reaches,\n"
                    "  before and after every training run; what that means is yours to judge. A problem that reproduces one\n"
                    "  of the held-out problems the student is scored on is refused.") if both else "")),
             "list_action": (
                '  {"action": "list", "status": "wrong", "subject": "algebra", "offset": 0}\n'
                f"      pool problems under the live student, each with how many of its {K} sampled answers are right and whether\n"
                "      the answer you would open is a right or a wrong one. status: wrong (at least one sampled answer is\n"
                "      wrong) | sometimes (some right, some wrong) | never (none right) | right (all right) | all"
                + (" | posed.\n      A problem you posed has no right or wrong: \"posed\" lists yours, each with the final answer the student reaches."
                   if both else ".")),
             "after_training": (
                f"- for every problem the version was trained on, how many of its {K} sampled answers are right, untrained and\n"
                "  now (and over all trained problems as one number: how well the corrections HELD); over the other pool\n"
                "  problems, how many sampled answers are right, untrained and now, and which problems have more or fewer\n"
                "  right answers than before (by index: open them and read what changed); and whether its answers got\n"
                + (f"  longer or stopped ending. Every pool problem is answered again {K} times by the new version, with the same\n"
                   f"  random seeds as before. The pool is {n} problems, so this is already a broad reading of what the run did;\n"
                   "  the validation set is the held-out check on it."
                   if not (self.solo and getattr(self.a, "pool_sample", 0)) else
                   f"  longer or stopped ending. The new version answers, {K} times each and with the same random seeds as before,\n"
                   "  the problems it was trained on and a number of the others (\"pool\" on the train action; the same\n"
                   "  problems each time): the report is about those. The validation set is the held-out check on it.")
                + ((
                    "\n- for every problem you posed that the version was trained on, the final answers of its sampled answers,\n"
                    "  when you posed it and now, and which of the other problems you posed now end differently.") if both else "")),
             "labels_note": " The pool's sampled answers are read the same way.",
             "sampled_note": (f"- Everything you see of the student is sampled: the pool's answers ({K} per problem at temperature {T:g}),\n"
                              "  continuations after a fork, validation and problem checks.")}
        if self.posing:
            v["pose_action"] = (
                '\n  {"action": "pose", "problem": "...", "subject": "algebra"}\n'
                "      add a problem of your own to the pool: its text as a competition problem is written (LaTeX allowed),\n"
                f"      without its solution or answer. The student answers it {K} times; you are shown the first of those\n"
                "      answers, and the final answer each of them reaches.\n"
                + ("      It takes the seconds the student needs to write its answer." if self.time_cap else
                     f"      Costs {self.a.call_cost} units plus one per token the student writes."))
        return v

    def flow_note(self, w):
        n = self.flows
        return (f"\nYOU ARE ONE OF {n} CONVERSATIONS of the same editor working on this student at the same time. Each has its own GPU,\n"
                "and every GPU holds the live student. You are conversation " + (f"0, the lead.\n" if w == 0 else f"{w}.\n")
                + f"- The pool is shared out: a problem belongs to the conversation whose number its idx leaves when divided by {n}.\n"
                f"  Yours are those that leave {w}; \"list\" shows only yours, and an action on another conversation's problem is\n"
                "  refused. What is refused to nobody: status, compare, and eval of pool problems.\n"
                "- Every conversation keeps corrections into ONE set, and the budgets are one set too, drawn on by all.\n"
                + ("- Only you train, validate, use and finish. A training run trains what every conversation has kept. The new\n"
                   "  version then replaces the live student on every GPU; the pool's answers and validation are written by all\n"
                   "  the GPUs together. You are told when another conversation keeps a correction or stops; the problems of a\n"
                   "  conversation that has stopped become yours. {\"action\": \"wait\"} waits for such news; it returns at once\n"
                   "  when every other conversation is itself waiting or has stopped.\n"
                   if w == 0 else
                   "- You open, fork, weight and keep. Training, validating, going back to a version and handing in are the\n"
                   "  lead's (conversation 0). When the lead trains or goes back to a version, that version replaces the live\n"
                   "  student on your GPU, forks and weights you had not kept are dropped, and you are told what the lead was\n"
                   "  told. {\"action\": \"wait\"} waits for that; {\"action\": \"stop\"} ends your conversation, and your problems\n"
                   "  then go to the lead.\n"))

    def flow_first(self, w, first):
        mine = sum(1 for i in range(len(self.rows)) if self.owner_of(i) == w)
        return first + f" You are conversation {w}{' (the lead)' if w == 0 else ''} of {self.flows}; {mine} of the {len(self.rows)} pool problems are yours."

    def actions(self):
        def told(do):
            def run(act):
                out = do(act)
                news = self.news()
                return out + ("\n\n" + news if news else "")
            return run
        return {k: told(v) for k, v in self.acts().items()}

    def acts(self):
        return {"list": self.do_list, "open": self.do_open, "look": self.do_look, "weight": self.do_weight, "undo": self.do_undo,
                "discard": self.do_discard, "keep": self.do_keep, "use": self.do_use, "status": self.do_status,
                "fork": self.do_fork, "continue": self.do_continue, "train": self.do_train, "eval": self.do_eval,
                "validate": self.do_validate, "compare": self.do_compare, "pose": self.do_pose, "wait": self.do_wait}

    def go_live(self, name):
        if self.solo:
            lead = self._seat
            if not all(s.name == name and s.srv is not None and not s.student.down() for s in self.seats):
                answered = lead.name == name and lead.srv is not None and not lead.student.down() and len(lead.status) == len(self.rows)
                before = dict(lead.status)
                self.serve_all(name, self.versions[name]["dir"], lead)
                if answered:
                    for s in self.seats:
                        if s is not lead:
                            s.status, s.have = lead.status, set()
                else:
                    self.screen(lead, None, self.pool_ids(name, self.trained_on(name)), before)
        out = super().go_live(name)
        mine = len(self.posed())
        if mine == len(self.rows):
            out = f"{name} is the live student. The pool holds {mine} problems of yours."
        elif self.K:
            out = f"{name} is the live student. {self.pool_line(self.status)}" + (f" And {mine} problems you posed." if mine else "")
        elif mine:
            right = sum(bool(s["correct"]) for s in self.status.values())
            out = (f"{name} is the live student. Pool under it: {right} right / {len(self.rows) - mine - right} wrong, "
                   f"and {mine} problems you posed.")
        if not self.have0:
            if name != "v0":
                raise RuntimeError("status0.json is missing for a resumed session")
            self.status0 = dict(self.status)
            self.save0()
        for i in self.status:
            self.status0.setdefault(i, self.status[i])
        self.versions[name]["pool"] = self.pool_marks(self.status, name)
        if name == "v0" and not self.versions["v0"].get("val"):
            out += "\n" + self.v0_val()
        return out

    def v0_val(self):
        import hashlib
        tag = hashlib.sha1(open(self.a.val, "rb").read()).hexdigest()[:10]
        f = os.path.join(self.a.eval_out, f"base-val-{tag}-t{self.a.eval_temperature:g}.jsonl")
        have = {r["id"]: r["marks"] for r in map(json.loads, open(f))} if os.path.exists(f) else {}
        k0 = min(len(have[i]) for i in self.counted) if all(i in have for i in self.counted) else 0
        if k0 < self.kmax:
            new = dict(self.val_answers(k=self.kmax - k0, first=k0))
            have = {i: (have[i][:k0] if k0 else "") + "".join("1" if x else "0" for x in new[i]) for i in self.counted}
            os.makedirs(self.a.eval_out, exist_ok=True)
            with open(f + ".tmp", "w") as fh:
                fh.writelines(json.dumps({"id": i, "marks": m}) + "\n" for i, m in have.items())
            os.replace(f + ".tmp", f)
        return self.val_record("v0", [(i, [c == "1" for c in have[i][:self.kmax]]) for i in self.counted], charge=False)

    def pool_marks(self, status, name=None):
        return {str(i): (s["right"] if self.K else int(bool(s["correct"]))) for i, s in status.items()
                if self.labelled(i) and (name is None or s.get("ver", name) == name)}

    def pool_change(self, now, ref, ids):
        val = lambda i: now[i]["right"] if self.K else int(bool(now[i]["correct"]))
        ids = [i for i in ids if str(i) in ref]
        d = [val(i) - ref[str(i)] for i in ids]
        n, mean = max(1, len(d)), sum(d) / max(1, len(d))
        se = math.sqrt(sum((x - mean) ** 2 for x in d) / max(1, n - 1) * n)
        return {"now": sum(val(i) for i in ids), "ref": sum(ref[str(i)] for i in ids), "n": len(ids), "change": sum(d), "se": se,
                "up": [i for i, x in zip(ids, d) if x > 0], "down": [i for i, x in zip(ids, d) if x < 0]}

    def save0(self):
        self.have0 = True
        more = ("right", "k", "shown", "finals", "mean_tokens", "cut")
        json.dump({str(k): {"correct": v["correct"], "final": v.get("final"), "tokens": v["tokens"], "boxed": v["boxed"], "ended": v["ended"],
                            **{f: v[f] for f in more if f in v}}
                   for k, v in self.status0.items()}, open(os.path.join(self.a.run, "status0.json"), "w"))

    def read_answer(self, i, seat=None):
        from math_eval import last_boxed, needs_finish, read
        srv = (seat or self.seat).srv
        if self.K:
            return self.read_sampled(i, srv)
        r = self.ask(srv, "open", idx=i)
        d = r["draft"]
        text = read(answer_text(d))
        boxed = "\\boxed" in text
        if needs_finish(text):
            text = self.ask(srv, "sample", idx=i, k=1, temperature=0.0)["answers"][0]["text"]
        return {"correct": self.grader(self.rows[i]["answer"], text) if self.labelled(i) else None, "final": last_boxed(text),
                "tokens": len(d["tokens"]), "boxed": boxed,
                "ended": d["ended_by"], "answer": [t["id"] for t in d["tokens"]]}, r

    def read_sampled(self, i, srv):
        from math_eval import last_boxed
        r = self.ask(srv, "rollouts", idx=i, k=self.K, temperature=self.a.eval_temperature, seed=0)
        ans = r["answers"]
        marks = [bool(self.grader(self.rows[i]["answer"], a["text"])) for a in ans] if self.labelled(i) else None
        j = marks.index(False) if marks and not all(marks) else 0
        d = self.ask(srv, "open", idx=i, rollout=j)["draft"]
        return {"correct": all(marks) if marks else None, "right": sum(marks) if marks else None, "k": len(ans),
                "shown": marks[j] if marks else None, "final": last_boxed(ans[j]["text"]), "finals": [last_boxed(a["text"]) for a in ans],
                "tokens": len(d["tokens"]), "mean_tokens": sum(a["length"] for a in ans) / len(ans),
                "cut": sum(a["ended_by"] != "eos" for a in ans), "boxed": ans[j]["boxed"], "ended": d["ended_by"],
                "answer": [t["id"] for t in d["tokens"]]}, {**r, "draft": d}

    def pool_ids(self, name, trained=(), want=None, n=None):
        total = len(self.rows) if n is None else n
        if want is None:
            want = getattr(self.a, "pool_sample", 0) or "all"
        if not self.solo or name == "v0" or want == "all" or int(want) >= total:
            return None
        if not hasattr(self, "pool_order"):
            self.pool_order = random.Random(getattr(self.a, "seed", 0)).sample(range(total), total)
        keep = set(int(i) for i in trained) | {i for i in range(total) if not self.labelled(i)}
        keep |= set([i for i in self.pool_order if i < total and i not in keep][:int(want)])
        return sorted(keep)

    def trained_on(self, name):
        by_id = {r["id"]: n for n, r in enumerate(self.rows)}
        inc = set(self.versions.get(name, {}).get("included") or [])
        return sorted({by_id[k["id"]] for k in self.kept() if k["key"] in inc and k["id"] in by_id})

    def screen(self, seat, n=None, ids=None, prev=None):
        part = ids is not None
        ids = range(len(self.rows) if n is None else n) if ids is None else ids
        if self.flows == 1:
            with ThreadPoolExecutor(self.threads) as ex:
                seat.status = dict(ex.map(lambda i: (i, self.read_answer(i, seat)[0]), ids))
            return
        up = {w: s for w, s in enumerate(self.seats) if s.name == seat.name and s.srv is not None and not s.student.down()}
        on = lambda i: up.get(i % self.flows, seat)
        def one(i):
            st = self.read_answer(i, on(i))[0]
            st["ver"] = seat.name
            return i, st
        with ThreadPoolExecutor(self.threads * max(1, len(up))) as ex:
            new = dict(ex.map(one, ids))
        status = {**{i: s for i, s in (prev or {}).items() if part}, **new}
        for s in set(up.values()) | {seat}:
            s.status, s.have = status, {i for i in ids if on(i) is s}

    def serve_all(self, name, model_dir, first):
        def one(s):
            try:
                if s.name != name or s.srv is None or s.student.down():
                    s.clear()
                    s.name, s.srv = name, s.student.start(model_dir, int(name[1:]))
            except Exception as e:
                s.clear()
                if s is first:
                    errs.append(e)
                print(f"HARNESS FAILURE: GPU {self.seats.index(s)} could not serve {name}: {e!r}", file=sys.stderr, flush=True)
        errs = []
        ts = [threading.Thread(target=one, args=(s,), daemon=True) for s in self.seats]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        if errs:
            raise errs[0]

    def load(self, seat, name, model_dir=None):
        if self.flows == 1:
            return super().load(seat, name, model_dir)
        self.serve_all(name, model_dir or self.versions[name]["dir"], seat)
        self.screen(seat)

    def servers(self, seat=None):
        seat = seat or self.seat
        if self.flows == 1:
            return [seat.srv]
        return [s.srv for s in self.seats if s.name == seat.name and s.srv is not None and not s.busy and not s.student.down()] or [seat.srv]

    def catch_up(self, seat):
        if self.flows > 1:
            return
        for i in range(len(seat.status), len(self.rows)):
            r = self.rows[i]
            self.ask(seat.srv, "add", id=r["id"], prompt=r["prompt"], extra={"answer": r.get("answer", ""), "level": r.get("level", "")})
            seat.status[i] = self.read_answer(i, seat)[0]

    @staticmethod
    def words(text):
        return re.sub(r"[^a-z0-9]+", " ", text.lower()).split()

    def held_out(self, text):
        if not hasattr(self, "held"):
            self.held = {"exact": set(), "runs": {}}
            for n, r in enumerate(list(self.val_rows) + list(session_base.load("test"))):
                w = self.words(r["prompt"])
                self.held["exact"].add(" ".join(w))
                for k in range(len(w) - 3):
                    self.held["runs"].setdefault(" ".join(w[k:k + 4]), set()).add(n)
        w = self.words(text)
        if " ".join(w) in self.held["exact"]:
            return True
        runs = [" ".join(w[k:k + 4]) for k in range(len(w) - 3)]
        if len(runs) < 8:
            return False
        hits = {}
        for g in runs:
            for n in self.held["runs"].get(g, ()):
                hits[n] = hits.get(n, 0) + 1
        return bool(hits) and max(hits.values()) >= 0.7 * len(runs)

    def do_pose(self, act):
        a = self.a
        if not self.posing:
            raise ValueError("this session works on a given pool; problems cannot be added to it")
        text = str(act.get("problem", "")).strip()
        subj = re.sub(r"[^a-z_]+", "_", str(act.get("subject", "own")).lower()).strip("_") or "own"
        if not 20 <= len(text) <= 3000:
            raise ValueError("\"problem\" must be the text of one problem (20 to 3000 characters)")
        if "\\boxed" in text:
            raise ValueError("a problem is posed without its answer")
        if len(self.posed()) >= a.max_own:
            raise ValueError(f"the pool is full ({a.max_own} problems)" if a.own_pool else f"you have posed {a.max_own} problems, the most allowed")
        if any(self.words(r["prompt"]) == self.words(text) for r in self.rows):
            raise ValueError("this problem is already in the pool")
        if self.held_out(text):
            raise ValueError("refused: this is, or is close to, a problem of the held-out sets the student is scored on")
        held = self.hold_gen(a.call_cost + self.most_new() * max(1, self.K), "the student's answer to a new problem")
        try:
            row = {"id": f"own/{subj}/{len(self.rows)}", "prompt": text, "answer": "", "level": ""}
            with open(self.prompts, "a") as f:
                f.write(json.dumps(row) + "\n")
            self.rows.append(row)
            seats = [x for x in self.seats if x.name == self._seat.name and x.srv is not None and not x.student.down()] if self.solo else [self.seat]
            for x in seats:
                i = self.ask(x.srv, "add", id=row["id"], prompt=text, extra={"answer": "", "level": ""})["idx"]
                assert i == len(self.rows) - 1, (i, len(self.rows))
            home = self.seat_of(i) if self.solo else self.seat
            s, r = self.read_answer(i, home)
            if getattr(home, "have", None) is not None:
                home.have.add(i)
        finally:
            self.release_gen(held)
        self.status[i] = s
        self.status0[i] = s
        self.save0()
        ends = (f"{self.live}'s {s['k']} sampled answers to it end with the final answers {self.fins(s)}; the first is the one you work on."
                if self.K else f"{self.live}'s own answer to it ends with the final answer {self.fin(s)}.")
        return (self.charge("gen", a.call_cost + r.get("generated", s["tokens"]), "the student's answer to the new problem") + "\n"
                + f"Problem {i} ({row['id']}) is in the pool. {ends}"
                  f"\nThe answer as it now stands:\n{self.show(r['draft'])}")

    def do_list(self, act):
        subj, want, off = act.get("subject"), act.get("status", "wrong"), int(act.get("offset", 0))
        if subj:
            subj = re.sub(r"[^a-z]+", "_", str(subj).lower().replace("&", "and")).strip("_")
        names = sorted({r["id"].split("/")[1] for r in self.rows})
        if subj and not any(subj in n for n in names):
            raise ValueError(f"no subject matches {subj!r}; the pool's subjects are {names}")
        mine = [i for i in self.posed() if not subj or subj in self.rows[i]["id"]]
        given = len(self.rows) - len(self.posed())
        out = []
        if given and want != "posed":
            out.append(self.list_sampled(act) if self.K else
                       super().do_list(act).replace("wrong, no boxed answer", "wrong (it did not box its answer; read as wrong all the same)"))
            if mine and want != "all":
                out.append(f'({len(mine)} problems you posed are not in this list: "status": "posed" lists them.)')
        if not given or want in ("posed", "all"):
            made = {}
            for k in self.kept():
                made.setdefault(k["id"], []).append(f"kept@v{k['round']}")
            rows = [f"{i} | {self.rows[i]['id'].split('/')[1]} | {self.fin(self.status[i])} | {self.status[i]['tokens']} tok"
                    f"{' (cut off)' if self.status[i]['ended'] != 'eos' else ''} | {' '.join(made.get(self.rows[i]['id'], [])) or '-'}" for i in mine]
            page = rows[off:off + 120]
            out.append(f"{'Problems you posed, under' if given else 'Pool under'} {self.live}: {len(rows)} problems match (showing {off}-{off + len(page)}):\n"
                       "idx | subject | the student's final answer | answer length | corrections kept so far\n" + "\n".join(page))
        return "\n".join(out)

    def list_sampled(self, act):
        want, subj, off = act.get("status", "wrong"), act.get("subject"), int(act.get("offset", 0))
        test = {"all": lambda s: True, "right": lambda s: s["right"] == s["k"], "wrong": lambda s: s["right"] < s["k"],
                "sometimes": lambda s: 0 < s["right"] < s["k"], "never": lambda s: s["right"] == 0}
        if want not in test:
            raise ValueError(f'"status" is one of {sorted(test)}' + (", posed" if self.posed() else ""))
        made, rows, by = {}, [], {}
        for k in self.kept():
            made.setdefault(k["id"], []).append(f"kept@v{k['round']}")
        for i, r in enumerate(self.rows):
            if not self.mine(i) or not self.labelled(i):
                continue
            s, name = self.status[i], r["id"].split("/")[1]
            b = by.setdefault(name, [0, 0])
            b[0], b[1] = b[0] + s["right"], b[1] + s["k"]
            if test[want](s) and (not subj or subj in r["id"]):
                rows.append(f"{i} | {name} | {r.get('level', '')} | {s['right']}/{s['k']}{' under ' + s['ver'] if s.get('ver', self.live) != self.live else ''} | "
                            f"{'right' if s['shown'] else 'wrong'}, {s['tokens']} tok"
                            f"{' (cut off)' if s['ended'] != 'eos' else ''} | {' '.join(made.get(r['id'], [])) or '-'}")
        page = rows[off:off + 120]
        head = "; ".join(f"{k} {v[0]}/{v[1]}" for k, v in sorted(by.items()))
        return (f"Pool under {self.live}, sampled answers right of those taken, by subject: {head}\n"
                f"{len(rows)} problems match (showing {off}-{off + len(page)}):\n"
                "idx | subject | level | sampled answers right | the answer you open | corrections kept so far\n" + "\n".join(page))

    def effect_sampled(self, trained, seat, start=None):
        now, was, K = seat.status, self.status0, self.K
        fresh = lambda i: now[i].get("ver", seat.name) == seat.name
        given, mine = [i for i in now if self.labelled(i) and fresh(i)], [i for i in now if not self.labelled(i) and fresh(i)]
        tot = lambda st, ids: sum(st[i]["right"] for i in ids)
        lines = []
        if given:
            tr = [i for i in trained if self.labelled(i)]
            rest = [i for i in given if i not in set(tr)]
            head = (f"What {seat.name} does on the pool, against the untrained student: {K} sampled answers per problem at temperature "
                    f"{self.a.eval_temperature:g}, drawn with the same random seeds for every version.")
            if tr:
                each = ", ".join(f"{i}: {was[i]['right']}→{now[i]['right']}" for i in tr[:80])
                lines.append(f"- the {len(tr)} problems it was trained on, sampled answers right of {K} (problem: untrained→now): {each}. "
                             f"Over all of them {tot(now, tr)} of {len(tr) * K} are right (untrained: {tot(was, tr)}): how well the corrections held")
            c = self.pool_change(now, self.pool_marks({i: was[i] for i in rest}), rest)
            lines.append(f"- the {'other ' if tr else ''}{len(rest)} problems: {c['now']} of {len(rest) * K} sampled answers right "
                         f"(untrained: {c['ref']}; change {c['change']:+d}, give or take {c['se']:.0f}); {len(c['up'])} problems have more right "
                         f"answers than before {c['up'][:25]}, {len(c['down'])} have fewer {c['down'][:25]}")
            ref = self.versions.get(start, {}).get("pool") if start and start != "v0" else None
            if ref:
                c = self.pool_change(now, ref, rest)
                lines.append(f"- the same {c['n']} problems against {start}, which it was trained from: {c['now']} sampled answers right ({start}: {c['ref']}; "
                             f"change {c['change']:+d}, give or take {c['se']:.0f}); {len(c['up'])} problems have more right answers than under {start} "
                             f"{c['up'][:25]}, {len(c['down'])} have fewer {c['down'][:25]}")
        else:
            head = (f"What {seat.name} does on the pool, against the student that first answered each problem: {K} sampled answers per "
                    f"problem at temperature {self.a.eval_temperature:g}; nothing here is labelled right or wrong.")
        if mine:
            m = " you posed" if given else ""
            tr = [i for i in trained if not self.labelled(i)]
            rest = [i for i in mine if i not in set(tr)]
            if tr:
                ends = "; ".join(f"{i}: {self.fins(was[i])} → {self.fins(now[i])}" for i in tr[:80])
                lines.append(f"- the {len(tr)} problems{m} that it was trained on, the final answers of their {K} sampled answers "
                             f"{'when you posed each' if given else 'before'} → now: {ends}")
            moved = [i for i in rest if now[i]["finals"] != was[i].get("finals")]
            lines.append(f"- the {'other ' if tr else ''}{len(rest)} problems{m}: {len(moved)} now end differently in at least one sampled answer {moved[:40]}")
        grp = given or mine
        longer = sum(now[i]["mean_tokens"] for i in grp) / max(1, sum(was[i]["mean_tokens"] for i in grp))
        lines.append(f"- its sampled answers{' to the given problems' if given and mine else ''} are {longer:.2f}x as long as "
                     f"{'the untrained student' if given else 'before'}'s; {sum(now[i]['cut'] for i in grp)} of {len(grp) * K} run to the length limit "
                     f"({'untrained' if given else 'before'}: {sum(was[i]['cut'] for i in grp)}). Open any of these problems to see what changed.")
        return head + "\n" + "\n".join(lines)

    def effect(self, trained, seat=None, start=None):
        seat = seat or self.seat
        if self.K:
            return self.effect_sampled(trained, seat, start)
        now, was, k, temp = seat.status, self.status0, self.a.eval_k, self.a.eval_temperature
        fresh = lambda i: now[i].get("ver", seat.name) == seat.name
        given, mine = [i for i in now if self.labelled(i) and fresh(i)], [i for i in now if not self.labelled(i) and fresh(i)]
        both, lines = bool(given) and bool(mine), []
        if given:
            g = " given" if both else ""
            tr = [i for i in trained if self.labelled(i)]
            rest = [i for i in given if i not in set(tr)]
            head = f"What {seat.name} does on the pool, against the untrained student (one most likely answer per problem):"
            if tr or not both:
                fixed = [i for i in tr if now[i]["correct"]]
                still = [i for i in tr if not now[i]["correct"]]
                chk = self.sampled([(i, {"idx": i}, self.rows[i]["answer"]) for i in tr[:80]], k, temp, seat)
                often = ", ".join(f"{i}: {round(c * k)}/{k}" for i, c in chk)
                held = sum(c for _, c in chk) / max(1, len(chk))
                often += f". Over all trained problems, {held:.0%} of these sampled attempts were right (how well the corrections held)"
                lines.append(f"- the {len(tr)}{g} problems it was trained on: {len(fixed)} it now answers correctly by itself {fixed[:40]}, "
                             f"{len(still)} still wrong {still[:40]}. Sampled {k} times each, it solves them (problem: right/{k}): {often}")
            c = self.pool_change(now, self.pool_marks({i: was[i] for i in rest}), rest)
            lines.append(f"- the {'other ' if tr or not both else ''}{len(rest)}{g} problems: {len(c['up'])} newly right {c['up'][:25]}, "
                         f"{len(c['down'])} newly wrong {c['down'][:25]} (net {c['change']:+d}, give or take {c['se']:.0f})")
            ref = self.versions.get(start, {}).get("pool") if start and start != "v0" else None
            if ref:
                c = self.pool_change(now, ref, rest)
                lines.append(f"- the same {c['n']} problems against {start}, which it was trained from: {len(c['up'])} right now and wrong under {start} "
                             f"{c['up'][:25]}, {len(c['down'])} wrong now and right under {start} {c['down'][:25]} (net {c['change']:+d}, give or take {c['se']:.0f})")
        else:
            head = (f"What {seat.name} does on the pool, against the student that first answered each problem (one most likely answer per "
                    f"problem; nothing here is labelled right or wrong):")
        if mine:
            m = " you posed" if both else ""
            tr = [i for i in trained if not self.labelled(i)]
            rest = [i for i in mine if i not in set(tr)]
            if tr or not both:
                chk = self.finals([(i, {"idx": i}) for i in tr[:80]], k, temp, seat)
                ends = "; ".join(f"{i}: {self.fin(was[i])} → {self.fin(now[i])}, sampled {', '.join(repr(x) for x in f)}" for i, f in chk)
                lines.append(f"- the {len(tr)} problems{m}{' that' if both else ''} it was trained on, the final answer of its own most likely answer "
                             f"{'when you posed it' if both else 'before'} → now, and of {k} sampled answers: {ends}")
            moved = [i for i in rest if now[i]["final"] != was[i].get("final")]
            lines.append(f"- the {'other ' if tr or not both else ''}{len(rest)} problems{m}: {len(moved)} now end with a different final answer {moved[:40]}")
        grp = given or mine
        longer = sum(now[i]["tokens"] for i in grp) / max(1, sum(was[i]["tokens"] for i in grp))
        cut = sum(now[i]["ended"] != "eos" for i in grp)
        cut0 = sum(was[i]["ended"] != "eos" for i in grp)
        lines.append((f"- its answers{' to the given problems' if both else ''} are {longer:.2f}x as long as the untrained student's; {cut} run to the "
                      f"length limit (untrained: {cut0})." if given else
                      f"- its answers are {longer:.2f}x as long as before; {cut} run to the length limit (before: {cut0}).")
                     + " Open any of these problems to see what changed.")
        return head + "\n" + "\n".join(lines)

    def compact(self, msgs):
        total = sum(len(m["content"]) for m in msgs)
        if total - getattr(self, "cut_floor", 0) < self.a.context_chars:
            return
        note = "\n[... the rest of this result was dropped to keep the conversation short; list, open or look again if you need it]"
        lists = [k for k, m in enumerate(msgs) if m["role"] == "user" and "problems match (showing" in m["content"]]
        for k, m in enumerate(msgs[2:-8], 2):
            if m["role"] == "user" and len(m["content"]) > 700 and not m["content"].endswith(note) and not (lists and k == lists[-1]):
                m["content"] = m["content"][:400] + note
            elif m["role"] == "assistant" and len(m["content"]) > 6000:
                m["content"] = m["content"][:6000] + " [...]"
        self.cut_floor = sum(len(m["content"]) for m in msgs)

    PARALLEL = ("open", "look", "fork", "continue", "weight", "undo", "discard", "keep")

    def seat_of(self, i):
        if not self.solo:
            return self._seat
        s = self.seats[i % self.flows]
        return s if s.name == self._seat.name and s.srv is not None and not s.student.down() else self._seat

    def call(self, path, **body):
        if "idx" not in body:
            return self.ask(self.srv, path, **body)
        i = int(body["idx"])
        if self.solo and getattr(self.tl, "seat", None) is None:
            self.tl.seat = self.seat_of(i)
            try:
                self.here(i)
                return self.ask(self.srv, path, **body)
            finally:
                del self.tl.seat
        self.here(i)
        return self.ask(self.srv, path, **body)

    @staticmethod
    def ask(srv, path, **body):
        try:
            return srv(path, **body)
        except RuntimeError as e:
            if "HTTP 400" not in str(e):
                raise
            m = re.search(r"error['\"]: ?['\"](.*?)['\"]\}", str(e))
            raise ValueError(m.group(1) if m else str(e)[-300:]) from None

    def idx_of(self, act):
        i = int(act["idx"])
        if not self.rows:
            raise ValueError("the pool is empty: pose a problem first")
        if not 0 <= i < len(self.rows):
            raise ValueError(f"idx must be a pool index 0..{len(self.rows) - 1}")
        if not self.mine(i):
            raise ValueError(f"problem {i} is conversation {self.owner_of(i)}'s to work on; yours are the pool problems whose idx leaves "
                             f"{self.w} when divided by {self.flows}" + (", and those of conversations that have stopped" if self.w == 0 else ""))
        return i

    def here(self, i):
        have = getattr(self.seat, "have", None)
        if self.flows > 1 and have is not None and i not in have and 0 <= i < len(self.rows):
            have.add(i)
            st = self.read_answer(i, self.seat)[0]
            st["ver"] = self.seat.name
            self.status[i] = st

    def show(self, d, start=0, stop=None):
        toks = d["tokens"]
        stop = len(toks) if stop is None else min(len(toks), stop)
        mb = getattr(self.a, "mark_below", 0.95)
        hot = sorted(sorted((i for i, t in enumerate(toks) if start <= i < stop and bits(t) >= self.a.tau), key=lambda i: -bits(toks[i]))[:self.a.branch])
        text = ("[...]" if start else "") + "".join((f"⟦{i}|{t['prob']:.0%}⟧" if t["prob"] < mb else "") + t["text"]
                                                    for i, t in enumerate(toks) if start <= i < stop) + ("[...]" if stop < len(toks) else "")
        table = "\n".join(f"{i} | H={bits(toks[i]):.2f} bits | now {vis(toks[i]['text'])!r} {toks[i]['prob']:.1%} | "
                          + "  ".join(f"{k}:{vis(c['text'])!r} {c['prob']:.1%}" for k, c in enumerate(toks[i]["top"][:self.a.show]))
                          for i in hot) or "(none above the threshold)"
        wt = lambda p: "" if (p.get("up", 1), p.get("down", 0)) == (1, 0) else f" (up {p.get('up', 1)}, down {p.get('down', 0)})"
        picks = ", ".join(f"@{p['pos']} {vis(p['original_text'])!r}→{vis(d['tokens'][p['pos']]['text'])!r}{wt(p)}" for p in d["picks"]) or "none"
        state = {"eos": "complete", "max_tokens": "cut off at the length limit", "paused": "PAUSED (unfinished: continue it, or fork its last token)"}
        last = ""
        if d["ended_by"] == "paused" and d["tokens"]:
            t = d["tokens"][-1]
            last = (f"\nThe last token, position {len(d['tokens']) - 1}, is {vis(t['text'])!r}; the student's choices there were: "
                    + "  ".join(f"{k}:{vis(c['text'])!r} {c['prob']:.1%}" for k, c in enumerate(t["top"][:self.a.show])))
        odds = ""
        if d["picks"]:
            first, forked = min(p["pos"] for p in d["picks"]), {p["pos"] for p in d["picks"]}
            low = sorted(((t["prob"], i) for i, t in enumerate(d["tokens"]) if i > first and i not in forked and t["prob"] < 0.5), key=lambda x: x[0])
            marks = {int(k) for k in (d.get("marks") or {})}
            path = sum(math.log(max(t["prob"], 1e-9)) for i, t in enumerate(d["tokens"]) if i > first and i not in forked)
            odds = (f"\nAfter the first fork the student wrote {len(low)} tokens it gave less than 50% ({sum(p < 0.1 for p, _ in low)} under 10%); "
                    f"left to itself it would write exactly this continuation about {math.exp(path):.1e} of the time. The least likely "
                    f"(pos token probability{', * = weighted' if marks else ''}): "
                    + "  ".join(f"{i} {vis(d['tokens'][i]['text'])!r} {p:.0%}{'*' if i in marks else ''}" for p, i in low[:30])
                    + ("\nThese are where this continuation got lucky. Weighting them up trains the student to take this path, not only its first step."
                       if low else ""))
        return (f"{len(d['tokens'])} tokens, {state.get(d['ended_by'], d['ended_by'])}. Forks in force: {picks}. "
                f"Token weights: {d.get('marks') or 'none'}.\n"
                f"Each token the student gave less than {mb:.0%} is preceded by ⟦position|its probability⟧; unmarked tokens had {mb:.0%} or more.\n"
                f"<answer>\n{text}\n</answer>\n"
                f"Undecided positions (pos | entropy | the student's token | its alternatives, numbered for \"choice\"):\n{table}{last}{odds}")

    def do_open(self, act):
        i = self.idx_of(act)
        r = self.call("open", idx=i)
        d, s = r["draft"], self.status[i]
        cost = self.charge("gen", self.a.call_cost + r["generated"], "writing this answer afresh") + "\n" if r.get("generated") else ""
        if self.K:
            s0 = self.status0[i]
            if self.labelled(i):
                said = (f"{self.live} got it right in {s['right']} of {s['k']} sampled answers"
                        + ("" if self.live == "v0" else f" (the untrained student: {s0['right']} of {s0['k']})")
                        + (". None was wrong: the one you work on is the first of them, a RIGHT answer" if s["shown"]
                           else ". The one you work on is the first of the WRONG ones"))
            else:
                said = (f"{self.live}'s {s['k']} sampled answers to it end with the final answers {self.fins(s)} "
                        f"(when you posed it: {self.fins(s0)}); the one you work on is the first")
            return (f"{cost}Problem {i} ({self.rows[i]['id']}, {self.rows[i].get('level', '')}); {said}.\n{self.rows[i]['prompt']}\n"
                    f"{self.ref(i)}\nThe answer as it now stands:\n{self.show(d)}")
        if self.labelled(i):
            verdict = (f"is {'right' if s['correct'] else 'WRONG'}"
                       + ("" if self.live == "v0" else " (the untrained student: " + ("right" if self.status0[i]["correct"] else "wrong") + ")"))
        else:
            verdict = f"ends with the final answer {self.fin(s)} (when you posed it: {self.fin(self.status0[i])})"
        return (f"{cost}Problem {i} ({self.rows[i]['id']}, {self.rows[i].get('level', '')}); {self.live}'s own answer to it "
                f"{verdict}.\n{self.rows[i]['prompt']}\n{self.ref(i)}\nThe answer as it now stands:\n{self.show(d)}")

    def ref(self, i):
        if not getattr(self.a, "key", False) or not self.labelled(i):
            return ""
        return f"Reference final answer: {self.rows[i]['answer']}\n"

    def do_look(self, act):
        i = self.idx_of(act)
        d = self.call("open", idx=i)["draft"]
        lo = max(0, int(act.get("from", 0)))
        hi = min(len(d["tokens"]), int(act.get("to", lo + 20)) + 1, lo + 60)
        rows = []
        for p in range(lo, hi):
            t = d["tokens"][p]
            rows.append(f"{p} | {vis(t['text'])!r} {t['prob']:.1%} | H={bits(t):.2f} | "
                        + "  ".join(f"{k}:{vis(c['text'])!r} {c['prob']:.1%}" for k, c in enumerate(t["top"][:12])))
        return f"Problem {i}, tokens {lo}-{hi - 1} (pos | token and its probability | entropy in bits | the student's top candidates):\n" + "\n".join(rows)

    def most_new(self):
        m = re.search(r"(?m)^max_new_tokens = (\d+)", open(self.a.config).read())
        return int(m.group(1)) if m else 4096

    def hold_gen(self, units, what):
        with self.mu:
            have = self.left["gen"] - self.reserved.get("gen", 0)
            if have < units:
                raise ValueError(f"{what} may use up to {int(units):,} generation units and {max(0, have):,} are left; "
                                 'a number for "write" (and fewer "tries") makes it smaller')
            self.reserved["gen"] = self.reserved.get("gen", 0) + units
        return units

    def release_gen(self, units):
        with self.mu:
            self.reserved["gen"] = self.reserved.get("gen", 0) - units

    def outcome(self, i, d):
        from math_eval import last_boxed
        if d["ended_by"] == "paused":
            return "\nThe answer is unfinished, so it has no final answer yet."
        final = last_boxed(answer_text(d))
        if final is None:
            return "\nThe answer ends with no boxed final answer."
        out = f"\nIts final answer now: {final!r}"
        if getattr(self.a, "key", False) and self.labelled(i):
            out += ", which matches the reference." if self.grader(self.rows[i]["answer"], answer_text(d)) else ", which does NOT match the reference."
        return out + ("" if out.endswith(".") else ".")

    def do_fork(self, act):
        i = self.idx_of(act)
        d = self.call("open", idx=i)["draft"]
        pos, tok = resolve(d, act)
        write = act.get("write", "all")
        n = None if write == "all" else max(0, int(write))
        tries = 1 if self.a.no_tries or n is not None else max(1, min(6, int(act.get("tries", 1))))
        held = self.hold_gen(tries * (self.a.call_cost + (n if n is not None else self.most_new())), "this fork")
        try:
            return self.fork_held(act, i, d, pos, tok, n, tries)
        finally:
            self.release_gen(held)

    def fork_held(self, act, i, d, pos, tok, n, tries):
        temp = float(act.get("temperature", self.a.gen_temperature))
        r = self.call("pick", idx=i, pos=pos, token=tok, up=int(act.get("up", 1)), down=int(act.get("down", 0)), temperature=temp,
                      **({} if n is None else {"max_new": n}))
        old = d["tokens"][pos]
        prob = next(c["prob"] for c in old["top"] if c["token"] == tok)
        note = (f"The token you put in at {pos} ({pos / max(1, len(d['tokens'])):.0%} of the way through the answer) had probability {prob:.1%} "
                f"there; the student's own choice {vis(old['text'])!r} had {old['prob']:.1%}."
                + (" That is a token the student all but ruled out: it will take many training steps to accept it." if prob < 0.02 else ""))
        if r["generated"]:
            note += f" What follows the fork was sampled at temperature {temp:g}: forking the same token again gives another continuation."
        extra = 0
        if tries > 1 and n is None:
            from math_eval import last_boxed
            more = self.call("sample", idx=i, draft=i, upto=pos, k=tries - 1, temperature=temp, seed=random.randrange(1, 10 ** 6))
            extra = sum(self.a.call_cost + a["tokens"] for a in more["answers"])
            texts = [answer_text(r["draft"])] + [a["text"] for a in more["answers"]]
            finals = [last_boxed(t) for t in texts]
            note += (f"\nThe same fork continued {tries} times ended with these final answers (the first is the answer shown below): "
                     + ", ".join(repr(x) for x in finals) + ".")
            if getattr(self.a, "key", False) and self.labelled(i):
                note += f" {sum(bool(self.grader(self.rows[i]['answer'], t)) for t in texts)} of {tries} match the reference."
        return (self.charge("gen", self.a.call_cost + r["generated"] + extra, f"the fork and {r['generated']} tokens written"
                            + (f" and {tries - 1} more continuations" if extra else "")) + "\n" + note
                + f"\nProblem {i}, from the fork at {pos} on:\n{self.show(r['draft'], max(0, pos - 12))}" + self.outcome(i, r["draft"]))

    def do_continue(self, act):
        i = self.idx_of(act)
        write = act.get("write", "all")
        n = None if write == "all" else max(1, int(write))
        before = len(self.call("open", idx=i)["draft"]["tokens"])
        held = self.hold_gen(self.a.call_cost + (n or self.most_new()), "writing on")
        try:
            r = self.call("continue", idx=i, temperature=float(act.get("temperature", self.a.gen_temperature)), **({} if n is None else {"max_new": n}))
            return (self.charge("gen", self.a.call_cost + r["generated"], f"{r['generated']} tokens written")
                    + f"\nProblem {i}, from token {before} on:\n{self.show(r['draft'], max(0, before - 12))}" + self.outcome(i, r["draft"]))
        finally:
            self.release_gen(held)

    def do_weight(self, act):
        i = self.idx_of(act)
        marks = list(act.get("marks") or [])
        if "from" in act and "below" not in act:
            marks += [{"pos": p, "w": act["w"]} for p in range(int(act["from"]), int(act.get("to", act["from"])) + 1)]
        if "below" in act:
            d0 = self.call("open", idx=i)["draft"]
            if not d0["picks"]:
                raise ValueError('"below" weights the unlikely tokens after a fork; this answer has no fork')
            first, forked = min(p["pos"] for p in d0["picks"]), {p["pos"] for p in d0["picks"]}
            lo, hi = max(first + 1, int(act.get("from", 0))), int(act.get("to", len(d0["tokens"])))
            marks += [{"pos": q, "w": act.get("w", 1)} for q, t in enumerate(d0["tokens"])
                      if lo <= q <= hi and q not in forked and t["prob"] < float(act["below"])]
            if not marks:
                return f"Problem {i}: no token after the fork has probability under {float(act['below']):.0%}; nothing weighted."
        if not marks:
            raise ValueError('give "marks": [{"pos": p, "w": w}, ...], or "from", "to", "w", or "below": probability')
        if len(marks) > 2000:
            raise ValueError("at most 2000 tokens per weight action")
        d0 = self.call("open", idx=i)["draft"]
        forked, n = {p["pos"] for p in d0["picks"]}, len(d0["tokens"])
        skipped = []
        if "from" in act and "below" not in act:
            skipped = [m["pos"] for m in marks if m["pos"] in forked or not 0 <= m["pos"] < n]
            marks = [m for m in marks if m["pos"] not in forked and 0 <= m["pos"] < n]
        bad = [m["pos"] for m in marks if int(m["pos"]) in forked or not 0 <= int(m["pos"]) < n]
        if bad:
            raise ValueError(f"nothing was weighted: positions {bad} are forks (set a fork's weights on the fork: up, down) "
                             f"or outside the answer (0..{n - 1})")
        if any(not str(m["w"]).lstrip("-").isdigit() for m in marks):
            raise ValueError("nothing was weighted: w must be a whole number (positive trains the token up, negative trains it down, 0 removes the weight)")
        f = getattr(self.a, "min_prob", 0.0)
        low = [m for m in marks if int(m["w"]) > 0 and d0["tokens"][int(m["pos"])]["prob"] < f]
        if low:
            raise ValueError("nothing was weighted: these tokens are under the probability floor of " + f"{f:g}" + ": "
                             + ", ".join(f"{int(m['pos'])} ({d0['tokens'][int(m['pos'])]['prob']:.2g})" for m in low))
        d = d0
        for m in marks:
            d = self.call("weight", idx=i, pos=int(m["pos"]), w=int(m["w"]))["draft"]
        note = f" Left out: {skipped} (forks, or past the answer's end)." if skipped else ""
        return f"Problem {i}: token weights now {d.get('marks') or 'none'} (position: weight; + trains the token up, - trains it down).{note}"

    def do_undo(self, act):
        i = self.idx_of(act)
        d = self.call("unpick", idx=i, pos=int(act["pos"]))["draft"]
        return f"Problem {i}: the fork at {act['pos']} is undone.\n{self.show(d)}"

    def do_discard(self, act):
        i = self.idx_of(act)
        self.call("drop", idx=i)
        return (f"Problem {i}: forks and weights discarded (opening it again shows the same sampled answer as before, unchanged)." if self.K else
                f"Problem {i}: forks and weights discarded (opening it again writes the student's own answer afresh).")

    def do_keep(self, act):
        i = self.idx_of(act)
        d = self.call("open", idx=i)["draft"]
        if not any(p.get("up", 1) > 0 or p.get("down", 0) > 0 for p in d["picks"]) and not d.get("marks"):
            raise ValueError(f"problem {i}'s answer has nothing in it to train: no fork with \"up\" or \"down\" above 0 and no weighted token; "
                             "weight tokens of it, or give a fork a weight, first")
        right = self.grader(self.rows[i]["answer"], answer_text(d)) if self.labelled(i) else None
        ep = self.call("commit", idx=i, meta={"correct": right, "editor": self.a.model, "forks": len(d["picks"]), "marks": len(d.get("marks") or {})})
        n = max(1, len(d["tokens"]))
        forks = "; ".join(f"@{p['pos']} ({p['pos'] / n:.0%} in)" for p in d["picks"])
        self.call("drop", idx=i)
        new = [k for k in self.kept() if k["key"] not in set(self.versions[self.live]["included"])]
        if self.flows > 1 and self.w:
            self.post(f"[Conversation {self.w} kept a correction on problem {i} ({len(d['picks'])} forks, {len(d.get('marks') or {})} weighted tokens). "
                      f"{len(new)} kept corrections are not yet trained into {self.live}.]", to=0)
        return (f"Problem {i} kept as a correction ({len(d['picks'])} forks{': ' + forks if forks else ''}; {len(d.get('marks') or {})} weighted tokens). "
                f"{len(new)} kept corrections are not yet trained into {self.live}.")

    def train_cfg(self, act):
        opt, hold = act.get("optimizer", getattr(self.a, "default_optimizer", "sgd")), act.get("hold", "end")
        if opt not in ("adam", "sgd"):
            raise ValueError("optimizer must be adam or sgd")
        if hold not in ("end", "rest", "none"):
            raise ValueError("hold must be end, rest or none")
        until = float(act.get("until", 0.9))
        if not 0.05 <= until <= 0.999:
            raise ValueError("until must be a probability between 0.05 and 0.999")
        mom = float(act.get("momentum", 0.0))
        if not 0.0 <= mom <= 0.95:
            raise ValueError("momentum must be between 0 and 0.95")
        if mom and opt != "sgd":
            raise ValueError('"momentum" is for optimizer "sgd"; adam has its own (0.9)')
        emb = act.get("embeddings", "freeze" if opt == "sgd" else "train")
        if emb not in ("train", "freeze"):
            raise ValueError('"embeddings" must be "train" or "freeze"')
        return {"optimizer": opt, "hold": hold, "until_taken": bool(act.get("_auto")), "until": until, "momentum": mom, "embeddings": emb}

    def train_args(self, cfg):
        out = (["--optimizer", "sgdm", "--momentum", str(cfg.get("momentum", 0.0)), "--max-grad-norm", "0", "--match-sign", str(cfg["lr"])]
               if cfg["optimizer"] == "sgd" else [])
        if cfg.get("embeddings") == "freeze":
            out += ["--freeze", "embed_tokens,lm_head"]
        return out + (["--keep-weight", "1", "--keep", cfg["hold"]] if cfg["hold"] != "none" else []) + (["--until-taken", "--taken-p", str(cfg["until"])] if cfg["until_taken"] else [])

    def pool_asked(self, want):
        if want is None or want == "all":
            return want
        if not str(want).isdigit():
            raise ValueError('"pool" is "all" or a number of problems')
        return int(want)

    @staticmethod
    def targets(line):
        e = json.loads(line)["episode"]
        return sum((f.get("up", 1) > 0) + (f.get("down", 0) > 0) for f in e["forks"]) + sum(1 for m in e.get("marks", []) if m["w"])

    def plan_run(self, act, name):
        num = lambda v, lo, hi: min(hi, max(lo, float(v)))
        lr = act.get("lr", 2e-7)
        if isinstance(lr, list):
            if len(lr) != 1:
                raise ValueError("one learning rate per training run; to compare two, train twice from the same start and validate both")
            lr = lr[0]
        lr = num(lr, 1e-9, 1e-3)
        steps = act.get("steps", "auto")
        if isinstance(steps, list):
            if len(steps) != 1:
                raise ValueError("one step setting per training run")
            steps = steps[0]
        auto = steps == "auto"
        n_steps = self.a.auto_cap if auto else int(num(steps, 1, 200))
        cfg = {"lr": lr, "passes": n_steps, "pull": num(act.get("pull", 0.0), 0.0, 0.5),
               "down_weight": num(act.get("down_weight", 1.0), 0.0, 100.0), "start": act.get("start", "current"),
               **self.train_cfg(dict(act, _auto=auto))}
        start = self.live if cfg["start"] == "current" else "v0" if cfg["start"] == "base" else cfg["start"]
        if start not in self.versions:
            raise ValueError(f"start must be current, base or one of {sorted(self.versions)}")
        if self.versions[start].get("pruned"):
            raise ValueError(f"{start}'s weights were discarded (only the live version, the best validated one and v0 are kept)")
        have = set(self.versions[start]["included"])
        cfg["data"] = act.get("data", "new" if have else "all")
        if cfg["data"] == "all" and have:
            raise ValueError(f"{start} has already been trained on {len(have)} of the kept corrections, and a run does not train a correction twice on the "
                             "same version. Use data \"new\" to stack the others on it, or start \"base\" with data \"all\" to train every one from the untrained student")
        by_id = {r["id"]: n for n, r in enumerate(self.rows)}
        only = {int(x) for x in act["only"]} if act.get("only") else None
        skip = {int(x) for x in act.get("exclude", [])}
        kept = self.kept()
        eps = [k for k in kept if k["key"] not in have
               and (only is None or by_id[k["id"]] in only) and by_id[k["id"]] not in skip]
        if not eps:
            raise ValueError(f"no kept corrections to train (nothing new since {start}, or the selection is empty)")
        ups = sum(sum(1 for f in json.loads(k["line"])["episode"]["forks"] if f.get("up", 1) > 0)
                  + sum(1 for m in json.loads(k["line"])["episode"].get("marks", []) if m["w"] > 0) for k in eps)
        if auto and not ups:
            raise ValueError('"steps": "auto" stops when the trained-up tokens pass "until", and these corrections train nothing up '
                             "(only tokens weighted down): give \"steps\" a number")
        toks = sum(len(json.loads(k["line"])["episode"]["prompt_tokens"]) + len(json.loads(k["line"])["episode"]["completion"]) for k in eps)
        had = self.versions[start].get("grads", 0)
        grads, cap = had + sum(self.targets(k["line"]) for k in eps), getattr(self.a, "grad_budget", 0)
        if cap and grads > cap:
            raise ValueError(f"this run would make a version with {grads} trained tokens ({had} already in {start}, {grads - had} in the {len(eps)} "
                             f"corrections to train) and a version may have at most {cap}. Train fewer of them (\"only\", \"exclude\"), take "
                             "weights off tokens, or start from a version that has fewer")
        included = have | {k["key"] for k in eps}
        return {"name": name, "start": start, "start_dir": self.versions[start]["dir"], "cfg": cfg, "lr": lr, "n_steps": n_steps, "auto": auto,
                "pool_want": self.pool_asked(act.get("pool")), "eps": eps, "toks": toks, "grads": grads, "included": sorted(included), "validate": self.val_k_asked(act.get("validate")), "rows": len(self.rows),
                "trained": sorted({by_id[k["id"]] for k in kept if k["key"] in included})}

    def do_train(self, act):
        if self.job:
            raise ValueError(f"{self.making()} still being made; " + 'wait ({"action": "wait"}) before another training run')
        many = "runs" in act
        specs = [act]
        if many:
            if not self.parallel:
                raise ValueError('"runs" needs more than one GPU; this session trains one run at a time')
            runs = act["runs"]
            if not isinstance(runs, list) or not 1 <= len(runs) <= self.parallel or not all(isinstance(r, dict) for r in runs):
                raise ValueError(f'"runs" must be a list of 1 to {self.parallel} runs, each with its own train settings')
            shared = {k: v for k, v in act.items() if k not in ("runs", "background")}
            specs = [{**shared, **r} for r in runs]
        plans = []
        for n, spec in enumerate(specs):
            try:
                plans.append(self.plan_run(spec, f"v{self.vnext + n}"))
            except ValueError as e:
                raise ValueError(f"run {n + 1} of {len(specs)}: {e}" if many else str(e)) from None
        units = sum(3 * p["toks"] * p["n_steps"] for p in plans)
        have = self.left["tr"] - self.reserved.get("tr", 0)
        if units > have:
            fits = max(0, have) // (3 * sum(p["toks"] for p in plans))
            raise ValueError(f"this training may take up to {units:,} training units ({3 * sum(p['toks'] for p in plans):,} a step"
                             f"{' over all runs' if many else ''}) and {max(0, have):,} are left: at most {fits} steps fit"
                             + (', so give "steps" a number' if any(p["auto"] for p in plans) and fits else ""))
        free = shutil.disk_usage(self.vroot).free / 1e9
        room = self.a.min_free_gb * (2 + len(plans)) / 3
        if free < room:
            raise RuntimeError(f"only {free:.0f} GB free on the run's disk; a training run needs {room:.0f} GB")
        looks = sum(p["validate"] for p in plans) * len(self.val_rows)
        if looks:
            self.need("val", looks, "the validation scores" if many else "its validation score")
        if many and self.solo:
            if act.get("background"):
                raise ValueError('"background" is not available here: the runs are made side by side while you wait')
            free = [s for s in self.seats if s is not self._seat]
            if len(plans) > len(free):
                seats = [free[j % len(free)] for j in range(len(plans))]
                for p in plans:
                    p["cand"], p["extra"], p["turn"] = True, [], True
            else:
                per = max(1, len(free) // len(plans))
                seats = free[:len(plans) * per:per]
                for j, p in enumerate(plans):
                    p["cand"], p["extra"] = True, free[j * per + 1:(j + 1) * per]
        else:
            seats = self.spare(len(plans))
        if len(seats) < len(plans):
            raise RuntimeError(f"{len(plans)} training runs need {len(plans)} free GPUs and {len(seats)} are free")
        cap = getattr(self.a, "max_trainings", 0)
        if cap and self.vnext - 1 + len(plans) > cap:
            raise ValueError(f"a session can make {cap} versions and {self.vnext - 1} are made: {max(0, cap - (self.vnext - 1))} training runs are left")
        self.vnext += len(plans)
        self.pinned, self.reserved = {p["start"] for p in plans}, {"tr": units, "val": looks}
        for p, seat in zip(plans, seats):
            p["seat"], seat.busy = seat, True
            if seat is not self.seat:
                seat.clear()
            for s in p.get("extra", []):
                s.busy = True
                s.clear()
        queues = {}
        for p in plans:
            queues.setdefault(id(p["seat"]), []).append(p)

        def chain(ps):
            for p in ps:
                self.make(p)
        job = {"plans": plans, "many": many, "live": self.live,
               "threads": [threading.Thread(target=chain, args=(ps,), daemon=True) for ps in queues.values()]}
        for t in job["threads"]:
            t.start()
        if many and act.get("background"):
            self.job, self.flight = job, [p["name"] for p in plans]
            return (f"Started in the background: {', '.join(self.flight)}. {self.live} stays the live student and you can go on working on it. "
                    'Their reports come with the result of your first action after they are done, or with {"action": "wait"}.')
        for t in job["threads"]:
            t.join()
        return self.take(job)

    def make(self, p):
        try:
            seat, name, cfg, n_steps = p["seat"], p["name"], p["cfg"], p["n_steps"]
            work = os.path.join(self.vroot, f"{name}-train")
            final = os.path.join(self.vroot, name)
            for d in (work, final):
                shutil.rmtree(d, ignore_errors=True)
            os.makedirs(work, exist_ok=True)
            batch = os.path.join(work, "batch.jsonl")
            open(batch, "w").writelines(k["line"] for k in p["eps"])
            p["prev"] = dict(seat.status)
            seat.student.stop()
            if p.get("cand"):
                seat.name, seat.srv = None, None
            env = getattr(seat.student, "env", self.a.env)
            wide = getattr(self.a, "train_gpus", 1) if self.solo and not p.get("cand") else 1
            if wide > 1:
                group = [seat] + [s for s in self.seats if s is not seat][:wide - 1]
                for s in group[1:]:
                    s.student.stop()
                env = dict(env, CUDA_VISIBLE_DEVICES=",".join(str(s.student.gpu) for s in group))
            t0 = time.time()
            with open(os.path.join(work, "train.log"), "w") as err:
                r = subprocess.run([sys.executable, os.path.join(session_base.HERE, "train_hf.py"), "--model", p["start_dir"],
                                    "--episodes", batch, "--out", work, "--lr", str(p["lr"]), "--stages", str(n_steps), "--pull", str(cfg["pull"]),
                                    "--down-weight", str(cfg["down_weight"]), "--forward", "bf16" if self.a.dtype == "bfloat16" else "f32", "--measure",
                                    *(["--devices", str(wide)] if wide > 1 else []),
                                    *self.train_args(cfg)], stdout=subprocess.DEVNULL, stderr=err, env=env)
            ck = os.path.join(work, f"ckpt-{n_steps:05d}")
            if getattr(r, "returncode", 0) != 0 or not os.path.exists(os.path.join(ck, "stats.json")):
                p["error"] = (open(os.path.join(work, "train.log")).read().strip().splitlines() or ["no output"])[-1][:200]
                shutil.rmtree(work, ignore_errors=True)
                return
            p["st"], p["secs"], p["dir"] = json.load(open(os.path.join(ck, "stats.json"))), round(time.time() - t0), final
            os.replace(ck, final)
            shutil.rmtree(work, ignore_errors=True)
            if self.flows > 1 and not p.get("cand"):
                self.serve_all(name, final, seat)
            else:
                seat.name, seat.srv = name, seat.student.start(final, int(name[1:]))
            if p.get("cand"):
                def also(s):
                    s.name, s.srv = name, s.student.start(final, int(name[1:]))
                    s.status, s.have = {}, set()
                ts = [threading.Thread(target=also, args=(s,), daemon=True) for s in p.get("extra", [])]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join()
                seat.status, seat.have = {}, set()
                p["effect"] = ((f"{name} is a candidate; its weights are kept" + ("." if getattr(self.a, "keep_versions", False) else " until your next training run.") if p.get("turn") else f"{name} is held on one GPU as a candidate.")
                               + " The pool is not answered by a candidate: \"use\" it and every GPU takes it and answers the pool.")
                p["pool"] = {}
            else:
                self.screen(seat, p["rows"], self.pool_ids(name, p["trained"], p.get("pool_want"), p["rows"]), p.get("prev"))
                p["effect"] = self.effect(p["trained"], seat, p["start"])
                p["pool"] = self.pool_marks(seat.status, name)
            if p["validate"]:
                mine = [seat] + [s for s in p.get("extra", []) if s.srv is not None and not s.student.down()]
                p["val"] = self.val_answers(seat, p["validate"], srvs=[s.srv for s in mine] if p.get("cand") else None)
        except Exception as e:
            p["crash"] = e

    def take(self, job):
        self.job, self.flight, self.reserved, self.pinned = None, [], {}, set()
        plans, many, told, back = job["plans"], job["many"], {}, ""
        for p in plans:
            seat, name = p["seat"], p["name"]
            seat.busy = False
            for s_ in p.get("extra", []):
                s_.busy = False
            if "crash" not in p and "error" not in p:
                st, cfg, eps = p["st"], p["cfg"], p["eps"]
                ran = int(st["steps"]) if st.get("steps") is not None else p["n_steps"]
                self.versions[name] = {"dir": p["dir"], "parent": p["start"], "included": p["included"],
                                       "settings": {**cfg, "passes": ran, "tried": {"lr": [p["lr"]], "passes": [p["n_steps"]]}},
                                       "grads": p["grads"], "episodes": len(eps), "stats": st, "train_secs": p["secs"], "pool": p["pool"]}
                self.archive(name)
                p["bill"] = self.charge("tr", 3 * p["toks"] * ran, f"training {len(eps)} corrections ({p['toks']:,} tokens) for {ran} steps")
                took = st.get("taken_after_steps")
                head = (f"Trained {name} from {p['start']} on {len(eps)} corrections ({st['forced_tokens']} trained-up tokens, {st['down_tokens']} tokens to push down) at "
                        f"lr {p['lr']:g}, optimizer {cfg['optimizer']}{', embeddings frozen' if cfg.get('embeddings') == 'freeze' else ''}, hold {cfg['hold']}, "
                        f"pull {cfg['pull']:g}: {ran} steps. The trained-up tokens' mean "
                        f"probability went from {math.exp(st['forced_logp_before']):.1%} to {math.exp(st['forced_logp_after']):.1%}.")
                if p["auto"]:
                    head += (f" Every trained-up token had already passed {cfg['until']:.0%}, so nothing was trained: {name} is {p['start']}."
                             if took == 0 else f" Training stopped by itself when every trained-up token had passed {cfg['until']:.0%}."
                             if took is not None else f" The trained-up tokens had NOT all passed {cfg['until']:.0%} after {p['n_steps']} steps, the most "
                                                      '"auto" takes.')
                if "moved_l2" in st:
                    g = st.get("grad_l2") or [0.0]
                    head += (f"\nThe weights ({st['n_params'] / 1e9:.2f} billion) ended {st['moved_l2']:.3g} from where they started (Euclidean distance): "
                             f"{st['moved_mean_abs']:.2g} a weight on average, {st['moved_max']:.2g} the most. The gradient's length was {g[0]:.3g} at the "
                             f"first step and {g[-1]:.3g} at the last.")
                if not many:
                    self.seat = seat
                    self.catch_up(seat)
                told[name] = head + ("" if many else f"\n{name} is now the live student.") + "\n" + p["effect"]
                continue
            was_live = seat is self.seat
            if not any(q is not p and q["seat"] is seat and seat.name == q["name"] for q in plans):
                seat.clear()
            if was_live:
                back = self.go_live(job["live"])
            if "crash" in p:
                print(f"HARNESS FAILURE making {name}: {p['crash']!r}", file=sys.stderr, flush=True)
        self.prune(keep=[p["name"] for p in plans if p.get("turn")])
        out = []
        for p in plans:
            name = p["name"]
            if "error" in p:
                if not many:
                    raise ValueError(f"the training run failed ({p['error']}). " + back)
                out.append(f"{name}: the training run failed ({p['error']}). Nothing was charged for it.")
            elif "crash" in p:
                if not many:
                    raise RuntimeError(f"{type(p['crash']).__name__}: {p['crash']}")
                out.append(f"{name}: making it failed inside the harness ({type(p['crash']).__name__}: {str(p['crash'])[:300]}). Nothing was charged for it.")
            else:
                out.append(told[name] + ("\n" + self.val_record(name, p["val"]) if p["validate"] else "") + ("\n" + p["bill"] if p["bill"] else ""))
        if not many:
            return out[0]
        made = [p["name"] for p in plans if p["name"] in told]
        return (f"Candidates made: {', '.join(made) or 'none'}. {self.live} is still the live student"
                + ('; "use" a candidate to go on from it.' if made else ".")
                + (f' The runs took turns on the other GPUs, which hold the last ones made; {{"action": "use", "version": "{self.live}"}} puts {self.live} back on every GPU.'
                   if made and self.solo and any(p.get("turn") for p in plans) else
                   f' The other GPUs hold the candidates until then; {{"action": "use", "version": "{self.live}"}} puts {self.live} back on every GPU.'
                   if made and self.solo else "") + "\n\n" + "\n\n".join(out))

    def making(self):
        return ", ".join(self.flight) + (" is" if len(self.flight) == 1 else " are")

    def news(self):
        if self.job and not any(t.is_alive() for t in self.job["threads"]):
            return "[Made in the background]\n" + self.take(self.job)
        return ""

    def do_wait(self, act=None):
        if not self.job:
            return "Nothing is being made."
        for t in self.job["threads"]:
            t.join()
        return f"{self.making()} done."

    def settle(self):
        if self.job:
            for t in self.job["threads"]:
                t.join()
            print(self.take(self.job)[:3000], file=sys.stderr, flush=True)

    def validate(self, name):
        f = os.path.join(self.a.run, "versions", f"{name}-val.jsonl")
        if not os.path.exists(f):
            with open(f, "w") as fh:
                fh.writelines(json.dumps({"id": r["id"], "correct": 0.0, "completion": ""}) + "\n" for r in self.val_rows)
        self.versions[name]["val"] = {}
        return {}

    def marks(self, jobs, k, temp, seat=None, first=0, srvs=None):
        own = (seat or self.seat).srv
        srvs = srvs or self.servers(seat)

        def one(nj):
            n, (name, q, answer) = nj
            srv, out = srvs[n % len(srvs)] if "prompt" in q else own, []
            for at in range(0, k, 16):
                r = self.ask(srv, "sample", k=min(16, k - at), temperature=temp, seed=first + at, **q)
                out += [bool(self.grader(answer, x["text"])) for x in r["answers"]]
            return name, out
        per = max(8, min(self.threads, 512 // min(16, max(1, k))))
        with ThreadPoolExecutor(per * len(srvs)) as ex:
            return list(ex.map(one, enumerate(jobs)))

    def sampled(self, jobs, k, temp, seat=None):
        return [(name, sum(m) / k) for name, m in self.marks(jobs, k, temp, seat)]

    def finals(self, jobs, k, temp, seat=None):
        from math_eval import last_boxed
        srv = (seat or self.seat).srv

        def one(job):
            name, q = job
            return name, [last_boxed(a["text"]) for a in self.ask(srv, "sample", k=k, temperature=temp, **q)["answers"]]
        with ThreadPoolExecutor(self.threads) as ex:
            return list(ex.map(one, jobs))

    def val_k(self, name):
        m = self.versions[name].get("marks")
        return len(next(iter(m.values()))) if m else 0

    def val_k_asked(self, k):
        if not k:
            return 0
        return self.a.eval_k if k is True else max(1, min(self.kmax, int(k)))

    def val_answers(self, seat=None, k=None, first=0, srvs=None):
        return self.marks([(r["id"], {"prompt": r["prompt"]}, r["answer"]) for r in self.val_rows], k or self.a.eval_k, self.a.eval_temperature, seat, first, srvs)

    def val_record(self, name, out, charge=True, first=0):
        v = self.versions[name]
        have = (v.get("marks") or {}) if first else {}
        v["marks"] = {i: have.get(i, "") + "".join("1" if x else "0" for x in m) for i, m in out}
        v["val"] = {i: m.count("1") / len(m) for i, m in v["marks"].items()}
        self.val_rewrite(name)
        new = sum(len(m) for _, m in out)
        bill = self.charge("val", new, "this validation score") if charge else ""
        return self.val_text(name) + ("\n" + bill if bill else "")

    def val_rewrite(self, name):
        v, f = self.versions[name], os.path.join(self.a.run, "versions", f"{name}-val.jsonl")
        if v.get("marks"):
            with open(f, "w") as fh:
                fh.writelines(json.dumps({"id": i, "correct": v["val"][i], "marks": m, "completion": ""}) + "\n" for i, m in v["marks"].items())
        elif os.path.exists(f):
            os.remove(f)

    def best_before(self, name):
        scored = [n for n, v in self.versions.items() if v.get("val") and int(n[1:]) < int(name[1:])]
        return max(scored, key=lambda n: (self.points(self.versions[n]["val"]), -int(n[1:]))) if scored else None

    def val_text(self, name):
        v, n = self.versions[name], len(self.counted)
        out = (f"Validation, {name}: {self.val_k(name) or self.a.eval_k} sampled answers to each of {n} problems at temperature "
               f"{self.a.eval_temperature:g}: expected solved {self.points(v['val']):.1f} of {n}.")
        others, par, best = [], v.get("parent"), self.best_before(name)
        if name != "v0" and self.versions["v0"].get("val"):
            others.append(("v0", "the untrained student"))
        if par and par != "v0" and self.versions[par].get("val"):
            others.append((par, "which it was trained from"))
        if best and best not in ("v0", par):
            others.append((best, "the best validated version before it"))
        return out + "".join(f"\n  {name} {self.versus(o, name, what)}." for o, what in others)

    def validate_live(self, charge=True, k=None, first=0):
        return self.val_record(self.live, self.val_answers(k=k, first=first), charge, first)

    def versus(self, a_, b_, what=""):
        A, B = self.versions[a_], self.versions[b_]
        ka, kb = self.val_k(a_), self.val_k(b_)
        kc = min(ka, kb)
        if kc:
            fa, fb = ({i: v["marks"][i][:kc].count("1") / kc for i in self.counted} for v in (A, B))
        else:
            fa, fb = A["val"], B["val"]
        d = [fb[i] - fa[i] for i in self.counted]
        n, mean = len(d), sum(d) / len(d)
        se = math.sqrt(sum((x - mean) ** 2 for x in d) / max(1, n - 1) * n)
        both = f" over the {kc} answers per problem both have" if kc and ka != kb else ""
        return (f"against {a_}{', ' + what if what else ''} ({sum(fa.values()):.1f}{both}): {sum(d):+.1f}, give or take {se:.1f} "
                f"({sum(x > 0 for x in d)} problems up, {sum(x < 0 for x in d)} down)")

    def do_validate(self, act):
        k = self.val_k_asked(act.get("k", True))
        have = self.val_k(self.live) if self.versions[self.live].get("val") else 0
        if have >= k:
            return f"{self.live} already has its validation score.\n" + self.val_text(self.live)
        self.need("val", (k - have) * len(self.val_rows), "this validation score")
        return self.validate_live(k=k - have, first=have)

    def do_compare(self, act):
        a_, b_ = act.get("a"), act.get("b")
        for x in (a_, b_):
            if x not in self.versions:
                raise ValueError(f'"a" and "b" must be two of {sorted(self.versions)}')
        if not (self.versions[a_].get("val") and self.versions[b_].get("val")):
            raise ValueError("both versions need a validation score (validate each while it is the live student)")
        out = f"On validation, {b_} {self.versus(a_, b_)}."
        pa, pb = self.versions[a_].get("pool"), self.versions[b_].get("pool")
        if pa and pb:
            ids = [i for i in pb if i in pa]
            d = [pb[i] - pa[i] for i in ids]
            mean = sum(d) / max(1, len(d))
            se = math.sqrt(sum((x - mean) ** 2 for x in d) / max(1, len(d) - 1) * len(d))
            unit = f"sampled answers right of {len(ids) * self.K}" if self.K else f"problems right of {len(ids)}"
            out += (f"\nOn the pool ({unit}; problems either was trained on are in it), {b_} against {a_} ({sum(pa[i] for i in ids)}): "
                    f"{sum(d):+d}, give or take {se:.0f} ({sum(x > 0 for x in d)} problems up, {sum(x < 0 for x in d)} down).")
        return out

    def score_dir(self, model_dir, out, gpu_mem=None):
        if not os.path.exists(out):
            return {}
        return {r["id"]: r["correct"] for r in map(json.loads, open(out))}

    def saved_state(self):
        return {"refused": getattr(self, "refused", 0), "refused_floor": getattr(self, "refused_floor", 0)}

    def restore_state(self, st):
        self.refused, self.refused_floor = st.get("refused", 0), st.get("refused_floor", 0)

    def tokens_left(self):
        if self.used_up():
            return None
        if self.time_cap:
            return (f"the session is not over: {hms(self.time_cap - self.elapsed())} of its time is left. A version is handed in in your last "
                    "reply, when the time is up. Keep working")
        if self.tok_cap and SPEND["tok"] < self.tok_cap:
            return ("the session is not over. It is ended from outside, at a point you are not told; you are then told so and have one "
                    "last reply in which to hand in a version. Keep working")
        return None

    def flow_stop_check(self):
        return self.tokens_left()

    def finish_check(self, version):
        if self.tokens_left():
            return self.tokens_left()
        if self.job:
            return f"{self.making()} still being made; " + 'wait first ({"action": "wait"})'
        kept, used = len(self.kept()), 1 - self.left["gen"] / self.total["gen"]
        self.refused = getattr(self, "refused", 0)
        floor = getattr(self.a, "min_kept", 0)
        if floor and kept < floor and used < 0.5 and getattr(self, "refused_floor", 0) < 5:
            self.refused_floor = getattr(self, "refused_floor", 0) + 1
            return (f"finish is accepted once {floor} corrections are kept or half of the generation budget is used; you have kept {kept} "
                    f"and used {used:.0%} of the generation budget. Keep working")
        if version == "v0" and kept < getattr(self.a, "min_corrections", 0) and used < 0.5 and self.refused < 2:
            self.refused += 1
            return (f"handing in the untrained student scores zero, and you have made {kept} corrections and used {used:.0%} of your generation "
                    f"budget. Keep working: finish is accepted once you have kept {self.a.min_corrections} corrections or used half of the "
                    f"generation budget")
        if not self.versions[version].get("val"):
            if self.live != version:
                self.go_live(version)
            self.validate_live(charge=self.left["val"] > 0)

    def best(self):
        scored = [n for n, v in self.versions.items() if v.get("val") and not v.get("late")]
        return max(scored, key=lambda n: (self.points(self.versions[n]["val"]), -int(n[1:])))

    def do_status(self, act=None):
        lines = []
        for n, v in self.versions.items():
            s = v["settings"]
            val = (f"validation {self.points(v['val']):.1f} of {len(self.counted)} at {self.val_k(n) or self.a.eval_k} answers a problem"
                   + (f" ({self.versus('v0', n)})" if s else "") if v.get("val") else "not validated")
            lines.append(f"{n}: {val}" + (f"; from {v['parent']} on {v['episodes']} corrections, lr {s['lr']:g} x {s['passes']} steps, hold {s.get('hold')}, "
                                          f"pull {s.get('pull', 0):g}" if s else " (untrained)")
                         + (" <- live" if n == self.live else " (loaded)" if any(x.name == n and not x.busy for x in self.seats) else "")
                         + (" (weights discarded)" if v.get("pruned") else ""))
        if self.flight:
            lines.append(f"Being made in the background: {', '.join(self.flight)}.")
        ks = self.kept()
        new = [k for k in ks if k["key"] not in set(self.versions[self.live]["included"])]
        if self.flows > 1 and not self.solo:
            lines.append("Conversations: " + "; ".join(f"{f['w']}{' (lead)' if f['w'] == 0 else ''} {'waiting' if f['state'] == 'wait' else 'stopped' if f['state'] == 'stopped' else 'working'}"
                                                       for f in self.fl) + ".")
        return "\n".join(lines) + f"\nCorrections kept: {len(ks)}; {len(new)} not yet trained into {self.live}." + ("" if self.time_cap else "\n" + self.budget())

    def do_eval(self, act):
        k = max(1, min(8, int(act.get("k", self.a.eval_k))))
        temp = float(act.get("temperature", self.a.eval_temperature))
        if act.get("problems"):
            idxs = list(dict.fromkeys(int(x) for x in act["problems"]))[:60]
            if any(not 0 <= i < len(self.rows) for i in idxs):
                raise ValueError(f"problems must be pool indices 0..{len(self.rows) - 1}")
            self.need("chk", len(idxs), "checking these problems")
            given, mine, out = [i for i in idxs if self.labelled(i)], [i for i in idxs if not self.labelled(i)], []
            if given:
                got = [(i, round(c * k)) for i, c in self.sampled([(i, {"idx": i}, self.rows[i]["answer"]) for i in given], k, temp)]
                solved = sum(c for _, c in got)
                out.append(f"{self.live}, {k} sampled answers per problem at temperature {temp:g}: {solved} of {len(got) * k} correct "
                           f"({solved / (len(got) * k):.1%}).\n" + "\n".join(f"problem {i}: {c}/{k}" for i, c in got))
            if mine:
                ends = self.finals([(i, {"idx": i}) for i in mine], k, temp)
                out.append(f"{self.live}, {k} sampled answers per problem at temperature {temp:g}, the final answer of each:\n"
                           + "\n".join(f"problem {i}: {', '.join(repr(x) for x in f)}" for i, f in ends))
            return "\n".join(out) + "\n" + self.charge("chk", len(idxs), "these problems")
        n = max(1, min(len(self.val_rows), int(act.get("n", len(self.val_rows)))))
        self.need("val", n * k, "these validation answers")
        got = [round(c * k) for _, c in self.sampled([(r["id"], {"prompt": r["prompt"]}, r["answer"]) for r in self.val_rows[:n]], k, temp)]
        mean = sum(got) / len(got)
        se = math.sqrt(sum((x - mean) ** 2 for x in got) / max(1, len(got) - 1) * len(got))
        return (f"{self.live}, {k} sampled answers per problem at temperature {temp:g}: {sum(got)} of {len(got) * k} correct "
                f"({sum(got) / (len(got) * k):.1%}), give or take {se:.0f} answers. (the first {len(got)} validation problems)\n"
                + self.charge("val", n * k, "these validation answers"))

    def finish(self, handed, turns):
        unscored = [n for n, v in self.versions.items() if not v.get("val")]
        for n in unscored:
            self.versions[n]["val"] = {i: 0.0 for i in self.counted}
        super().finish(handed, turns)
        p = os.path.join(self.a.run, "result.json")
        res = json.load(open(p))
        for n in unscored:
            res["versions"][n].update(validation_correct=None, validation_all=None, validated=False)
        res["budgets"] = {k: {"of": self.total[k], "used": self.total[k] - self.left[k]} for k in NAMES}
        res["budgets"]["call_cost"] = self.a.call_cost
        json.dump(res, open(p, "w"), indent=1)
        for t in self.archiving:
            t.join()
        res["handed_in_dir"] = (self.versions[handed].get("archive") or self.versions[handed]["dir"]) if handed != "v0" else None
        res["version_dirs"] = {n: v.get("archive") or v["dir"] for n, v in self.versions.items() if n != "v0" and not v.get("pruned")}
        json.dump(res, open(p, "w"), indent=1)
        local = self.vroot != os.path.join(self.a.run, "versions")
        for n, v in self.versions.items():
            if n == "v0" or v.get("pruned"):
                continue
            if getattr(self.a, "keep_versions", False):
                if local and v.get("archive") and v["dir"] != v["archive"]:
                    shutil.rmtree(v["dir"], ignore_errors=True)
                continue
            if n != handed:
                shutil.rmtree(v["dir"], ignore_errors=True)


def main():
    ap = session_base.parser()
    ap.add_argument("--gen-budget", type=int, default=150_000,
                    help="generation units: forks, continuations and fresh answers (one token written = 1, plus --call-cost a call)")
    ap.add_argument("--train-units", type=int, default=25_000_000, help="training units: 3 per token of the trained answers per step")
    ap.add_argument("--val-looks", type=int, default=30, help="validation scores: one per (learning rate, steps) of a training, one per eval of the validation set")
    ap.add_argument("--problem-checks", type=int, default=300, help="pool problems the live student may be sampled on (eval with \"problems\")")
    ap.add_argument("--call-cost", type=int, default=50, help="generation units charged for every generation call, on top of its tokens")
    ap.add_argument("--own-pool", action="store_true",
                    help="the pool starts empty and the editor writes its problems (the pose action); validation and test are unchanged")
    ap.add_argument("--pose", action="store_true",
                    help="the editor may add problems of its own to the given pool (the pose action); they carry no reference answer")
    ap.add_argument("--max-own", type=int, default=600, help="--own-pool, --pose: most problems the editor may add")
    ap.add_argument("--key", action="store_true",
                    help="the editor is given the reference final answer of every pool problem (shown when a problem is opened)")
    ap.add_argument("--candidates", type=int, default=-1,
                    help="most training runs one train action may make at once, each on its own GPU beside the live student's "
                         "(default: one less than the job's GPUs; 0: one run at a time, as with one GPU)")
    ap.add_argument("--no-tries", action="store_true", help="leave out the option to continue one fork several times (fork 'tries')")
    ap.add_argument("--min-corrections", type=int, default=0,
                    help="the untrained student cannot be handed in before this many corrections are kept or half the generation budget is used")
    ap.add_argument("--pool-k", type=int, default=0,
                    help="every version answers each pool problem this many times, sampled as validation samples (--eval-temperature, the "
                         "same seeds for every version): the editor is told how many are right, and the answer it opens is a wrong one "
                         "when there is one. 0: one most likely answer per problem")
    ap.add_argument("--pool-sample", type=int, default=0,
                    help="--spread: after a training run the new version answers the trained problems and this many others (the same ones "
                         "each time); the rest are answered when the editor opens them (0: every problem after every run)")
    ap.add_argument("--default-optimizer", choices=("sgd", "adam"), default="sgd",
                    help="the optimizer of a train action that names none (the instructions say which)")
    ap.add_argument("--max-trainings", type=int, default=0,
                    help="most versions (training runs) a session makes; each is followed by the whole pool answered again (0: no such limit)")
    ap.add_argument("--min-kept", type=int, default=0,
                    help="no version can be handed in before this many corrections are kept or half the generation budget is used (0: no such floor)")
    ap.add_argument("--replay", default="", help="scripted:replay: the file of actions to take, one JSON action a line")
    ap.add_argument("--script-edits", type=int, default=60, help="scripted editors: corrections to make")
    ap.add_argument("--script-tries", type=int, default=40, help="scripted:greedy: most training runs in its search")
    ap.add_argument("--min-free-gb", type=int, default=90, help="a training run is refused when the run's disk has less free space than this")
    ap.add_argument("--context-chars", type=int, default=200_000,
                    help="growth of the conversation (characters, about 3.5 per token) after which old results are cut short")
    ap.add_argument("--grad-budget", type=int, default=0,
                    help="the most trained tokens a version may have in it (forked tokens trained up, replaced tokens pushed down, weighted "
                         "tokens, over all the corrections trained into it); a run that would go over is refused (0: no such limit)")
    ap.add_argument("--gen-threads", type=int, default=32, help="problems each student server is asked at once when a set is answered (validation, the pool)")
    ap.add_argument("--auto-cap", type=int, default=40, help="most steps a train with steps \"auto\" takes")
    ap.add_argument("--eval-k", type=int, default=4, help="sampled answers per problem of a validation score, unless the editor asks for more")
    ap.add_argument("--val-k-max", type=int, default=0,
                    help="most sampled answers per problem a validation score can have (the untrained student gets them all at the start; "
                         "0: --eval-k, so every score has the same number)")
    ap.add_argument("--eval-temperature", type=float, default=0.7)
    ap.add_argument("--gen-temperature", type=float, default=0.7, help="what the student writes after a fork is sampled at this temperature (0 = greedy)")
    ap.add_argument("--tau", type=float, default=0.5); ap.add_argument("--branch", type=int, default=25); ap.add_argument("--show", type=int, default=8)
    session_base.main(ap, Direct)


if __name__ == "__main__":
    main()
