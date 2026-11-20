import argparse, json, os, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(__file__))

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {"reasoning": {"type": "string"}, "verdict": {"type": "string", "enum": ["pass", "fail"]}},
    "required": ["reasoning", "verdict"],
    "additionalProperties": False,
}
GRADER_MAX_TOKENS = 64000
GRADER = "openai/gpt-5.6-luna"


def load(path):
    return [json.loads(l) for l in open(path)] if os.path.exists(path) else []


def append(path, rows, lock=threading.Lock()):
    with lock, open(path, "a") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)


def generate(a):
    from vllm import LLM, SamplingParams
    from transformers import AutoConfig, AutoTokenizer
    rows = load(a.rows)
    done = {r["id"] for r in load(a.out)}
    todo = [r for r in rows if r["id"] not in done]
    print(f"{len(rows)} tasks, {len(done)} answered before, {len(todo)} to answer", file=sys.stderr)
    if not todo:
        return
    tok = AutoTokenizer.from_pretrained(a.model)
    ids = [tok(tok.apply_chat_template([{"role": "user", "content": r["prompt"]}], add_generation_prompt=True, tokenize=False),
               add_special_tokens=False)["input_ids"] for r in todo]
    longest = max(map(len, ids))
    window = getattr(AutoConfig.from_pretrained(a.model), "max_position_embeddings", None)
    if window and longest + a.max_new_tokens > window:
        sys.exit(f"the longest prompt ({longest:,} tokens) and --max-new-tokens {a.max_new_tokens:,} exceed the student's {window:,}-token window")
    kw = {"moe_backend": os.environ["STUDENT_MOE_BACKEND"]} if os.environ.get("STUDENT_MOE_BACKEND") else {}
    llm = LLM(model=a.model, dtype=a.dtype, tensor_parallel_size=a.gpus, seed=0, max_model_len=longest + a.max_new_tokens,
              gpu_memory_utilization=a.gpu_mem, enable_prefix_caching=True, **kw)
    sp = SamplingParams(temperature=0.0, max_tokens=a.max_new_tokens)
    t0 = time.time()
    for k in range(0, len(todo), a.chunk):
        outs = llm.generate([{"prompt_token_ids": x} for x in ids[k:k + a.chunk]], sp)
        append(a.out, [{"id": r["id"], "answer": o.outputs[0].text, "finish": o.outputs[0].finish_reason,
                        "prompt_tokens": len(x), "answer_tokens": len(o.outputs[0].token_ids), "model": a.model,
                        "dtype": a.dtype, "max_new_tokens": a.max_new_tokens}
                       for r, x, o in zip(todo[k:], ids[k:], outs)])
        print(f"{min(k + a.chunk, len(todo))}/{len(todo)} answered, {time.time() - t0:.0f}s", file=sys.stderr, flush=True)


def parse_json(text):
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    for i, ch in enumerate(text):
        if ch == "{":
            depth = 0
            for j in range(i, len(text)):
                depth += {"{": 1, "}": -1}.get(text[j], 0)
                if depth == 0:
                    try:
                        return json.loads(text[i:j + 1])
                    except json.JSONDecodeError:
                        break
    raise ValueError(f"No JSON found in grader response: {text[:200]}")


class Grader:
    def __init__(self, model, max_usd, spent=0.0):
        from judge_client import OPENROUTER, post
        self.key = os.environ.get("OPENROUTER_API_KEY") or sys.exit("OPENROUTER_API_KEY is not set")
        self.model, self.max_usd, self.spent, self.lock = model, max_usd, spent, threading.Lock()
        self.url, self.post = OPENROUTER, post

    def over(self):
        return self.spent >= self.max_usd

    def call(self, prompt, schema):
        body = {"model": self.model, "messages": [{"role": "user", "content": prompt}], "max_tokens": GRADER_MAX_TOKENS,
                "usage": {"include": True}}
        if schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "verdict", "strict": True, "schema": VERDICT_SCHEMA}}
        for attempt in range(6):
            try:
                r = self.post(self.url, body, headers={"Authorization": f"Bearer {self.key}"}, timeout=900)
                if "choices" not in r:
                    raise RuntimeError(f"OpenRouter: {str(r)[:400]}")
                break
            except Exception as e:
                if attempt == 5 or not re.search(r"HTTP (429|5\d\d)|timed out|Connection|OpenRouter:", str(e)):
                    raise
                time.sleep(20 * (attempt + 1))
        u = r.get("usage") or {}
        with self.lock:
            self.spent += float(u.get("cost") or 0)
        return r["choices"][0]["message"].get("content") or "", u

    def evaluate(self, prompt):
        err, usage = None, {}
        for schema in (True, False):
            try:
                text, u = self.call(prompt, schema)
                usage = {k: usage.get(k, 0) + (u.get(k) or 0) for k in ("prompt_tokens", "completion_tokens", "cost")}
                v = parse_json(text)
                return str(v.get("verdict", "fail")).lower(), v.get("reasoning", ""), usage
            except Exception as e:
                err = e
        return "error", f"grading error: {type(err).__name__}: {err}", usage


def rubric_prompt(lab):
    return open(os.path.join(lab, "lab_core", "evaluation", "prompts", "rubric_criterion.txt"), encoding="utf-8").read()


def grade_answer(G, template, row, answer, criteria, parallel, done, cut):
    ans = answer.strip()

    def one(c):
        if G.over():
            return
        base = {"id": row["id"], "criterion": c["id"], "title": c["title"], "grader": G.model}
        if not ans:
            rec = {**base, "verdict": "fail", "reasoning": "no agent output found", "usage": {}}
        elif cut:
            rec = {**base, "verdict": "fail", "reasoning": "the answer never ended (cut off at the token limit)", "usage": {}}
        else:
            prompt = template.format(task_description=row["title"], agent_output=f"## Agent Output: {row['deliverable']}\n{ans}",
                                     criterion_title=c["title"], match_criteria=c["match_criteria"])
            verdict, reasoning, usage = G.evaluate(prompt)
            rec = {**base, "verdict": verdict, "reasoning": reasoning, "usage": usage}
        done(rec)
    if criteria:
        one(criteria[0])
        with ThreadPoolExecutor(parallel) as pool:
            list(pool.map(one, criteria[1:]))


def grade(a):
    rows = {r["id"]: r for r in load(a.rows)}
    answers = {r["id"]: r for r in load(a.answers)}
    template = rubric_prompt(a.lab)
    old = load(a.out)
    done = {(v["id"], v["criterion"]) for v in old if v["verdict"] != "error"}
    G = Grader(a.grader, a.max_usd, spent=sum((v.get("usage") or {}).get("cost", 0) for v in old))
    tasks = [i for i in rows if i in answers and any((i, c["id"]) not in done for c in rows[i]["criteria"])]
    print(f"{len(answers)} answers, {len(tasks)} tasks to grade, ${G.spent:.2f} spent before, cap ${a.max_usd}", file=sys.stderr)

    def task(tid):
        todo = [c for c in rows[tid]["criteria"] if (tid, c["id"]) not in done]
        grade_answer(G, template, rows[tid], answers[tid]["answer"], todo, a.parallel, lambda rec: append(a.out, [rec]),
                     answers[tid].get("finish") == "length")
        print(f"{tid}: {len(todo)} criteria graded, ${G.spent:.2f} so far", file=sys.stderr, flush=True)

    with ThreadPoolExecutor(a.tasks) as pool:
        list(pool.map(task, tasks))
    if G.over():
        print(f"stopped at the cap: ${G.spent:.2f} >= ${a.max_usd}; run again with a higher --max-usd to go on", file=sys.stderr)
    print(f"grader spent ${G.spent:.2f} in all", file=sys.stderr)


def scores(rows, verdicts):
    by = {}
    for v in load(verdicts):
        by.setdefault(v["id"], {})[v["criterion"]] = v["verdict"] == "pass"
    return {r["id"]: sum(by.get(r["id"], {}).values()) / len(r["criteria"]) for r in rows}


def compare(a):
    import math
    rows = load(a.rows)
    B, T = scores(rows, a.base), scores(rows, a.trained)
    cut = {x["id"] for x in load(a.answers) if x.get("finish") == "length"} if a.answers else set()

    def line(name, ids):
        d = [T[i] - B[i] for i in ids]
        m = sum(d) / len(d)
        se = math.sqrt(sum((x - m) ** 2 for x in d) / max(1, len(d) - 1) / len(d))
        return (f"{name} ({len(ids)} tasks): mean criterion pass rate {sum(B[i] for i in ids) / len(ids):.1%} -> "
                f"{sum(T[i] for i in ids) / len(ids):.1%} ({m:+.1%}, paired standard error {se:.1%}); "
                f"tasks up {sum(x > 0 for x in d)}, down {sum(x < 0 for x in d)}, same {sum(x == 0 for x in d)}")
    ids = [r["id"] for r in rows]
    print(line("all", ids))
    if a.answers:
        print(f"cut off at the token limit: {len(cut)}")
        print(line("answers that ended", [i for i in ids if i not in cut]))


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    g = sp.add_parser("generate")
    g.add_argument("--rows", required=True); g.add_argument("--model", required=True); g.add_argument("--out", required=True)
    g.add_argument("--max-new-tokens", type=int, default=16384); g.add_argument("--dtype", default="float32")
    g.add_argument("--gpus", type=int, default=1, help="tensor parallel over this many GPUs")
    g.add_argument("--gpu-mem", type=float, default=0.9); g.add_argument("--chunk", type=int, default=64)
    j = sp.add_parser("grade")
    j.add_argument("--rows", required=True); j.add_argument("--answers", required=True); j.add_argument("--out", required=True)
    j.add_argument("--lab", required=True, help="the LAB checkout the rows were built from (its rubric prompt)")
    j.add_argument("--grader", default=GRADER); j.add_argument("--max-usd", type=float, default=25.0)
    j.add_argument("--tasks", type=int, default=8); j.add_argument("--parallel", type=int, default=8)
    c = sp.add_parser("compare")
    c.add_argument("--rows", required=True); c.add_argument("--base", required=True); c.add_argument("--trained", required=True)
    c.add_argument("--answers", help="the trained student's answers: also the answers that ended, alone")
    a = ap.parse_args()
    {"generate": generate, "grade": grade, "compare": compare}[a.cmd](a)


if __name__ == "__main__":
    main()
