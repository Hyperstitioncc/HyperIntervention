import asyncio, json, os, re, sys, tempfile, threading, types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SPECIAL = {"<|endoftext|>": 2000, "<|im_start|>": 2001, "<|im_end|>": 2002}
BACK = {v: k for k, v in SPECIAL.items()}
TEMPLATE = "<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n"
SUBJECTS = ["algebra", "geometry", "number_theory"]


class Tok:
    all_special_ids = list(SPECIAL.values())

    def get_added_vocab(self):
        return dict(SPECIAL)

    def __call__(self, text, add_special_tokens=False):
        ids, i = [], 0
        while i < len(text):
            for s, t in SPECIAL.items():
                if text.startswith(s, i):
                    ids.append(t)
                    i += len(s)
                    break
            else:
                assert ord(text[i]) < 2000, text[i]
                ids.append(ord(text[i]))
                i += 1
        return {"input_ids": ids}

    def decode(self, ids, skip_special_tokens=False):
        return "".join(("" if skip_special_tokens else BACK[t]) if t in BACK else chr(t) for t in ids)


TOK = Tok()


def fake_modules():
    web = types.SimpleNamespace(json_response=lambda body, status=200: types.SimpleNamespace(body=body, status=status))
    mods = {"aiohttp": types.SimpleNamespace(web=web),
            "transformers": types.SimpleNamespace(AutoTokenizer=types.SimpleNamespace(from_pretrained=lambda m: TOK)),
            "vllm": types.SimpleNamespace(SamplingParams=lambda **kw: types.SimpleNamespace(**kw)),
            "vllm.engine": types.SimpleNamespace(),
            "vllm.engine.arg_utils": types.SimpleNamespace(AsyncEngineArgs=lambda **kw: kw),
            "vllm.v1": types.SimpleNamespace(), "vllm.v1.engine": types.SimpleNamespace(),
            "vllm.v1.engine.async_llm": types.SimpleNamespace(AsyncLLM=None)}
    for k, v in mods.items():
        sys.modules[k] = v


fake_modules()
import session_base, judge_session, math_eval, student_server


class Engine:
    model = ""

    async def generate(self, prompt, sp, request_id):
        ids = prompt["prompt_token_ids"]
        if hasattr(sp, "prompt_logprobs"):
            plp = {k: {ids[k]: types.SimpleNamespace(logprob=-0.7), (ids[k] * 7 + 3) % 4000: types.SimpleNamespace(logprob=-4.0)}
                   for k in range(len(ids))}
            out = TOK("x")["input_ids"]
            lp = [{t: types.SimpleNamespace(logprob=-0.7), t + 1: types.SimpleNamespace(logprob=-0.9)} for t in out]
            yield types.SimpleNamespace(outputs=[types.SimpleNamespace(token_ids=out, logprobs=lp, finish_reason="stop", stop_reason=None)],
                                        prompt_logprobs=plp)
            return
        text = TOK.decode(ids)
        fresh = text.endswith("assistant\n")
        case = int((re.findall(r"case (\d+)\?", text) or [0])[-1])
        if text.endswith("the final answer is $\\boxed{"):
            out = TOK("5}$")["input_ids"]
            yield types.SimpleNamespace(outputs=[types.SimpleNamespace(token_ids=out, text="5}$", finish_reason="stop", stop_reason=None)])
            return
        if not hasattr(sp, "logprobs"):
            right = bool(re.search(r"/versions/v\d", self.model)) and case % 2 == 0
            out = TOK("So \\boxed{7}" if right else "So 5")["input_ids"]
            yield types.SimpleNamespace(outputs=[types.SimpleNamespace(token_ids=out, text=TOK.decode(out), finish_reason="stop", stop_reason=2002)
                                                 for _ in range(getattr(sp, "n", 1))])
            return
        out = TOK("So \\boxed{5}" if fresh else " then \\boxed{7}")["input_ids"]
        if fresh and getattr(sp, "temperature", 0) > 0:
            trained = bool(re.search(r"/versions/v\d", self.model))
            right = case == 0 or ((case + sp.seed) % 3 != 1 if trained else (case + sp.seed) % 3 == 0)
            out = TOK("So \\boxed{7}" if right else "So \\boxed{5}")["input_ids"]
        cut = out[:sp.max_tokens]
        lp = [{t: types.SimpleNamespace(logprob=-0.7), t + 1: types.SimpleNamespace(logprob=-0.9)} for t in cut]
        yield types.SimpleNamespace(outputs=[types.SimpleNamespace(
            token_ids=cut, logprobs=lp, finish_reason="length" if len(cut) < len(out) else "stop", stop_reason=SPECIAL["<|im_end|>"])])


class FakeStudent:

    started = []
    picks = []

    def __init__(self, a, gpu=None, n=0):
        self.a, self.n, self.proc, self.loop, self.lock = a, n, None, asyncio.new_event_loop(), threading.Lock()

    def start(self, model_dir, round_):
        FakeStudent.started.append((self.n, os.path.basename(model_dir)))
        cfg = re.sub(r"(?m)^model = .*$", f'model = "{self.a.base_for_test}"', open(self.a.config).read())
        live = os.path.join(self.a.run, f"ui-live{self.n}.toml")
        open(live, "w").write(cfg)
        steer = student_server.Steer(types.SimpleNamespace(config=live, gpu_mem=0.1, max_model_len=0, dtype="float32", round=round_))
        steer.engine.model = model_dir
        outer = self

        class Srv:
            def __call__(self, path, **body):
                async def js():
                    return body
                if path == "pick":
                    FakeStudent.picks.append(outer.n)
                with outer.lock:
                    r = outer.loop.run_until_complete(steer.handle(types.SimpleNamespace(path="/api/" + path, can_read_body=True, json=js)))
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}: {r.body}")
                return r.body
        self.proc = True
        return Srv()

    def stop(self):
        self.proc = None

    def down(self):
        return self.proc is None


class FakeJudge:
    plan = []

    def __init__(self, a):
        self.a, self.cost = a, [0, 0]

    tok = 0
    out = 0

    def ask(self, msgs):
        self.cost[0] += 10
        session_base.SPEND["tok"] += FakeJudge.tok
        while callable(FakeJudge.plan[0]):
            FakeJudge.plan.pop(0)()
        return FakeJudge.plan.pop(0), "", {"provider": "fake", "completion_tokens": FakeJudge.out}


def fake_run(cmd, **kw):
    arg = lambda k: cmd[cmd.index(k) + 1]
    tool = os.path.basename(cmd[1])
    if tool == "train_hf.py":
        fake_run.train.append(cmd)
        if fake_run.gate is not None:
            fake_run.gate.wait(30)
        if float(arg("--lr")) in fake_run.fail:
            print("out of memory", file=kw["stderr"], flush=True)
            return types.SimpleNamespace(returncode=1, stdout="", stderr="")
        lr, n = float(arg("--lr")), int(arg("--stages"))
        ck = os.path.join(arg("--out"), f"ckpt-{n:05d}")
        os.makedirs(ck)
        open(os.path.join(ck, "model.safetensors"), "w").write(f"lr{lr}-p{n}")
        eps = [json.loads(l) for l in open(arg("--episodes"))]
        json.dump({"forced_tokens": sum(len(e["episode"]["forks"]) for e in eps), "down_tokens": 2, "forced_logp_before": -2.0,
                   "forced_logp_after": -2.0 / (1 + n * lr * 1e6), "down_logp_before": -1.0, "down_logp_after": -2.0,
                   **({"n_params": 7_600_000_000, "moved_l2": 0.0123, "moved_mean_abs": 1.1e-7, "moved_max": 3e-6, "grad_l2": [180.0, 27.0]}
                      if "--measure" in cmd else {})},
                  open(os.path.join(ck, "stats.json"), "w"))
    elif tool == "eval_watch.py" and cmd[2] == "one":
        fl = lambda n, b, a_, u, d: {"n": n, "base": b, "acc": a_, "up": u, "down": d}
        arm = lambda gain: {"test": fl(100, 60, 60 + gain, 20 + gain, 20), "test_by_subject": {"algebra": fl(100, 60, 60 + gain, 20 + gain, 20)},
                            "test_base_boxed": fl(80, 55, 55 + gain // 2, 10, 10 - gain // 2), "test_base_unboxed": fl(20, 5, 5 + gain // 2, 6, 6 - gain // 2)}
        json.dump({"arms": {"trained": arm(10), "control": arm(0)}}, open(os.path.join(arg("--out"), arg("--tag") + ".json"), "w"))
    elif tool == "sample_eval.py":
        base = "base-test" in arg("--out")
        with open(arg("--out"), "w") as f:
            for k in range(20):
                f.write(json.dumps({"id": f"test/algebra/{k}", "frac": 0.5 if base else 0.5 + 0.25 * (k % 2), "k": 4}) + "\n")
    else:
        raise AssertionError(f"unexpected subprocess {cmd[:3]}")
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")


fake_run.train, fake_run.gate, fake_run.fail = [], None, set()


nval = 10


def session(tmp, name, plan, gen=10 ** 7, model="fake/editor", looks=15, own=False, key=False, pose=False, gpus=None, replay="", pool_k=0, kmax=0, workflows=1, tokens=0, spread=False, keep=False, runs=0, local="", sample=0, minutes=0, grads=0):
    run = os.path.join(tmp, name)
    os.makedirs(run, exist_ok=True)
    base = os.path.join(tmp, "model")
    if not os.path.exists(base):
        os.makedirs(base)
        json.dump({"eos_token_id": 2000}, open(os.path.join(base, "generation_config.json"), "w"))
    rows = [{"id": f"train/{SUBJECTS[i % 3]}/{i}", "prompt": f"How many ways, case {i}?", "answer": "7", "level": "Level 3"} for i in range(6)]
    val = [{"id": f"train/algebra/{100 + i}", "prompt": f"Val, case {i}?", "answer": "7"} for i in range(10)]
    for f, rs in (("prompts.jsonl", [] if own else rows), ("val.jsonl", val)):
        with open(os.path.join(run, f), "w") as fh:
            fh.writelines(json.dumps(r) + "\n" for r in rs)
    with open(os.path.join(run, "ui.toml"), "w") as f:
        f.write(f'model = "{base}"\nrun_dir = "{run}"\nprompts = "{run}/prompts.jsonl"\ntemplate = {json.dumps(TEMPLATE)}\n[episode]\neos = [2000, 2002]\n[episode.sampling]\nmax_new_tokens = 200\n')
    session_base.load = lambda split: [dict(r, solution="x") for r in rows]
    os.makedirs(os.path.join(tmp, "eval"), exist_ok=True)
    a = types.SimpleNamespace(
        config=os.path.join(run, "ui.toml"), val=os.path.join(run, "val.jsonl"), run=run, eval_out=os.path.join(tmp, "eval"), model=model, script_edits=3, script_tries=2,
        providers="", temperature=0.3, max_reply=1000, reasoning_tokens=100, turns=40, dtype="float32", gpu_mem=0.5,
        max_model_len=0, port=1, sample_k=0, seed=0, env=dict(os.environ), auto_cap=40, context_chars=10 ** 9, min_free_gb=0, gen_temperature=0.7, min_corrections=2, no_tries=False, own_pool=own, pose=pose, max_own=3, key=key, gpus=gpus, candidates=-1, replay=replay, pool_k=pool_k,
        base_for_test=base, gen_budget=gen, train_units=10 ** 9, val_looks=looks, problem_checks=3, call_cost=50, eval_k=1, val_k_max=kmax, workflows=workflows, token_budget=tokens, time_budget_min=minutes, grad_budget=grads, spread=spread, keep_versions=keep, max_trainings=runs, wall_min=0, local_dir=local, pool_sample=sample, eval_temperature=0.7, tau=0.5, branch=40, show=8)
    a.url, a.log = "x", run
    FakeJudge.plan = list(plan)
    s = judge_session.Direct(a)
    s.run()
    res = json.load(open(os.path.join(run, "result.json")))
    return run, res, [json.loads(l) for l in open(os.path.join(run, "agent.jsonl"))]


def main():
    os.environ["OPENROUTER_API_KEY"] = "test"
    student_server.AsyncLLM = types.SimpleNamespace(from_engine_args=lambda a: Engine())

    session_base.Student, session_base.Judge = FakeStudent, FakeJudge
    session_base.Grader = lambda: (lambda ans, text: math_eval.last_boxed(text) == ans)
    session_base.subprocess.run = fake_run
    tmp = tempfile.mkdtemp(prefix="session-test-")
    j = lambda **kw: "note\n```json\n" + json.dumps(kw) + "\n```"
    plan = [
        j(action="list", status="wrong"),
        j(action="open", idx=0),
        j(action="look", idx=0, to=2),
        j(action="fork", idx=0, pos=1, choice=1, write=2),
        j(action="continue", idx=0, write="all"),
        j(action="weight", idx=0, marks=[{"pos": 0, "w": -1}]),
        j(action="keep", idx=0),
        j(action="fork", idx=1, pos=1, choice=1, write=0),
        j(action="keep", idx=1),
        j(action="continue", idx=1),
        j(action="weight", idx=1, **{"from": 2, "to": 3, "w": 2}),
        j(action="keep", idx=1),
        j(action="fork", idx=2, pos=1, choice=1),
        j(action="undo", idx=2, pos=1),
        j(action="discard", idx=2),
        j(action="view", idx=0),
        j(action="train", lr=[1e-6, 2e-6], steps=3),
        j(action="train", lr=2e-6, steps=3, optimizer="sgd", embeddings="freeze", hold="rest", pull=0.1, start="base", data="all", validate=True),
        j(action="fork", idx=3, pos=1, choice=1, tries=3), j(action="weight", idx=3, below=0.6), j(action="keep", idx=3),
        j(action="train", lr=1e-6),
        j(action="validate"), j(action="use", version="v1"),
        j(action="eval", problems=[0, 1], k=1),
        j(action="eval", n=4, k=1),
        j(action="status"),
        j(action="finish", version="v1"),
    ]
    run, res, log = session(tmp, "d1", plan)
    flat = [(a_, r) for e in log for a_, r in zip(e["actions"], e["results"])]
    by = lambda name: [r for a_, r in flat if a_.get("action") == name]
    o = by("open")[0]
    assert "is WRONG" in o and "Reference" not in o and "<answer>" in o and "Undecided positions" in o, o
    assert "⟦0|50%⟧" in o and "is preceded by ⟦position|its probability⟧" in o, o
    assert "0 | " in by("look")[0] and "1:" in by("look")[0], by("look")[0]
    f = by("fork")
    assert "PAUSED" in f[0] and "generation units for the fork and 2 tokens written" in f[0] and "The last token, position" in f[0], f[0]
    assert "the fork and 0 tokens written" in f[1] and "PAUSED" in f[1], f[1]
    assert "The same fork continued 3 times ended with these final answers" in f[3] and "and 2 more continuations" in f[3], f[3]
    assert "'7'" in f[3].split("final answers")[1].split("\n")[0], f[3]
    c = by("continue")
    assert "complete" in c[0] and "PAUSED" not in c[0], c[0]
    k = by("keep")
    assert "kept as a correction (1 forks: @1 (" in k[0] and "1 weighted tokens)" in k[0] and "unfinished" in k[1] and "2 weighted tokens)" in k[2], k
    assert "The token you put in at 1" in f[0] and "had probability" in f[0], f[0]
    assert "After the first fork the student wrote" in f[0] and "The least likely" in f[0], f[0]
    col = [json.loads(l) for l in open(os.path.join(run, "collected.jsonl"))]
    assert len(col) == 3 and col[0]["episode"]["marks"] == [{"pos": 0, "token": col[0]["episode"]["completion"][0], "w": -1}], col[0]["episode"].get("marks")
    assert [m["w"] for m in col[1]["episode"]["marks"]] == [2, 2] and all(e["meta"]["correct"] for e in col)
    assert len(col[2]["episode"]["marks"]) >= 5 and all(m["w"] == 1 and m["pos"] > 1 for m in col[2]["episode"]["marks"]), col[2]["episode"].get("marks")
    assert "is undone" in by("undo")[0] and "discarded" in by("discard")[0] and "unknown action" in by("view")[0]
    t = by("train")
    assert "one learning rate per training run" in t[0], t[0]
    assert "Trained v1 from v0 on 2 corrections" in t[1] and "optimizer sgd, embeddings frozen, hold rest, pull 0.1: 3 steps" in t[1], t[1]
    assert "v1 is now the live student" in t[1] and "What v1 does on the pool" in t[1] and "the 2 problems it was trained on" in t[1], t[1]
    assert "Validation, v1: 1 sampled answers to each of" in t[1] and "v1 against v0, the untrained student (" in t[1] and "give or take" in t[1], t[1]
    assert "validation answers for this validation score" in t[1] and "ended 0.0123 from where they started" in t[1] and "training units for training 2 corrections" in t[1], t[1]
    assert "Sampled 1 times each, it solves them" in t[1], t[1]
    assert "Trained v2 from v1 on 1 corrections" in t[2] and "Validation" not in t[2] and "the 3 problems it was trained on" in t[2], t[2]
    assert "Validation, v2: 1 sampled answers" in by("validate")[0] and "v2 against v1, which it was trained from (" in by("validate")[0], by("validate")[0]
    cmd = fake_run.train[0]
    arg = lambda key: cmd[cmd.index(key) + 1]
    assert (arg("--optimizer"), arg("--momentum"), arg("--max-grad-norm"), arg("--keep"), arg("--pull"), arg("--stages"), arg("--forward")) == \
        ("sgdm", "0.0", "0", "rest", "0.1", "3", "f32"), cmd
    assert arg("--match-sign") == "2e-06" and "--measure" in cmd and arg("--freeze") == "embed_tokens,lm_head", cmd
    assert "--freeze" in fake_run.train[1] and "--match-sign" in fake_run.train[1], fake_run.train[1]
    assert "--until-taken" in fake_run.train[1] and fake_run.train[1][fake_run.train[1].index("--stages") + 1] == "40"
    e = by("eval")
    assert "problem 0: 1/1" in e[0] and "problem 1: 0/1" in e[0] and "2 problem checks for these problems; 1 left" in e[0], e[0]
    assert "2 of 4 correct" in e[1] and "validation problems" in e[1] and "give or take" in e[1] and "4 validation answers for these validation answers;" in e[1], e[1]
    assert "STILL AVAILABLE: " in by("status")[0] and "generation units: " in by("status")[0]
    g = res["budgets"]
    assert res["handed_in"] == "v1" and g["gen"]["of"] == 10 ** 7 and 0 < g["gen"]["used"] < 10 ** 4 and g["val"]["used"] == 2 * nval + 4 and g["val"]["of"] == 15 * nval and g["chk"]["used"] == 2, g
    assert g["tr"]["used"] > 0
    assert res["versions"]["v1"]["settings"]["optimizer"] == "sgd" and res["versions"]["v1"]["settings"]["hold"] == "rest"
    assert res["versions"]["v2"]["parent"] == "v1" and res["versions"]["v2"]["episodes"] == 1
    print("direct session: all checks passed")

    plan = [j(action="fork", idx=0, pos=1, choice=1), j(action="keep", idx=0),
            j(action="train", lr=1e-6, steps=2, validate=2),
            j(action="validate", k=2),
            j(action="validate", k=4),
            j(action="validate", k=9),
            j(action="fork", idx=1, pos=1, choice=1), j(action="keep", idx=1),
            j(action="train", lr=1e-6, steps=2, validate=True),
            j(action="compare", a="v1", b="v2"), j(action="compare", a="v1", b="v9"), j(action="status"),
            j(action="finish", version="v2")]
    runk, resk, logk = session(tmp, "dv", plan, kmax=4)
    rk = {a_["action"] + str(n): r for n, (a_, r) in enumerate((a_, r) for e in logk for a_, r in zip(e["actions"], e["results"]))}
    v0 = [json.loads(l) for l in open(os.path.join(runk, "versions", "v0-val.jsonl"))]
    assert all(len(r["marks"]) == 4 for r in v0), v0[0]
    assert "Validation, v1: 2 sampled answers" in rk["train2"] and f"{2 * nval} validation answers for this validation score" in rk["train2"], rk["train2"]
    assert "over the 2 answers per problem both have" in rk["train2"], rk["train2"]
    assert "already has its validation score" in rk["validate3"] and "validation answers for" not in rk["validate3"], rk["validate3"]
    assert "Validation, v1: 4 sampled answers" in rk["validate4"] and f"{2 * nval} validation answers for this validation score" in rk["validate4"], rk["validate4"]
    assert "both have" not in rk["validate4"] and "already has its validation score" in rk["validate5"], (rk["validate4"], rk["validate5"])
    assert "Validation, v2: 1 sampled answers" in rk["train8"] and "v2 against v1, which it was trained from (" in rk["train8"], rk["train8"]
    assert "against v1, which it was trained from:" in rk["train8"], rk["train8"]
    assert "On validation, v2 against v1 (" in rk["compare9"] and "over the 1 answers per problem both have" in rk["compare9"] and "On the pool" in rk["compare9"], rk["compare9"]
    assert '"a" and "b" must be two of' in rk["compare10"] and "at 4 answers a problem" in rk["status11"], (rk["compare10"], rk["status11"])
    assert resk["budgets"]["val"] == {"of": 15 * nval, "used": 5 * nval}, resk["budgets"]
    print("direct session, validation scores of chosen size: all checks passed")

    session_base.SPEND["tok"], FakeJudge.tok = 0, 30
    plan = [j(action="finish", version="v0"), j(action="fork", idx=0, pos=1, choice=1), j(action="keep", idx=0), j(action="status"),
            j(action="open", idx=1) + "\n" + j(action="finish", version="v0"), j(action="status")]
    runt, rest, logt = session(tmp, "dt", plan, tokens=100)
    FakeJudge.tok = 0
    outt = [r for e in logt for r in e["results"]]
    assert "the session is not over. It is ended from outside, at a point you are not told" in outt[0], outt[0]
    told = " ".join(m["content"] for m in json.load(open(os.path.join(runt, "state.json")))["msgs"] if m["role"] != "assistant")
    assert "own tokens" not in told and "Editor spend" not in told and "dollar" not in told and "generation units: " in told, told[-600:]
    lastmsg = [m["content"] for m in json.load(open(os.path.join(runt, "state.json")))["msgs"] if m["role"] == "user"]
    assert "this is your last reply" in lastmsg[4] and "this is your last reply" not in lastmsg[3], lastmsg[3:5]
    assert "only finish is carried out" in outt[4] and len(logt) == 5 and rest["handed_in"] == "v0", (outt[4:], len(logt))
    sysmsg = json.load(open(os.path.join(runt, "state.json")))["msgs"][0]["content"]
    assert "4. The session's length is not yours to set and you are not told it." in sysmsg and "accepted in your last reply" in sysmsg, sysmsg[-900:]
    assert "{" + "own_note" not in sysmsg and "{" + "record_note" not in sysmsg and "{" + "finish_note" not in sysmsg
    assert not re.findall(r"\{[a-z_]+\}", sysmsg), re.findall(r"\{[a-z_]+\}", sysmsg)
    for head in ("THE GOAL.", "HOW AN EDIT BECOMES TRAINING", "YOUR BUDGETS.", "WHAT TRAINING ON AN EDIT ACTUALLY DOES", "HOW EDITS THAT GENERALISE ARE MADE", "THE WORKING LOOP", "\nMEASUREMENT AND ITS NOISE\n"):
        assert sysmsg.count(head) == 1, (head, sysmsg.count(head))
    assert len(sysmsg) < 40000, len(sysmsg)
    assert "<<only" not in sysmsg and "\n>>\n" not in sysmsg and "YOUR BUDGET IS TIME" not in sysmsg and "Using a budget:" in sysmsg

    clock, slow = [5000.0], [0]
    tick = lambda secs: clock.__setitem__(0, clock[0] + secs)
    real_now, real_run = session_base.now, session_base.subprocess.run

    def slow_run(cmd, **kw):
        if any("train_hf.py" in str(c) for c in cmd):
            tick(slow[0])
        return real_run(cmd, **kw)
    session_base.now, session_base.subprocess.run = (lambda: clock[0]), slow_run
    try:
        plan = [lambda: tick(30), j(action="finish", version="v0"),
                lambda: tick(20), j(action="fork", idx=0, pos=1, choice=1) + "\n" + j(action="keep", idx=0),
                lambda: (tick(10), slow.__setitem__(0, 15)), j(action="train", lr=1e-6, steps=2, validate=True),
                lambda: (tick(15), slow.__setitem__(0, 60)),
                j(action="fork", idx=1, pos=1, choice=1) + "\n" + j(action="keep", idx=1) + "\n" + j(action="train", lr=1e-6, steps=2, validate=True) + "\n" + j(action="status"),
                lambda: tick(5), j(action="finish", version="v2"), j(action="status")]
        runc, resc, logc = session(tmp, "dclock", plan, minutes=2)
        stc = json.load(open(os.path.join(runc, "state.json")))
        userc = [m["content"] for m in stc["msgs"] if m["role"] == "user"]
        outc = [e["results"] for e in logc]
        assert float(open(os.path.join(runc, "clock_start")).read()) == 5000.0
        assert userc[0].endswith("TIME: 0 s used of 2 min; 2 min 00 s left. Begin."), userc[0][-120:]
        assert "the session is not over: 1 min 30 s of its time is left" in outc[0][0], outc[0]
        assert userc[1].endswith("TIME: 30 s used of 2 min; 1 min 30 s left."), userc[1][-120:]
        assert userc[2].endswith("TIME: 50 s used of 2 min; 1 min 10 s left."), userc[2][-120:]
        assert userc[3].endswith("TIME: 1 min 15 s used of 2 min; 45 s left."), userc[3][-120:]
        assert "Trained v1 from v0" in outc[2][0] and logc[2]["clock"] == 75.0, (outc[2][0][:200], logc[2]["clock"])
        assert len(outc[3]) == 4 and "kept as a correction" in outc[3][1], outc[3]
        assert outc[3][2].endswith(session_base.CUT_DURING) and "Trained v2" not in " ".join(outc[3]) and "this took" not in outc[3][2], outc[3][2]
        assert outc[3][3].endswith(session_base.CUT_BEFORE), outc[3][3]
        assert "Trained v2 from v1" in logc[3]["withheld"][0], logc[3]["withheld"]
        assert "TIME: 2 min 30 s used of 2 min; 0 s left." in userc[4] and "The session's time is up: this is your last reply" in userc[4], userc[4][-500:]
        assert "last reply" not in userc[3] and "Validation, v2" not in " ".join(userc), userc[3][-300:]
        assert stc["versions"]["v2"].get("late") and not stc["versions"]["v1"].get("late"), stc["versions"]["v2"].keys()
        assert "version must be one whose weights are kept: ['v0', 'v1']" in outc[4][0] and len(logc) == 5, (outc[4], len(logc))
        assert resc["handed_in"] in ("v0", "v1"), resc["handed_in"]
        toldc = " ".join(userc)
        assert "generation units" not in toldc and "STILL AVAILABLE" not in toldc and "validation answers for" not in toldc and "Actions left" not in toldc, toldc[-900:]
        assert "\n\n\n" not in toldc, toldc[-900:]
        sysc = stc["msgs"][0]["content"]
        assert "YOUR BUDGET IS TIME. The session lasts 2 min of real time, by the clock on the wall" in sysc and "YOUR BUDGETS." not in sysc, sysc[:300]
        assert "Your own tokens are limited" not in sysc and "YOUR TOKENS" not in " ".join(userc) and "trained tokens in it" not in sysc and "be earned by the corrections it is spent on" in sysc
        assert "it is not a step every batch needs" in sysc and "A sweep costs time on the GPUs" not in sysc and "Settle the training settings with a sweep" not in sysc
        assert "weight up the unlikely tokens" not in sysc.lower() and "Weight up the ones that carry" not in sysc and "within the first few tokens" not in sysc
        assert "Fork EARLY: at the first point in the solution where the student goes wrong, which is often in its first" in sysc and "3. Check each correction before you keep it." in sysc
        assert "a round does not need one to be built on" in sysc and sysc.count("\n1. ") >= 1 and sysc.count("2. Work in batches large enough to read.") == 1 and "{" + "loop_ga" not in sysc
        assert "Taking time on the GPUs:" in sysc and "Using a budget:" not in sysc and "accepted in your last reply, when the session's time is up." in sysc
        assert "<<only" not in sysc and "\n>>\n" not in sysc and not re.findall(r"\{[a-z_]+\}", sysc), re.findall(r"\{[a-z_]+\}", sysc)
        for head in ("THE GOAL.", "HOW AN EDIT BECOMES TRAINING", "YOUR BUDGET IS TIME.", "WHAT TRAINING ON AN EDIT ACTUALLY DOES", "HOW EDITS THAT GENERALISE ARE MADE", "THE WORKING LOOP", "\nMEASUREMENT AND ITS NOISE\n"):
            assert sysc.count(head) == 1, (head, sysc.count(head))
        assert "there is no point at which the student is good enough" in sysc and len(sysc) < 40000

        clock[0], slow[0] = 9000.0, 0
        plan = [lambda: tick(130), j(action="fork", idx=0, pos=1, choice=1) + "\n" + j(action="status"), j(action="status")]
        rund, resd, logd = session(tmp, "dclock2", plan, minutes=2)
        assert [r.endswith(session_base.CUT_BEFORE) for r in logd[0]["results"]] == [True, True], logd[0]["results"]
        assert "only finish is carried out" in logd[1]["results"][0] and len(logd) == 2 and resd["handed_in"] == "v0", (logd[1]["results"], len(logd))
        lastd = [m["content"] for m in json.load(open(os.path.join(rund, "state.json")))["msgs"] if m["role"] == "user"]
        assert "TIME: 2 min 10 s used of 2 min; 0 s left." in lastd[1] and "this is your last reply" in lastd[1], lastd[1][-400:]

        clock[0] = 30000.0
        session_base.SPEND["tok"], FakeJudge.tok = 0, 0
        plan = [j(action="fork", idx=0, pos=1, choice=1) + "\n" + j(action="weight", idx=0, marks=[{"pos": 2, "w": 2}, {"pos": 3, "w": -1}]) + "\n" + j(action="keep", idx=0),
                j(action="fork", idx=1, pos=1, choice=1) + "\n" + j(action="keep", idx=1),
                j(action="train", lr=1e-6, steps=2),
                j(action="train", lr=1e-6, steps=2, only=[1]),
                j(action="train", lr=1e-6, steps=2),
                lambda: setattr(FakeJudge, "tok", 600), j(action="status"), j(action="finish", version="v1"),
                j(action="status"), j(action="finish", version="v1")]
        rung, resg, logg = session(tmp, "dgrad", plan, minutes=600, grads=2, tokens=1000)
        FakeJudge.tok = 0
        rg = [e["results"] for e in logg]
        userg = [m["content"] for m in json.load(open(os.path.join(rung, "state.json")))["msgs"] if m["role"] == "user"]
        assert "would make a version with 4 trained tokens (0 already in v0, 4 in the 2 corrections to train) and a version may have at most 2" in rg[2][0], rg[2][0]
        assert "Trained v1 from v0" in rg[3][0] and userg[4].endswith("left. TRAINED TOKENS: 1 of 2 in v1. YOUR TOKENS: 0 used of 1,000."), (rg[3][0][:120], userg[4][-150:])
        assert "would make a version with 4 trained tokens (1 already in v1, 3 in the 1 corrections to train)" in rg[4][0], rg[4][0]
        assert userg[0].endswith("left. TRAINED TOKENS: 0 of 2 in v0. YOUR TOKENS: 0 used of 1,000. Begin.") and userg[1].count("TRAINED TOKENS") == 1, userg[0][-150:]
        assert "the session is not over: " in rg[6][0] and "of its time is left" in rg[6][0], rg[6][0]
        assert "YOUR TOKENS: 600 used of 1,000." in userg[7] and "YOUR TOKENS: 1,200 used of 1,000." in userg[8], (userg[7][-120:], userg[8][-500:])
        assert "Your tokens are used up: this is your last reply" in userg[8] and "time is up" not in userg[8], userg[8][-400:]
        assert resg["handed_in"] == "v1" and "Handed in v1." in rg[8][0], (resg["handed_in"], rg[8])
        sysg = json.load(open(os.path.join(rung, "state.json")))["msgs"][0]["content"]
        assert "a version can have at most 2 trained tokens in it" in sysg and "What things take with this session's" in sysg and "Your own tokens are limited as well: 1,000 in all" in sysg and "{" + "grad_note" not in sysg

        clock[0] = 20000.0
        os.makedirs(os.path.join(tmp, "dclock3"))
        open(os.path.join(tmp, "dclock3", "clock_start"), "w").write(repr(20000.0 - 100))
        rune, rese, loge = session(tmp, "dclock3", [j(action="status"), lambda: tick(30), j(action="status"), j(action="status")], minutes=2)
        usere = [m["content"] for m in json.load(open(os.path.join(rune, "state.json")))["msgs"] if m["role"] == "user"]
        assert usere[0].endswith("TIME: 1 min 40 s used of 2 min; 20 s left. Begin."), usere[0][-120:]
        assert usere[1].count("TIME: ") == 1 and usere[1].endswith("TIME: 1 min 40 s used of 2 min; 20 s left."), usere[1][-300:]
        assert loge[1]["results"][0].endswith(session_base.CUT_BEFORE) and "TIME: 2 min 10 s used of 2 min; 0 s left." in usere[2] and "this is your last reply" in usere[2], usere[2][-400:]
    finally:
        session_base.now, session_base.subprocess.run = real_now, real_run
    print("direct session, a time budget: all checks passed")
    assert '"lr": 2e-7, "steps": "auto", "until": 0.9' in sysmsg and "It always takes at least" in sysmsg and "continuations of a whole answer" in sysmsg
    assert '"optimizer": "sgd", "embeddings": "freeze",' in sysmsg and 'optimizer "sgd" is the default' in sysmsg and "as large as you can: there is no point at which the student is good enough" in sysmsg and "as long as the session lasts" not in sysmsg
    import types as _t
    fake = _t.SimpleNamespace(a=_t.SimpleNamespace(default_optimizer="adam"))
    cfg_a = judge_session.Direct.train_cfg(fake, {})
    assert cfg_a["optimizer"] == "adam" and cfg_a["embeddings"] == "train" and judge_session.Direct.train_cfg(fake, {"optimizer": "sgd"})["embeddings"] == "freeze", cfg_a
    session_base.SPEND["tok"] = 0
    session_base.SPEND["tok"], FakeJudge.tok = 0, 10
    def gone():
        raise session_base.JudgeFailed("editor calls keep failing")
    slept = []
    real_sleep, session_base.time.sleep = session_base.time.sleep, lambda s: slept.append(s)
    plan = ["I think I am done here.", gone_marker := "RAISE", j(action="status"), j(action="finish", version="v0")] + [j(action="finish", version="v0")] * 12
    real_ask = FakeJudge.ask
    def ask(self, msgs):
        if FakeJudge.plan and FakeJudge.plan[0] == "RAISE":
            FakeJudge.plan.pop(0); gone()
        return real_ask(self, msgs)
    FakeJudge.ask = ask
    try:
        runs_, ress_, logs_ = session(tmp, "dquiet", plan, tokens=100)
    finally:
        FakeJudge.ask, session_base.time.sleep, FakeJudge.tok = real_ask, real_sleep, 0
    outs_ = [r for e in logs_ for r in e["results"]]
    assert "no action found" in outs_[0] and "The session goes on: continue with your next action" in outs_[0] and "tokens" not in outs_[0], outs_[0]
    usr = [m["content"] for m in json.load(open(os.path.join(runs_, "state.json")))["msgs"] if m["role"] == "user"]
    assert slept == [60] and any("No reply was received from you. Continue" in u for u in usr) and "STILL AVAILABLE: " in outs_[1] + usr[2], (slept, usr[1][-200:])
    session_base.SPEND["tok"] = 0
    print("direct session, the editor's token budget: all checks passed")

    plan = [j(action="fork", idx=0, pos=1, choice=1), j(action="keep", idx=0), j(action="train", lr=1e-6, steps=2, validate=True),
            j(action="fork", idx=1, pos=1, choice=1), j(action="keep", idx=1), j(action="train", lr=1e-6, steps=2),
            j(action="fork", idx=2, pos=1, choice=1), j(action="keep", idx=2), j(action="train", lr=1e-6, steps=2),
            j(action="use", version="v1"), j(action="finish", version="v2")]
    runq, resq, logq = session(tmp, "dq", plan, keep=True, runs=2)
    outq = [r for e in logq for r in e["results"]]
    assert "a session can make 2 versions and 2 are made: 0 training runs are left" in outq[8], outq[8]
    assert any("training runs: 1 left (of 2; 1 made)" in m["content"] for m in json.load(open(os.path.join(runq, "state.json")))["msgs"] if m["role"] == "user")
    assert outq[9].startswith("v1 is the live student") and resq["handed_in"] == "v2", outq[9]
    assert all(os.path.exists(os.path.join(runq, "versions", v, "model.safetensors")) for v in ("v1", "v2")), os.listdir(os.path.join(runq, "versions"))
    firstq = json.load(open(os.path.join(runq, "state.json")))["msgs"][0]["content"]
    assert "Every version keeps its weights" in firstq and "at most 2 versions" in firstq, firstq[:3000]
    print("direct session, every version kept and a cap on training runs: all checks passed")

    plan = [j(action="fork", idx=0, pos=1, choice=1, up=0, down=0), j(action="keep", idx=0),
            j(action="weight", idx=0, marks=[{"pos": 3, "w": 3}]), j(action="keep", idx=0),
            j(action="train", lr=1e-6, steps=2), j(action="finish", version="v1")]
    n_before = len(fake_run.train)
    runz, resz, logz = session(tmp, "dz", plan)
    outz = [r for e in logz for r in e["results"]]
    assert "has nothing in it to train" in outz[1] and "kept as a correction" in outz[3], (outz[1], outz[3])
    cmdz = fake_run.train[n_before]
    epz = [json.loads(l)["episode"] for l in open(os.path.join(runz, "collected.jsonl"))]
    assert epz[0]["forks"][0]["up"] == 0 and epz[0]["forks"][0].get("down", 0) == 0 and epz[0]["marks"] and epz[0]["marks"][0]["w"] == 3, epz[0]
    sys.path.insert(0, HERE)
    import train_hf
    ld = train_hf.load_episodes(os.path.join(runz, "collected.jsonl"))
    assert len(ld) == 1 and len(ld[0]["forced"]) == 1 and ld[0]["forced_w"] == [3.0] and ld[0]["down"] == [], ld
    print("direct session, a fork that only steers: all checks passed")

    plan = [j(action="fork", idx=0, pos=1, choice=1), j(action="keep", idx=0),
            j(action="validate"), j(action="validate"),
            j(action="eval", problems=[0, 1, 2, 3], k=1),
            j(action="fork", idx=1, pos=1, choice=1), j(action="open", idx=1),
            j(action="finish", version="v0"),
            j(action="weight", idx=2, marks=[{"pos": 0, "w": 1}]), j(action="keep", idx=2), j(action="finish", version="v0")]
    run2, res2, log2 = session(tmp, "d2", plan, gen=300, looks=2)
    flat2 = [(a_, r) for e in log2 for a_, r in zip(e["actions"], e["results"])]
    r2 = {a_["action"] + str(n): r for n, (a_, r) in enumerate(flat2)}
    assert "generation units for the fork" in r2["fork0"] and "kept as a correction" in r2["keep1"], r2
    assert "already has its validation score" in r2["validate2"] and "scores zero" in r2["finish7"] and "needs about 4 problem checks and 3 are left" in r2["eval4"], r2
    assert "generation units and" in r2["fork5"] and "Problem 1" in r2["open6"] and res2["handed_in"] == "v0", r2
    print("direct session, budget exhausted: all checks passed")

    plan7 = [
        j(action="open", idx=0),
        j(action="pose", problem="How many ways, case 3?"),
        j(action="pose", problem="Count the arrangements in case 8?", subject="Counting"),
        j(action="pose", problem="Count the arrangements in case 8?"),
        j(action="pose", problem="Count the orderings there are in case 10?", answer="7"),
        j(action="list", status="wrong"),
        j(action="fork", idx=0, pos=1, choice=1), j(action="keep", idx=0),
        j(action="fork", idx=1, pos=1, choice=1), j(action="keep", idx=1),
        j(action="train", lr=1e-6, validate=True),
        j(action="pose", problem="Count the seatings possible in case 12?"),
        j(action="pose", problem="Count the handshakes made in case 14?"),
        j(action="eval", problems=[0, 2]), j(action="open", idx=0),
        j(action="finish", version="v1"),
    ]
    run7, res7, log7 = session(tmp, "d7", plan7, own=True)
    r7 = [r for e in log7 for r in e["results"]]
    assert "the pool is empty" in r7[0] and "held-out sets" in r7[1] and "already in the pool" in r7[3], r7[:4]
    assert "Problem 0 (own/counting/0) is in the pool. v0's own answer to it ends with the final answer '5'." in r7[2] and "generation units for the student's answer" in r7[2], r7[2]
    assert "0 | counting | '5' |" in r7[5] and "1 | own | '5' |" in r7[5] and "2 problems match" in r7[5], r7[5]
    assert "Trained v1 from v0 on 2 corrections" in r7[10] and "the 2 problems it was trained on" in r7[10], r7[10]
    assert "0: '5' → '5', sampled '7'; 1: '5' → '5', sampled '7'" in r7[10] and "the other 0 problems: 0 now end" in r7[10], r7[10]
    assert "Problem 2 (own/own/2) is in the pool. v1's own answer" in r7[11] and "the pool is full (3 problems)" in r7[12], r7[11:13]
    assert "the final answer of each:\nproblem 0: '7'\nproblem 2: '7'" in r7[13] and "2 problem checks for these problems; 1 left" in r7[13], r7[13]
    assert "v1's own answer to it ends with the final answer '5' (when you posed it: '5')" in r7[14], r7[14][:300]
    verdicts = ("is right", "WRONG", "newly right", "newly wrong", "still wrong", "answers correctly", " correct (", "right /")
    assert not any(w in r for r in r7 for w in verdicts), [r for r in r7 if any(w in r for w in verdicts)]
    pool7 = [json.loads(l) for l in open(os.path.join(run7, "prompts.jsonl"))]
    assert [x["id"] for x in pool7] == ["own/counting/0", "own/own/1", "own/own/2"] and all(x["answer"] == "" for x in pool7), pool7
    col7 = [json.loads(l) for l in open(os.path.join(run7, "collected.jsonl"))]
    assert len(col7) == 2 and all(c["meta"]["correct"] is None for c in col7), col7
    assert res7["handed_in"] == "v1" and len(json.load(open(os.path.join(run7, "status0.json")))) == 3
    first7 = json.load(open(os.path.join(run7, "state.json")))["msgs"][0]["content"]
    assert "A pool that starts EMPTY" in first7 and '"action": "pose"' in first7 and "{" + "pool" not in first7 and "{pose" not in first7, first7[:1500]
    assert "needs no reference answer" in first7 and '"answer": "..."' not in first7 and '"status": "wrong"' not in first7 and "right / wrong" not in first7, first7[:1500]
    assert not re.search(r"\{[a-z_]+\}", first7), re.findall(r"\{[a-z_]+\}", first7)
    print("direct session, a pool the editor writes: all checks passed")

    plan9 = [
        j(action="list"),
        j(action="pose", problem="Count the arrangements in case 8?", subject="counting"),
        j(action="list"), j(action="list", status="posed"), j(action="list", status="all"),
        j(action="fork", idx=0, pos=1, choice=1), j(action="keep", idx=0),
        j(action="fork", idx=6, pos=1, choice=1), j(action="keep", idx=6),
        j(action="train", lr=1e-6, validate=True),
        j(action="eval", problems=[0, 6]), j(action="open", idx=6), j(action="open", idx=0),
        j(action="pose", problem="Count the seatings possible in case 12?"),
        j(action="train", lr=1e-6),
        j(action="use", version="v0"),
        j(action="finish", version="v1"),
    ]
    run9, res9, log9 = session(tmp, "d9", plan9, pose=True, key=True)
    r9 = [r for e in log9 for r in e["results"]]
    assert "6 problems match" in r9[0] and "you posed" not in r9[0], r9[0]
    assert "Problem 6 (own/counting/6) is in the pool. v0's own answer to it ends with the final answer '5'." in r9[1], r9[1]
    assert "6 problems match" in r9[2] and '(1 problems you posed are not in this list: "status": "posed" lists them.)' in r9[2], r9[2]
    assert r9[3].startswith("Problems you posed, under v0: 1 problems match") and "6 | counting | '5' |" in r9[3] and "right" not in r9[3], r9[3]
    assert "by subject" in r9[4] and "Problems you posed, under v0: 1 problems match" in r9[4] and "| wrong |" in r9[4], r9[4]
    assert "counting" not in r9[4].split("\n")[0], r9[4]
    t9 = r9[9]
    assert "Trained v1 from v0 on 2 corrections" in t9 and "against the untrained student" in t9, t9
    assert "the 1 given problems it was trained on: 0 it now answers correctly by itself [], 1 still wrong [0]" in t9 and "(problem: right/1): 0: 1/1" in t9, t9
    assert "the other 5 given problems: 0 newly right" in t9 and "its answers to the given problems are" in t9, t9
    assert "the 1 problems you posed that it was trained on" in t9 and "when you posed it → now" in t9 and "6: '5' → '5', sampled '7'" in t9, t9
    assert "the other 0 problems you posed: 0 now end with a different final answer" in t9, t9
    e9 = r9[10]
    assert "1 of 1 correct" in e9 and "problem 0: 1/1" in e9 and "the final answer of each:\nproblem 6: '7'" in e9 and "2 problem checks for these problems; 1 left" in e9, e9
    assert "ends with the final answer '5' (when you posed it: '5')" in r9[11] and "Reference final answer" not in r9[11], r9[11][:300]
    assert "is WRONG (the untrained student: wrong)" in r9[12] and "Reference final answer: 7" in r9[12], r9[12][:300]
    assert "Problem 7 (own/own/7) is in the pool. v1's own answer" in r9[13] and "no kept corrections to train" in r9[14], r9[13:15]
    assert "v0 is the live student. Pool under it: 0 right / 6 wrong, and 2 problems you posed." in r9[15], r9[15]
    col9 = [json.loads(l) for l in open(os.path.join(run9, "collected.jsonl"))]
    assert [c["meta"]["correct"] for c in col9] == [True, None], col9
    pool9 = [json.loads(l) for l in open(os.path.join(run9, "prompts.jsonl"))]
    assert len(pool9) == 8 and [x["answer"] for x in pool9[6:]] == ["", ""] and res9["handed_in"] == "v1", pool9
    first9 = json.load(open(os.path.join(run9, "state.json")))["msgs"][0]["content"]
    assert "A pool of 6 problems" in first9 and "Problems of your own, if you want them" in first9 and '"action": "pose"' in first9, first9[:1500]
    assert "status: wrong | right | all | posed" in first9 and "for every problem you posed that the version was trained on" in first9, first9
    assert "you are given the reference final answer" in first9 and not re.search(r"\{[a-z_]+\}", first9), re.findall(r"\{[a-z_]+\}", first9)
    first1 = json.load(open(os.path.join(run, "state.json")))["msgs"][0]["content"]
    assert "pose" not in first1 and "posed" not in first1, "a session without --pose is told nothing of posing"
    print("direct session, the given pool with posed problems: all checks passed")

    gate = threading.Event()
    FakeStudent.started.clear()
    n_tr = len(fake_run.train)
    plan10 = [
        j(action="fork", idx=0, pos=1, choice=1), j(action="keep", idx=0), j(action="fork", idx=1, pos=1, choice=1), j(action="keep", idx=1),
        j(action="train", runs=[{"lr": 1e-6}, {"lr": 2e-6, "only": [0]}], validate=True),
        j(action="status"), j(action="use", version="v2"),
        j(action="fork", idx=2, pos=1, choice=1), j(action="keep", idx=2),
        lambda: setattr(fake_run, "gate", gate),
        j(action="train", runs=[{"lr": 1e-6}], background=True),
        j(action="train", lr=1e-6),
        j(action="open", idx=3), j(action="status"),
        j(action="finish", version="v2"),
        gate.set,
        j(action="wait"),
        lambda: setattr(fake_run, "gate", None),
        j(action="train", lr=1e-6),
        j(action="use", version="v2"), j(action="use", version="v3"), j(action="use", version="v0"),
        j(action="train", runs=[{"lr": 1e-6}, {"lr": 2e-6}, {"lr": 3e-6}]),
        j(action="finish", version="v2"),
    ]
    run10, res10, log10 = session(tmp, "d10", plan10, gpus=["0", "1", "2"])
    r10 = [r for e in log10 for r in e["results"]]
    c = r10[4]
    assert c.startswith('Candidates made: v1, v2. v0 is still the live student; "use" a candidate to go on from it.'), c[:200]
    assert "Trained v1 from v0 on 2 corrections" in c and "Trained v2 from v0 on 1 corrections" in c and "is now the live student" not in c, c
    assert "What v1 does on the pool" in c and "What v2 does on the pool" in c and "Validation, v1:" in c and "Validation, v2:" in c, c
    assert c.count("training units for training") == 2 and c.count("validation answers for this validation score") == 2, c
    assert sorted(float(t[t.index("--lr") + 1]) for t in fake_run.train[n_tr:n_tr + 2]) == [1e-6, 2e-6], fake_run.train[n_tr:]
    assert sorted(FakeStudent.started[:3]) == [(0, "model"), (1, "v1"), (2, "v2")], FakeStudent.started
    st10 = r10[5].split("\n")
    assert "<- live" in st10[0] and st10[1].endswith("(loaded)") and st10[2].endswith("(loaded)"), st10
    assert r10[6].startswith("v2 is the live student. Pool under it:"), r10[6]
    assert r10[9].startswith("Started in the background: v3. v2 stays the live student") and "[Made in the background]" not in r10[9], r10[9]
    assert "v3 is still being made" in r10[10] and "Problem 3" in r10[11] and "[Made in the background]" not in r10[11], r10[10:12]
    assert "Being made in the background: v3." in r10[12] and "v3 is still being made" in r10[13], r10[12:14]
    w = r10[14]
    assert w.startswith("v3 is done.\n\n[Made in the background]\nCandidates made: v3. v2 is still the live student") and "Trained v3 from v2 on 2 corrections" in w, w
    assert "Trained v4 from v2 on 2 corrections" in r10[15] and "v4 is now the live student" in r10[15], r10[15]
    assert r10[16].startswith("v2 is the live student") and "v3's weights were discarded" in r10[17], r10[16:18]
    assert r10[18].startswith("v0 is the live student") and '"runs" must be a list of 1 to 2 runs' in r10[19], r10[18:20]
    assert FakeStudent.started[3:] == [(0, "v3"), (0, "v4"), (0, "model")], FakeStudent.started
    assert res10["handed_in"] == "v2" and sorted(res10["versions"]) == ["v0", "v1", "v2", "v3", "v4"] and res10["budgets"]["val"]["used"] == 2 * nval, res10["budgets"]
    assert json.load(open(os.path.join(run10, "state.json")))["vnext"] == 5
    first10 = json.load(open(os.path.join(run10, "state.json")))["msgs"][0]["content"]
    assert '"runs": [' in first10 and "several training runs at once (up to 2)" in first10 and '{"action": "wait"}' in first10, first10
    assert not re.search(r"\{[a-z_]+\}", first10) and '"runs"' not in first1 and '"wait"' not in first1, "one GPU: nothing is said of runs"
    fake_run.fail = {5e-6}
    plan11 = [j(action="fork", idx=0, pos=1, choice=1), j(action="keep", idx=0),
              j(action="train", runs=[{"lr": 5e-6}, {"lr": 1e-6}]), j(action="status"), j(action="finish", version="v2")]
    run11, res11, log11 = session(tmp, "d11", plan11, gpus=["0", "1", "2"])
    r11 = [r for e in log11 for r in e["results"]]
    assert r11[2].startswith("Candidates made: v2. v0 is still the live student") and "Trained v2 from v0" in r11[2], r11[2]
    assert "v1: the training run failed (out of memory). Nothing was charged for it." in r11[2] and r11[2].count("training units for training") == 1, r11[2]
    assert sorted(res11["versions"]) == ["v0", "v2"] and res11["handed_in"] == "v2" and "v1" not in r11[3], r11[3]
    plan12 = [j(action="fork", idx=0, pos=1, choice=1), j(action="keep", idx=0), j(action="train", lr=5e-6), j(action="open", idx=1),
              j(action="train", lr=1e-6), j(action="finish", version="v2")]
    run12, res12, log12 = session(tmp, "d12", plan12)
    r12 = [r for e in log12 for r in e["results"]]
    assert "the training run failed (out of memory). v0 is the live student. Pool under it: 0 right / 6 wrong." in r12[2], r12[2]
    assert "Problem 1" in r12[3] and "v0's own answer" in r12[3] and "Trained v2 from v0 on 1 corrections" in r12[4] and "v2 is now the live student" in r12[4], r12[3:5]
    billed = int(re.search(r"\[([\d,]+) training units", r12[4]).group(1).replace(",", ""))
    assert res12["budgets"]["tr"]["used"] == billed > 0 and res12["handed_in"] == "v2", res12["budgets"]
    fake_run.fail = set()
    smoke = os.path.join(HERE, "../experiments/math/smoke_replay.jsonl")
    run13, res13, log13 = session(tmp, "d13", [], model="scripted:replay", gpus=["0", "1", "2"], pose=True, replay=smoke)
    acts13 = [a_["action"] for e in log13 for a_ in e["actions"]]
    bad13 = [(a_, r) for e in log13 for a_, r in zip(e["actions"], e["results"]) if "could not be done" in r or "failed inside" in r]
    assert not bad13 and acts13[-1] == "finish" and acts13.count("train") == 3 and "pose" in acts13, (bad13, acts13)
    assert res13["handed_in"] in ("v1", "v2") and len(res13["versions"]) == 5 and res13["editor_usd"] == 0.0, res13["versions"].keys()
    print("direct session, candidates on several GPUs: all checks passed")

    FakeStudent.started.clear()
    flows = os.path.join(HERE, "../experiments/math/smoke_flows.jsonl")
    runf, resf, logf = session(tmp, "df", [], model="scripted:replay", gpus=["0", "1", "2"], key=True, replay=flows, workflows=3)
    of = lambda w, kind: [r for e in logf if e.get("flow") == w for a_, r in zip(e["actions"], e["results"]) if a_.get("action") == kind]
    assert sorted(FakeStudent.started) == sorted([(n, v) for n in range(3) for v in ("model", "v1")]), FakeStudent.started
    stf = json.load(open(os.path.join(runf, "state.json")))
    sys0, sys1 = stf["flows"][0]["msgs"][0]["content"], stf["flows"][1]["msgs"][0]["content"]
    assert "YOU ARE ONE OF 3 CONVERSATIONS" in sys0 and "You are conversation 0, the lead." in sys0 and "Only you train, validate, use and finish" in sys0, sys0[:1500]
    assert "You are conversation 1." in sys1 and "are the\n  lead's (conversation 0)" in sys1 and "{flows_note}" not in sys1, sys1[:1500]
    assert "2 of the 6 pool problems are yours" in stf["flows"][1]["msgs"][1]["content"], stf["flows"][1]["msgs"][1]["content"]
    assert "2 problems match" in of(1, "list")[0] and "problem 0 is conversation 0's to work on" in of(1, "open")[0], (of(1, "list"), of(1, "open"))
    assert "kept as a correction" in of(1, "keep")[0] and "kept as a correction" in of(2, "keep")[0] and 'only the lead (conversation 0) can "train"' in of(1, "train")[0], of(1, "train")
    assert "This conversation has stopped" in of(1, "stop")[0], of(1, "stop")
    news = [x for e in logf if e.get("flow") == 0 for x in e.get("news", [])]
    assert any("Conversation 1 kept a correction on problem 1" in x for x in news) and any("Conversation 2 has stopped" in x for x in news), news
    t = of(0, "train")[0]
    assert "Trained v1 from v0 on 3 corrections" in t and "Validation, v1:" in t, t
    assert "Problem 4" in of(0, "open")[0], of(0, "open")
    assert "Conversations: 0 (lead) working; 1 stopped; 2 stopped." in of(0, "status")[0], of(0, "status")
    assert resf["handed_in"] == "v1" and not [r for e in logf for r in e["results"] if "failed inside" in r], resf
    FakeStudent.started.clear(); FakeStudent.picks.clear()
    plan = [j(action="fork", idx=0, pos=1, choice=1) + "\n" + j(action="fork", idx=1, pos=1, choice=1) + "\n" + j(action="fork", idx=2, pos=1, choice=1)
            + "\n" + j(action="keep", idx=0) + "\n" + j(action="keep", idx=1) + "\n" + j(action="keep", idx=2) + "\n" + j(action="status"),
            j(action="train", lr=1e-6, steps=2, validate=True),
            j(action="open", idx=4) + "\n" + j(action="open", idx=5) + "\n" + j(action="open", idx=99),
            j(action="finish", version="v1")]
    runp, resp, logp = session(tmp, "dsp", plan, gpus=["0", "1", "2"], key=True, spread=True)
    assert sorted(FakeStudent.started) == sorted([(n, v) for n in range(3) for v in ("model", "v1")]), FakeStudent.started
    assert sorted(FakeStudent.picks) == [0, 1, 2], FakeStudent.picks
    r0 = logp[0]["results"]
    assert len(r0) == 7 and all("kept as a correction" in x for x in r0[3:6]) and "Corrections kept: 3" in r0[6], r0
    assert "Trained v1 from v0 on 3 corrections" in logp[1]["results"][0], logp[1]["results"][0]
    assert "Problem 4" in logp[2]["results"][0] and "Problem 5" in logp[2]["results"][1] and "idx must be a pool index" in logp[2]["results"][2], logp[2]["results"]
    firstp = json.load(open(os.path.join(runp, "state.json")))["msgs"][0]["content"]
    assert "AT THE SAME TIME" in firstp and "held on 3 GPUs" in firstp and "CONVERSATIONS" not in firstp, firstp[:2500]
    assert resp["handed_in"] == "v1" and len(logp) == 4, (resp["handed_in"], len(logp))
    print("direct session, one conversation over several GPUs: all checks passed")

    FakeStudent.started.clear()
    plan = [j(action="fork", idx=0, pos=1, choice=1) + "\n" + j(action="keep", idx=0),
            j(action="train", runs=[{"lr": 1e-6, "steps": 2}, {"lr": 2e-6, "steps": 2}], validate=True),
            j(action="status"),
            j(action="use", version="v2"),
            j(action="pose", problem="How many ways can three books be put in a row, case 0?", subject="counting"),
            j(action="fork", idx=6, pos=1, choice=1) + "\n" + j(action="weight", idx=6, marks=[{"pos": 2, "w": 7}, {"pos": 3, "w": -4}]) + "\n" + j(action="keep", idx=6),
            j(action="train", lr=1e-6, steps=2),
            j(action="finish", version="v3")]
    runw, resw, logw = session(tmp, "dsw", plan, gpus=["0", "1", "2"], key=True, pose=True, spread=True, keep=True)
    rw = [e["results"] for e in logw]
    assert "Candidates made: v1, v2. v0 is still the live student" in rw[1][0] and "The other GPUs hold the candidates" in rw[1][0], rw[1][0][:600]
    assert rw[1][0].count("held on one GPU as a candidate") == 2 and "Validation, v1:" in rw[1][0] and "Validation, v2:" in rw[1][0], rw[1][0]
    assert rw[3][0].startswith("v2 is the live student"), rw[3][0][:300]
    st = FakeStudent.started
    assert st.count((1, "v1")) == 1 and st.count((2, "v2")) == 1 and st.count((0, "v2")) == 1 and st.count((1, "v2")) == 1, st
    assert "is in the pool" in rw[4][0] and "own/counting/6" in rw[4][0], rw[4][0][:300]
    assert "token weights now" in rw[5][1] and "7" in rw[5][1] and "-4" in rw[5][1] and "kept as a correction" in rw[5][2], rw[5]
    assert "Trained v3 from v2 on 1 corrections" in rw[6][0] and sorted(x for x in st if x[1] == "v3") == [(0, "v3"), (1, "v3"), (2, "v3")], (rw[6][0][:200], st)
    firstw = json.load(open(os.path.join(runw, "state.json")))["msgs"][0]["content"]
    assert "a sweep: several training runs in one action (up to 6)" in firstw and "This session has\n      3 GPUs" in firstw and "taking turns when there are more runs than that" in firstw and '"action": "pose"' in firstw and "w any whole number" in firstw, firstw[:200]
    assert resw["handed_in"] == "v3" and not [x for r in rw for x in r if "failed inside" in x or "could not be done" in x], [x for r in rw for x in r if "could not" in x or "failed" in x]
    FakeStudent.started.clear()
    plan = [j(action="fork", idx=0, pos=1, choice=1) + "\n" + j(action="keep", idx=0),
            j(action="train", runs=[{"lr": 1e-6, "steps": 2}], validate=True), j(action="use", version="v1"), j(action="finish", version="v1")]
    runx, resx, logx = session(tmp, "dsx", plan, gpus=["0", "1", "2"], key=True, spread=True)
    stx = FakeStudent.started
    assert stx.count((1, "v1")) == 1 and stx.count((2, "v1")) == 1 and stx.count((0, "v1")) == 1, stx
    assert "Validation, v1:" in logx[1]["results"][0] and logx[2]["results"][0].startswith("v1 is the live student") and resx["handed_in"] == "v1", logx[1]["results"][0][:300]
    FakeStudent.started.clear()
    plan = [j(action="fork", idx=0, pos=1, choice=1) + "\n" + j(action="keep", idx=0),
            j(action="train", runs=[{"lr": 1e-6, "steps": 2}, {"lr": 2e-6, "steps": 2}, {"lr": 4e-6, "steps": 2}], validate=True),
            j(action="status"), j(action="use", version="v1"),
            j(action="fork", idx=1, pos=1, choice=1) + "\n" + j(action="keep", idx=1), j(action="train", lr=1e-6, steps=2), j(action="finish", version="v4")]
    runz, resz, logz = session(tmp, "dsz", plan, gpus=["0", "1"], key=True, spread=True, keep=True)
    rz, stz = [e["results"] for e in logz], FakeStudent.started
    assert "Candidates made: v1, v2, v3. v0 is still the live student" in rz[1][0] and "The runs took turns on the other GPUs" in rz[1][0], rz[1][0][:500]
    assert all(f"Validation, v{n}:" in rz[1][0] and f"v{n} is a candidate; its weights are kept." in rz[1][0] for n in (1, 2, 3)), rz[1][0]
    assert [x for x in stz if x[1] in ("v1", "v2", "v3")][:3] == [(1, "v1"), (1, "v2"), (1, "v3")], stz
    assert rz[3][0].startswith("v1 is the live student") and stz.count((0, "v1")) == 1 and stz.count((1, "v1")) == 2, (rz[3][0][:200], stz)
    assert "Trained v4 from v1 on 1 corrections" in rz[5][0] and resz["handed_in"] == "v4", rz[5][0][:200]
    assert not [x for r in rz for x in r if "failed inside" in x or "could not be done" in x], [x for r in rz for x in r if "could not" in x or "failed" in x]
    sysz = json.load(open(os.path.join(runz, "state.json")))["msgs"][0]["content"]
    assert "(up to 6)" in sysz and "This session has\n      2 GPUs: the live student keeps one, and the runs are made on the other 1" in sysz, sysz[:200]
    FakeStudent.started.clear()
    plan = [j(action="fork", idx=0, pos=1, choice=1) + "\n" + j(action="keep", idx=0),
            j(action="train", runs=[{"lr": 1e-6, "steps": 2}, {"lr": 7e-6, "steps": 2}, {"lr": 4e-6, "steps": 2}], validate=True),
            j(action="use", version="v3"), j(action="finish", version="v3")]
    fake_run.fail.add(7e-6)
    try:
        runq, resq, logq = session(tmp, "dsq", plan, gpus=["0", "1"], key=True, spread=True, keep=True)
    finally:
        fake_run.fail.clear()
    rq = [e["results"] for e in logq]
    assert "Candidates made: v1, v3." in rq[1][0] and "v2: the training run failed" in rq[1][0] and "Validation, v3:" in rq[1][0], rq[1][0][:700]
    assert rq[2][0].startswith("v3 is the live student") and resq["handed_in"] == "v3", rq[2][0][:200]
    plan = [j(action="fork", idx=1, pos=1, choice=1, tries=3), j(action="keep", idx=1)] + [j(action="finish", version="v0")] * 4
    runy, resy, logy = session(tmp, "dsy", plan, gpus=["0", "1", "2"], key=True, spread=True)
    assert "could not be done" not in logy[0]["results"][0] and "The same fork continued 3 times" in logy[0]["results"][0], logy[0]["results"][0][:400]
    print("direct session, a sweep, a posed problem and free weights over several GPUs: all checks passed")

    FakeStudent.started.clear()
    plan = [j(action="fork", idx=0, pos=1, choice=1) + "\n" + j(action="keep", idx=0),
            j(action="train", lr=1e-6, steps=2),
            j(action="list", status="all"),
            "\n".join(j(action="open", idx=i) for i in range(1, 6)),
            j(action="list", status="all"),
            j(action="fork", idx=1, pos=1, choice=1) + "\n" + j(action="keep", idx=1),
            j(action="train", lr=1e-6, steps=2, pool="all"),
            j(action="list", status="all"),
            j(action="finish", version="v2")]
    scratch = os.path.join(tmp, "scratch-dsl")
    runl, resl, logl = session(tmp, "dsl", plan, gpus=["0", "1", "2"], key=True, pool_k=2, spread=True, keep=True, local=scratch, sample=1)
    rl = [e["results"] for e in logl]
    assert "the other 1 problems" in rl[1][0] and "answered when you open them" not in rl[1][0], rl[1][0]
    assert rl[2][0].count(" under v0") == 4 and rl[4][0].count(" under v0") == 0, (rl[2][0], rl[4][0])
    assert "the other 4 problems" in rl[6][0] and rl[7][0].count("/2 under v") == 0, (rl[6][0][:900], rl[7][0])
    firstl = json.load(open(os.path.join(runl, "state.json")))["msgs"][0]["content"]
    assert 'pool (add "pool": 500 or "pool": "all" to a train action)' in firstl and "1 unless you say otherwise" in firstl, firstl[:300]
    assert all(os.path.exists(os.path.join(runl, "versions", v, "model.safetensors")) for v in ("v1", "v2")), os.listdir(os.path.join(runl, "versions"))
    assert not os.path.exists(os.path.join(scratch, "versions", "v1")) and resl["version_dirs"]["v1"].startswith(runl) and resl["handed_in_dir"].startswith(runl), resl["version_dirs"]
    assert resl["handed_in"] == "v2" and not [x for r in rl for x in r if "failed inside" in x or "could not be done" in x]
    print("direct session, versions on the node's own disk and the pool answered on demand: all checks passed")

    FakeStudent.started.clear()
    runs_, ress_, logs_ = session(tmp, "dfs", [], model="scripted:keyed", gpus=["0", "1", "2"], key=True, workflows=3)
    bads_ = [r for e in logs_ for r in e["results"] if "could not be done" in r and "to work on" in r or "failed inside" in r]
    assert not bads_ and ress_["kept"]["corrections"] >= 1 and {n for n, _ in FakeStudent.started} == {0, 1, 2}, (bads_, ress_["kept"], FakeStudent.started)
    assert all(e.get("flow") == 0 or [a_["action"] for a_ in e["actions"]] == ["stop"] for e in logs_), [e.get("flow") for e in logs_]
    print("direct session, several conversations at once: all checks passed")

    import sample_eval
    ids = [f"test/x/{i}" for i in range(11)]
    parts = [os.path.join(tmp, f"s.shard{g}") for g in range(3)]
    for g, part in enumerate(parts):
        with open(part, "w") as f:
            f.writelines(json.dumps({"id": i, "frac": g / 2, "k": 2}) + "\n" for i in ids[g::3])
    sample_eval.join(parts, ids, os.path.join(tmp, "s.jsonl"))
    got = [json.loads(l) for l in open(os.path.join(tmp, "s.jsonl"))]
    assert [g["id"] for g in got] == ids and [g["frac"] for g in got[:4]] == [0.0, 0.5, 1.0, 0.0] and not any(map(os.path.exists, parts)), got
    open(parts[0], "w").write(json.dumps({"id": ids[0], "frac": 1.0, "k": 2}) + "\n")
    try:
        sample_eval.join(parts[:1], ids, os.path.join(tmp, "s2.jsonl"))
        raise SystemExit("a shard with problems missing was joined")
    except AssertionError as e:
        assert "10 problems missing" in str(e), e
    print("the sealed test over several GPUs: all checks passed")

    run8, res8, log8 = session(tmp, "d8", [j(action="open", idx=0)] + [j(action="finish", version="v0")] * 3, key=True)
    first8 = json.load(open(os.path.join(run8, "state.json")))["msgs"][0]["content"]
    assert "you are given the reference final answer" in first8 and "Reference final answer: 7" in log8[0]["results"][0], log8[0]["results"][0]
    assert "Reference final answer" not in o and "NOT given reference answers" in json.load(open(os.path.join(run, "state.json")))["msgs"][0]["content"]
    print("direct session, the answer key: all checks passed")

    run4, res4, log4 = session(tmp, "d4", [], model="scripted:blind")
    kinds = [a_["action"] for e in log4 for a_ in e["actions"]]
    assert kinds == ["fork", "keep"] * 3 + ["train", "finish"], kinds
    assert res4["kept"]["corrections"] == 3 and res4["handed_in"] == "v1" and res4["editor_usd"] == 0.0 and res4["budgets"]["gen"]["used"] > 0
    run5, res5, log5 = session(tmp, "d5", [], model="scripted:greedy")
    kinds = [a_["action"] for e in log5 for a_ in e["actions"]]
    assert kinds == ["fork", "keep"] * 3 + ["train"] * 3 + ["finish"], kinds
    assert all("only" in e["actions"][0] for e in log5[7:9]) and len(log5[7]["actions"][0]["only"]) == 1
    run6, res6, log6 = session(tmp, "d6", [], model="scripted:keyed")
    kinds = [a_["action"] for e in log6 for a_ in e["actions"]]
    assert kinds[:6] == ["fork", "keep"] * 3 and kinds.count("train") == 3 and kinds[-1] == "finish", kinds
    col6 = [json.loads(l) for l in open(os.path.join(run6, "collected.jsonl"))]
    assert len(col6) == 3 and all(c["meta"]["correct"] for c in col6)
    print("scripted direct editors: all checks passed")

    plan = [j(action="list"), j(action="list", status="sometimes"), j(action="list", status="right"), j(action="open", idx=2), j(action="open", idx=0),
            j(action="fork", idx=3, pos=1, choice=1), j(action="discard", idx=3), j(action="open", idx=3),
            j(action="fork", idx=2, pos=1, choice=1), j(action="keep", idx=2), j(action="open", idx=2),
            j(action="train", lr=1e-6, validate=True), j(action="list", status="all"), j(action="finish", version="v1")]
    runk, resk, logk = session(tmp, "dk", plan, key=True, pool_k=2)
    outk = [r for e in logk for r in e["results"]]
    byk = lambda kind: [r for e in logk for a_, r in zip(e["actions"], e["results"]) if a_.get("action") == kind]
    firstk = json.load(open(os.path.join(runk, "state.json")))["msgs"][0]["content"]
    assert "each answered 2 times by the student" in firstk and "sometimes (some right, some wrong)" in firstk, firstk[:3000]
    assert "single most likely answer" not in firstk and "Everything you see of the student is sampled" in firstk and "{" + "sampled_note}" not in firstk
    begin = json.load(open(os.path.join(runk, "state.json")))["msgs"][1]["content"]
    assert "5 of 12 answers right; 1 problems right every time, 3 some of the time, 2 never" in begin, begin
    l = byk("list")
    assert "5 problems match" in l[0] and "\n1 | geometry | Level 3 | 0/2 | wrong, " in l[0] and "\n2 | number_theory | Level 3 | 1/2 | wrong, " in l[0], l[0]
    assert "\n0 | " not in l[0] and "3 problems match" in l[1] and "1 problems match" in l[2] and "\n0 | algebra | Level 3 | 2/2 | right, " in l[2], (l[1], l[2])
    assert "algebra 3/4; geometry 0/4; number_theory 2/4" in l[0], l[0]
    o = [re.sub(r"⟦\d+\|\d+%⟧", "", x) for x in byk("open")]
    assert "v0 got it right in 1 of 2 sampled answers. The one you work on is the first of the WRONG ones" in o[0] and "boxed{5}" in o[0], o[0]
    assert "v0 got it right in 2 of 2 sampled answers. None was wrong" in o[1] and "a RIGHT answer" in o[1] and "boxed{7}" in o[1], o[1]
    assert "opening it again shows the same sampled answer" in byk("discard")[0]
    assert "writing this answer afresh" not in o[2] and "Forks in force: none" in o[2] and "boxed{5}" in o[2] and "then" not in o[2], o[2]
    assert "Forks in force: none" in o[3] and "then" not in o[3], o[3]
    t = byk("train")[0]
    assert "sampled answers right of 2 (problem: untrained→now): 2: 1→2. Over all of them 2 of 2 are right (untrained: 1)" in t, t
    assert "the other 5 problems: 7 of 10 sampled answers right (untrained: 4; change +3, give or take 1); 3 problems have more right answers than before [1, 4, 5], 0 have fewer []" in t, t
    assert "of 12 run to the length limit (untrained: 0)" in t and "one greedy answer" not in t and "v1 is now the live student" in t, t
    assert "v1 got it right" not in l[3] and "Pool under v1" in l[3] and "\n2 | number_theory | Level 3 | 2/2 | right, " in l[3] and "kept@v0" in l[3], l[3]
    s0 = json.load(open(os.path.join(runk, "status0.json")))
    assert s0["2"]["right"] == 1 and s0["2"]["k"] == 2 and s0["2"]["shown"] is False and s0["0"]["correct"] is True and s0["1"]["correct"] is False, s0
    assert resk["handed_in"] == "v1" and resk["kept"]["corrections"] == 1
    colk = [json.loads(x) for x in open(os.path.join(runk, "collected.jsonl"))]
    assert len(colk) == 1 and colk[0]["meta"]["correct"] and len(colk[0]["episode"]["forks"]) == 1
    print("direct session, a pool answered by sampling: all checks passed")

    import collections, random as _random
    rnd, splits = _random.Random(1), {}
    for split, n in (("train", 7500), ("test", 5000)):
        splits[split] = [{"id": f"{split}/{SUBJECTS[i % 3]}/{i}", "prompt": f"{split} {i}", "answer": "7",
                          "level": f"Level {rnd.choice([1, 2, 3, 3, 4, 4, 5, 5, 5] if split == 'train' else [1, 2, 3, 4, 5])}",
                          "type": SUBJECTS[i % 3]} for i in range(n)]
    order = math_eval.mixed(4, splits)
    assert len(order) == 12500 and len({r["id"] for r in order}) == 12500
    assert [r["id"] for r in order] == [r["id"] for r in math_eval.mixed(4, splits)] != [r["id"] for r in math_eval.mixed(5, splits)]
    assert all(r["id"].split("/")[0] == "mix" and r["id"].split("/")[1] == r["type"] and r["id"].split("/")[2][0] in "ab" for r in order)
    key = lambda r: (r["id"].split("/")[2][0], r["level"], r["type"])
    whole = collections.Counter(map(key, order))
    for lo, hi in ((0, 600), (5000, 5500), (6000, 7500), (7500, 12500)):
        part = collections.Counter(map(key, order[lo:hi]))
        worst = max(abs(part[k] - whole[k] * (hi - lo) / 12500) for k in whole)
        assert worst <= 1.0 + 1e-9, (lo, hi, worst)
    twice = {"train": [dict(splits["train"][0]), dict(splits["train"][1], prompt="train  0")], "test": [dict(splits["test"][0], prompt="train 0")]}
    assert [r["id"] for r in math_eval.mixed(4, twice)] == ["mix/algebra/a0"]
    print("the mixed split: all checks passed")
    lab_fix(tmp, j)


def lab_fix(tmp, j):
    class FakeGrader:
        def __init__(self, model="fake/grader", usd=0.0, spent=0.0):
            self.model, self.spent = model, spent
        def over(self):
            return False
        def evaluate(self, prompt):
            return "pass", "fine", {"cost": 0.0}
    import lab_collect, lab_eval, lab_session
    lab_eval.Grader = FakeGrader
    lab = os.path.join(tmp, "lab")
    prompts = os.path.join(lab, "lab_core", "evaluation", "prompts")
    os.makedirs(prompts)
    open(os.path.join(prompts, "rubric_criterion.txt"), "w").write("rub {task_description} {criterion_title} {match_criteria} {agent_output}")
    row = {"id": "area/one", "area": "area", "work_type": "draft", "title": "A task", "instructions": "Write the memo.",
           "deliverable": "Memo", "criteria": [{"id": "c1", "title": "It is a memo", "match_criteria": "PASS if it is."}],
           "prompt": "Write the memo.\n\n=== documents/a.txt ===\nhello document\n", "tokens": 20}
    pool = os.path.join(lab, "pool.jsonl")
    open(pool, "w").write(json.dumps(row) + "\n")
    val = os.path.join(lab, "val.jsonl")
    open(val, "w").write(json.dumps(row) + "\n")
    run = os.path.join(lab, "run")
    os.makedirs(run)
    cfg = os.path.join(run, "ui.toml")
    open(cfg, "w").write(f'model = "{os.path.join(tmp, "model")}"\nrun_dir = "{run}"\nprompts = "{pool}"\n'
                         f'template = {json.dumps("<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n")}\nread = "plain"\n'
                         '[episode]\neos = [151643, 151645]\n[episode.sampling]\ntemperature = 0.0\nmax_new_tokens = 4096\n')
    a = lab_session.parser().parse_args(["--config", cfg, "--val", val, "--run", run, "--eval-out", lab, "--lab", lab,
                                         "--test", val, "--fix-only", "--model", "fake/judge", "--turns", "20"])
    a.url, a.log, a.fix_only = "http://127.0.0.1:1", run, True
    a.gpus = [None]
    a.env = dict(os.environ)
    a.base_for_test = os.path.join(tmp, "model")
    student = session_base.Student(a, "0", 0)
    srv = student.start(a.base_for_test, 0)
    FakeJudge.plan = [
        j(action="list"),
        j(action="open", idx=0),
        j(action="options", idx=0, pos=2, text="hi"),
        j(action="fork", idx=0, pos=2, text="hi", write=3),
        j(action="continue", idx=0, write="all"),
        j(action="edit", idx=0, pos=4, text="XY"),
        j(action="keep", idx=0),
        j(action="finish", version="v0"),
    ]
    ak = types.SimpleNamespace(**{**vars(a), "run": os.path.join(run, "w0")})
    os.makedirs(ak.run)
    lab_collect.Shared(ak, srv, [0], run).run()
    student.stop()
    eps = [json.loads(l)["episode"] for l in open(os.path.join(run, "collected.jsonl"))]
    assert len(eps) == 1 and [f["pos"] for f in eps[0]["forks"]] == [2, 4], eps[0]["forks"]
    for f in eps[0]["forks"]:
        assert eps[0]["completion"][f["pos"]:f["pos"] + len(f["forced"])] == f["forced"]
    res = json.load(open(os.path.join(run, "w0", "result.json")))
    assert res["handed_in"] == "v0" and res["fixes"]["corrections"][0]["forks"] == [2, 4], res
    st = json.load(open(os.path.join(run, "w0", "state.json")))
    assert st["msgs"][0]["role"] == "system" and st["handed"] == "v0", sorted(st)
    print("a fix-only LAB conversation, its options, text forks and edits: all checks passed")


if __name__ == "__main__":
    main()
