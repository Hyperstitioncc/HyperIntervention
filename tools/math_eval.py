import argparse, json, math, os, random, re, sys

SYSTEM = "Please reason step by step, and put your final answer within \\boxed{}."
SHOTS = {
    "1": ("What is the value of $2 + 3 \\cdot 4$?",
          "Multiplication comes before addition, so first $3 \\cdot 4 = 12$. Then $2 + 12 = 14$.\n\n"
          "The final answer is $\\boxed{14}$."),
    "2": ("If $3x + 7 = 2x + 15$, what is the value of $x^2 - 1$?",
          "We are given the equation $3x + 7 = 2x + 15$ and asked for the value of $x^2 - 1$.\n\n"
          "First, solve for $x$. Subtract $2x$ from both sides:\n$$x + 7 = 15.$$\n"
          "Subtract $7$ from both sides:\n$$x = 8.$$\n\n"
          "Now compute $x^2 - 1$. Since $x = 8$, we have $x^2 = 64$, so\n$$x^2 - 1 = 64 - 1 = 63.$$\n\n"
          "Check: $3(8) + 7 = 31$ and $2(8) + 15 = 31$, so $x = 8$ does satisfy the equation.\n\n"
          "Therefore, the final answer is $\\boxed{63}$."),
}
_shot = SHOTS.get(os.environ.get("MATH_SHOT", "0"))
SHOT = f"<|im_start|>user\n{_shot[0]}<|im_end|>\n<|im_start|>assistant\n{_shot[1]}<|im_end|>\n" if _shot else ""
TEMPLATE = (
    "<|im_start|>system\n" + SYSTEM + "<|im_end|>\n" + SHOT +
    "<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n"
)
if os.environ.get("MATH_PROMPT") == "plain":
    TEMPLATE = ("<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n<|im_start|>user\n{prompt}\n" + SYSTEM
                + "<|im_end|>\n<|im_start|>assistant\n")
SUBJECTS = ["algebra", "counting_and_probability", "geometry", "intermediate_algebra",
            "number_theory", "prealgebra", "precalculus"]


def last_boxed(s):
    i = s.rfind("\\boxed")
    if i < 0:
        return None
    j = s.find("{", i)
    if j < 0:
        return None
    depth = 0
    for k in range(j, len(s)):
        depth += {"{": 1, "}": -1}.get(s[k], 0)
        if depth == 0:
            return s[j + 1:k]
    return None


MIX = int(os.environ.get("MATH_MIX", "0") or 0)
MIX_TEST = 5000
_mixed = {}


def official(split):
    from datasets import load_dataset
    rows = []
    for sub in SUBJECTS:
        for i, r in enumerate(load_dataset("EleutherAI/hendrycks_math", sub, split=split)):
            rows.append({"id": f"{split}/{sub}/{i}", "prompt": r["problem"], "answer": last_boxed(r["solution"]),
                         "level": r["level"], "type": r["type"], "solution": r["solution"]})
    return rows


def mixed(seed, splits):
    rnd, groups, seen = random.Random(seed), {}, set()
    for split, tag in (("train", "a"), ("test", "b")):
        for r in splits[split]:
            text = " ".join(r["prompt"].split())
            if text in seen:
                continue
            seen.add(text)
            _, sub, i = r["id"].split("/")
            groups.setdefault((split, r["level"], r["type"]), []).append(dict(r, id=f"mix/{sub}/{tag}{i}"))
    placed = []
    for key in sorted(groups):
        g = groups[key]
        rnd.shuffle(g)
        u = rnd.random()
        placed += [((k + u) / len(g), key, r["id"], r) for k, r in enumerate(g)]
    return [t[-1] for t in sorted(placed, key=lambda t: t[:3])]


def load(split):
    if not MIX:
        return official(split)
    if MIX not in _mixed:
        _mixed[MIX] = mixed(MIX, {s: official(s) for s in ("train", "test")})
    rows = _mixed[MIX]
    return rows[len(rows) - MIX_TEST:] if split == "test" else rows[:len(rows) - MIX_TEST]


def braced(tex):
    out, i = [], 0
    macro = re.compile(r"\\(dfrac|tfrac|frac|sqrt)(?![a-zA-Z])")
    while True:
        m = macro.search(tex, i)
        if not m:
            out.append(tex[i:])
            return "".join(out)
        out.append(tex[i:m.end()])
        j = m.end()
        if m.group(1) == "sqrt" and j < len(tex) and tex[j] == "[":
            k = tex.find("]", j)
            if k < 0:
                i = j
                continue
            out.append(tex[j:k + 1])
            j = k + 1
        for _ in range(1 if m.group(1) == "sqrt" else 2):
            while j < len(tex) and tex[j] == " ":
                j += 1
            if j >= len(tex):
                break
            if tex[j] == "{":
                depth, k = 0, j
                while k < len(tex):
                    depth += {"{": 1, "}": -1}.get(tex[k], 0)
                    if depth == 0:
                        break
                    k += 1
                out.append("{" + braced(tex[j + 1:k]) + "}")
                j = k + 1
            elif tex[j] == "\\":
                c = re.match(r"\\[a-zA-Z]+|\\.", tex[j:]).group(0)
                out.append("{" + c + "}")
                j += len(c)
            else:
                out.append("{" + tex[j] + "}")
                j += 1
        i = j


def canon(tex):
    tex = braced(tex or "")
    tex = re.sub(r"\\[dt]frac", r"\\frac", tex)
    tex = re.sub(r"\\left|\\right|\\!|\\,|\;|\\ |\$", "", tex)
    tex = re.sub(r"\^\{?\\circ\}?|°", "", tex)
    tex = re.sub(r"\\text\{\s*\}", "", tex)
    return re.sub(r"\s+", "", tex).rstrip(".")


def grade(answer, completion):
    from math_verify import parse, verify
    boxed = last_boxed(completion)
    try:
        if verify(parse("$" + braced(answer) + "$"), parse(braced(completion))):
            return True
    except Exception:
        pass
    return boxed is not None and canon(boxed) == canon(answer)


MAX_TOKENS = int(os.environ.get("MATH_EVAL_MAX_TOKENS", 4096))


def make_llm(model, lora=None, max_tokens=MAX_TOKENS, gpu_mem=0.9):
    from vllm import LLM
    kw = dict(model=model, dtype=os.environ.get("MATH_EVAL_DTYPE", "float32"), max_model_len=max_tokens + 2048, seed=0, gpu_memory_utilization=gpu_mem)
    if os.environ.get("STUDENT_MOE_BACKEND"):
        kw["moe_backend"] = os.environ["STUDENT_MOE_BACKEND"]
    if lora:
        kw.update(enable_lora=True, max_lora_rank=json.load(open(os.path.join(lora, "adapter_config.json")))["r"])
    return LLM(**kw)


FINISH = os.environ.get("MATH_FINISH", "0") != "0"
FINISH_TEXT = "\n\nTherefore, the final answer is $\\boxed{"
_LEFT_TURN = re.compile(r"\n(?:user|system|assistant)\n|\n(?:Human|Assistant|User):|<\|im_start\|>|Please reason step by step")


def scored_set():
    dev = os.environ.get("MATH_DEV_ROWS")
    return [json.loads(l) for l in open(dev)] if dev else load("test")


STOP_IDS = [151643, 151645]


def read(text):
    return own_turn(text) if FINISH else text


def needs_finish(text):
    return FINISH and "\\boxed" not in text


def own_turn(text):
    m = _LEFT_TURN.search(text)
    return text[:m.start()] if m else text


def finish(llm, rows, texts, req=None):
    from vllm import SamplingParams
    cut = [[own_turn(t) for t in ts] for ts in texts]
    todo = [(i, j) for i, ts in enumerate(cut) for j, t in enumerate(ts) if "\\boxed" not in t]
    if todo:
        sp = SamplingParams(temperature=0.0, max_tokens=64, stop=["\n"], stop_token_ids=STOP_IDS)
        outs = llm.generate([TEMPLATE.replace("{prompt}", rows[i]["prompt"]) + cut[i][j].rstrip() + FINISH_TEXT for i, j in todo],
                            sp, lora_request=req)
        for (i, j), o in zip(todo, outs):
            cut[i][j] = cut[i][j].rstrip() + FINISH_TEXT + o.outputs[0].text
    return cut


def generate(llm, rows, lora=None, max_tokens=MAX_TOKENS, n=1, temperature=0.0, seed=0):
    from vllm import SamplingParams
    req = None
    if lora:
        from vllm.lora.request import LoRARequest
        req = LoRARequest("adapter", 1, lora)
    top_p = float(os.environ.get("MATH_TOP_P") or 0)
    sp = SamplingParams(n=n, temperature=temperature, max_tokens=max_tokens, seed=seed, stop_token_ids=STOP_IDS,
                        **({"top_p": top_p} if top_p and temperature > 0 else {}))
    outs = llm.generate([TEMPLATE.replace("{prompt}", r["prompt"]) for r in rows], sp, lora_request=req)
    texts = [[c.text for c in o.outputs] for o in outs]
    return finish(llm, rows, texts, req) if FINISH else texts


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    l = sp.add_parser("list"); l.add_argument("--n", type=int, default=600); l.add_argument("--seed", type=int, default=4)
    l.add_argument("--skip", type=int, default=0, help="leave out the first SKIP of the shuffle (an earlier list)")
    l.add_argument("--levels", default="1,2,3,4,5"); l.add_argument("--out", required=True)
    c = sp.add_parser("compare"); c.add_argument("a"); c.add_argument("b")
    sp.add_parser("mix-report")
    a = ap.parse_args()

    if a.cmd == "mix-report":
        if not MIX:
            sys.exit("set MATH_MIX=SEED")
        tr = [r for r in load("train") if r["answer"]]
        sets = {"pool": tr[:600], "validation": tr[5000:5500], "development": tr[6000:7500], "test": load("test")}
        ids = [set(r["id"] for r in v) for v in sets.values()]
        print(f"MATH_MIX={MIX}: {len(tr)} problems with an answer outside the test; problems shared between any two sets: "
              f"{sum(len(x & y) for i, x in enumerate(ids) for y in ids[i + 1:])}")
        for name, v in sets.items():
            n = len(v)
            lv = " ".join(f"L{k[-1]} {100 * sum(r['level'] == k for r in v) / n:4.1f}" for k in sorted({r["level"] for r in tr}))
            ty = " ".join(f"{t[:5]} {100 * sum(r['type'] == t for r in v) / n:4.1f}" for t in sorted({r["type"] for r in tr}))
            print(f"{name:12s} {n:5d}  from official test {100 * sum(r['id'].rsplit('/', 1)[1][0] == 'b' for r in v) / n:4.1f}%  |  {lv}  |  {ty}")
        return

    if a.cmd == "list":
        levels = {f"Level {x}" for x in a.levels.split(",")}
        rows = [r for r in load("train") if r["answer"] and r["level"] in levels]
        if not MIX:
            random.Random(a.seed).shuffle(rows)
        rows = rows[a.skip:a.skip + a.n]
        with open(a.out, "w") as f:
            for r in rows:
                f.write(json.dumps({k: r[k] for k in ["id", "prompt", "answer", "level", "type"]}) + "\n")
        print(f"{len(rows)} problems -> {a.out}", file=sys.stderr)

    else:
        A = {r["id"]: r["correct"] for r in map(json.loads, open(a.a))}
        B = {r["id"]: r["correct"] for r in map(json.loads, open(a.b))}
        ids = sorted(A.keys() & B.keys())
        gain = sum(B[i] and not A[i] for i in ids)
        loss = sum(A[i] and not B[i] for i in ids)
        n, k = gain + loss, min(gain, loss)
        pval = min(1.0, 2 * sum(math.comb(n, j) for j in range(k + 1)) / 2 ** n) if n else 1.0
        print(json.dumps({"n": len(ids), "acc_a": sum(A[i] for i in ids) / len(ids),
                          "acc_b": sum(B[i] for i in ids) / len(ids), "gained": gain, "lost": loss,
                          "p_mcnemar": pval}, indent=1))


if __name__ == "__main__":
    main()
