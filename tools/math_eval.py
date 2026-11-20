"""MATH data, eval and comparison for manual-rl experiments (runs in a vLLM venv).

  math_eval.py pick  --model M --n 400 --out prompts.jsonl    train-split problems the model gets wrong
      [--samples 8 --temperature 0.7]                         ...greedy, but solves in >= 1 of K samples
  math_eval.py eval  --model M [--lora DIR] --out DIR          greedy on the test split, graded
  math_eval.py compare A/results.jsonl B/results.jsonl         paired flips between two evals

Training prompts come from the train split and eval from the test split (same distribution, no
shared problems). Both use TEMPLATE, which manual-rl's run.toml must repeat exactly.
"""
import argparse, json, math, os, random, re, sys

SYSTEM = "Please reason step by step, and put your final answer within \\boxed{}."
TEMPLATE = (
    "<|im_start|>system\n" + SYSTEM + "<|im_end|>\n"
    "<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n"
)
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


def load(split):
    from datasets import load_dataset
    rows = []
    for sub in SUBJECTS:
        for i, r in enumerate(load_dataset("EleutherAI/hendrycks_math", sub, split=split)):
            rows.append({"id": f"{split}/{sub}/{i}", "prompt": r["problem"], "answer": last_boxed(r["solution"]),
                         "level": r["level"], "type": r["type"], "solution": r["solution"]})
    return rows


def braced(tex):
    r"""TeX shorthand made explicit: \frac\pi 2 -> \frac{\pi}{2}, \sqrt3 -> \sqrt{3}, \dfrac12 -> \dfrac{1}{2}.
    MATH's reference answers use it and math_verify cannot parse it, so a right answer graded wrong."""
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


def make_llm(model, lora=None, max_tokens=4096, gpu_mem=0.9):
    from vllm import LLM
    # MATH_EVAL_DTYPE=float32 evaluates an f32 checkpoint without rounding its weights to bf16
    kw = dict(model=model, dtype=os.environ.get("MATH_EVAL_DTYPE", "bfloat16"), max_model_len=max_tokens + 2048, seed=0, gpu_memory_utilization=gpu_mem)
    if lora:
        kw.update(enable_lora=True, max_lora_rank=json.load(open(os.path.join(lora, "adapter_config.json")))["r"])
    return LLM(**kw)


def generate(llm, rows, lora=None, max_tokens=4096, n=1, temperature=0.0, seed=0):
    from vllm import SamplingParams
    req = None
    if lora:
        from vllm.lora.request import LoRARequest
        req = LoRARequest("adapter", 1, lora)
    # <|im_end|> ends the template's assistant turn; a base model's generation_config only lists <|endoftext|>
    sp = SamplingParams(n=n, temperature=temperature, max_tokens=max_tokens, seed=seed, stop_token_ids=[151645])
    outs = llm.generate([TEMPLATE.replace("{prompt}", r["prompt"]) for r in rows], sp, lora_request=req)
    return [[c.text for c in o.outputs] for o in outs]


def summarize(res):
    n = len(res)
    acc = sum(r["correct"] for r in res) / n
    out = {"n": n, "acc": acc, "stderr": math.sqrt(acc * (1 - acc) / n), "by_level": {}, "by_type": {}}
    for key, field in (("by_level", "level"), ("by_type", "type")):
        groups = {}
        for r in res:
            groups.setdefault(r[field], []).append(r["correct"])
        out[key] = {k: round(sum(v) / len(v), 4) for k, v in sorted(groups.items())}
    return out


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("pick"); p.add_argument("--model", required=True); p.add_argument("--n", type=int, default=400)
    p.add_argument("--out", required=True); p.add_argument("--seed", type=int, default=0)
    p.add_argument("--levels", default="3,4,5")
    p.add_argument("--samples", type=int, default=0,
                   help="also sample each greedy-wrong problem K times; keep those solved at least once")
    p.add_argument("--temperature", type=float, default=0.7)
    p.add_argument("--keep", choices=["near", "hard"], default="near",
                   help="with --samples: near = solved in >= 1 sample (easiest first); hard = solved in none")
    p.add_argument("--gpu-mem", type=float, default=0.9, help="vLLM share of GPU memory (lower it beside `manual-rl ui`)")
    e = sp.add_parser("eval"); e.add_argument("--model", required=True); e.add_argument("--lora")
    e.add_argument("--out", required=True); e.add_argument("--limit", type=int, default=0)
    e.add_argument("--gpu-mem", type=float, default=0.9)
    c = sp.add_parser("compare"); c.add_argument("a"); c.add_argument("b")
    a = ap.parse_args()

    if a.cmd == "pick":
        levels = {f"Level {x}" for x in a.levels.split(",")}
        rows = [r for r in load("train") if r["answer"] and r["level"] in levels]
        random.Random(a.seed).shuffle(rows)
        rows = rows[:a.n]
        llm = make_llm(a.model, gpu_mem=a.gpu_mem)
        texts = [t[0] for t in generate(llm, rows)]
        wrong = [r for r, t in zip(rows, texts) if not grade(r["answer"], t)]
        keys = ["id", "prompt", "answer", "level", "type"]
        msg = f"{len(wrong)}/{len(rows)} wrong greedy"
        if a.samples:
            samples = generate(llm, wrong, n=a.samples, temperature=a.temperature, seed=a.seed)
            for r, ts in zip(wrong, samples):
                r["solved"] = f"{sum(grade(r['answer'], t) for t in ts)}/{a.samples} at T={a.temperature}"
            hist = {}
            for r in wrong:
                hist[r["solved"]] = hist.get(r["solved"], 0) + 1
            if a.keep == "near":
                wrong = sorted((r for r in wrong if not r["solved"].startswith("0/")),
                               key=lambda r: -int(r["solved"].split("/")[0]))
            else:
                wrong = sorted((r for r in wrong if r["solved"].startswith("0/")), key=lambda r: r["level"])
            keys.append("solved")
            msg += f"; kept {len(wrong)} ({a.keep}) ({dict(sorted(hist.items()))})"
        with open(a.out, "w") as f:
            for r in wrong:
                f.write(json.dumps({k: r[k] for k in keys}) + "\n")
        print(f"{msg} -> {a.out}", file=sys.stderr)

    elif a.cmd == "eval":
        rows = load("test")
        if a.limit:
            rows = rows[:a.limit]
        texts = [t[0] for t in generate(make_llm(a.model, lora=a.lora, gpu_mem=a.gpu_mem), rows, lora=a.lora)]
        os.makedirs(a.out, exist_ok=True)
        res = []
        with open(os.path.join(a.out, "results.jsonl"), "w") as f:
            for r, t in zip(rows, texts):
                x = dict(r, completion=t, correct=grade(r["answer"], t) if r["answer"] else False)
                res.append(x)
                f.write(json.dumps(x) + "\n")
        s = summarize(res)
        s.update(model=a.model, lora=a.lora)
        json.dump(s, open(os.path.join(a.out, "summary.json"), "w"), indent=1)
        print(json.dumps(s, indent=1))

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
