"""Greedy vLLM pass over a problem list; write the ids it already gets right (for llm_editor --skip)."""
import json, sys
sys.path.insert(0, "/home/mason/manual-rl/tools")
from math_eval import make_llm, generate, grade
model, prompts, start, out = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4]
rows = [json.loads(l) for l in open(prompts)][start:]
texts = [t[0] for t in generate(make_llm(model, gpu_mem=0.3), rows)]
ok = [grade(r["answer"], t) for r, t in zip(rows, texts)]
with open(out, "w") as f:
    for r, k in zip(rows, ok):
        if k:
            f.write(r["id"] + "\n")
with open(out + ".jsonl", "w") as f:
    for r, t, k in zip(rows, texts, ok):
        f.write(json.dumps({"id": r["id"], "correct": k, "completion": t}) + "\n")
print(f"{sum(ok)}/{len(rows)} right greedy from index {start}", flush=True)
