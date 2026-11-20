import json, sys
sys.path.insert(0, "/home/mason/manual-rl/tools")
from math_eval import make_llm, generate, grade
rows = [json.loads(l) for l in open("/home/mason/mathx/pick-random.jsonl")][:60]
out = {}
for name, m in [("base", "/home/mason/models/qwen2.5-7b"), ("instruct", "/home/mason/models/qwen2.5-7b-instruct")]:
    llm = make_llm(m, gpu_mem=0.3)
    texts = [t[0] for t in generate(llm, rows)]
    ok = [grade(r["answer"], t) for r, t in zip(rows, texts)]
    out[name] = texts
    lens = sorted(len(t) for t in texts)
    print(name, "correct", sum(ok), "/", len(rows), "boxed", sum("boxed" in t for t in texts), "median chars", lens[len(lens) // 2], "max", lens[-1], flush=True)
    del llm
    import gc, torch; gc.collect(); torch.cuda.empty_cache()
    json.dump({"texts": texts, "ok": ok}, open(f"/home/mason/mathx/vcheck-{name}.json", "w"))
