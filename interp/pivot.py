import json, sys, gc
sys.path.insert(0, "/home/mason/manual-rl/tools")
from math_eval import make_llm, grade, TEMPLATE, load
from transformers import AutoTokenizer
from vllm import SamplingParams

base_m, trained_m, bf, tf, out = sys.argv[1:6]
tok = AutoTokenizer.from_pretrained(base_m)
L = lambda f: {json.loads(l)["id"]: json.loads(l) for l in open(f)}
B, T = L(bf), L(tf)
probs = {r["id"]: r for r in load("test")}
items = []
for i in B:
    if B[i]["correct"] or not T[i]["correct"]:
        continue
    a = tok(B[i]["completion"], add_special_tokens=False)["input_ids"]
    b = tok(T[i]["completion"], add_special_tokens=False)["input_ids"]
    k = next((j for j in range(min(len(a), len(b))) if a[j] != b[j]), None)
    if k is None:
        continue
    prompt = tok(TEMPLATE.replace("{prompt}", probs[i]["prompt"]), add_special_tokens=False)["input_ids"]
    items.append({"id": i, "answer": probs[i]["answer"], "prompt": prompt, "prefix": b[:k], "tb": a[k], "tt": b[k],
                  "pos": k})
print(len(items), "items", flush=True)
sp = SamplingParams(temperature=0.0, max_tokens=4096, stop_token_ids=[151645], logprobs=20)


def run(model, reqs):
    llm = make_llm(model, gpu_mem=0.25)
    outs = llm.generate([{"prompt_token_ids": r} for r in reqs], sp)
    res = [(o.outputs[0].text, o.outputs[0]) for o in outs]
    del llm
    gc.collect()
    import torch
    torch.cuda.empty_cache()
    return res


r1 = run(base_m, [it["prompt"] + it["prefix"] + [it["tt"]] for it in items])
for it, (text, _) in zip(items, r1):
    it["base_with_tt"] = grade(it["answer"], tok.decode(it["prefix"] + [it["tt"]]) + text)
r2 = run(trained_m, [it["prompt"] + it["prefix"] + [it["tb"]] for it in items] + [it["prompt"] for it in items])
n = len(items)
for it, (text, _), (full, _) in zip(items, r2[:n], r2[n:]):
    it["trained_with_tb"] = grade(it["answer"], tok.decode(it["prefix"] + [it["tb"]]) + text)
    it["retrained_correct"] = grade(it["answer"], full)
    it["retrained_same"] = full.strip() == T[it["id"]]["completion"].strip()
    it["tb_text"], it["tt_text"] = tok.decode([it["tb"]]), tok.decode([it["tt"]])
    it["ctx"] = tok.decode(it["prefix"][-40:])
with open(out, "w") as f:
    for it in items:
        f.write(json.dumps({k: v for k, v in it.items() if k not in ("prompt",)}) + "\n")
piv = [it for it in items if it["base_with_tt"] and not it["trained_with_tb"]]
print(f"retrained reproduces eval answer: {sum(it['retrained_same'] for it in items)}/{n}, still correct "
      f"{sum(it['retrained_correct'] for it in items)}/{n}")
print(f"base flips to right with tt: {sum(it['base_with_tt'] for it in items)}/{n}; trained flips to wrong with tb: "
      f"{sum(not it['trained_with_tb'] for it in items)}/{n}; both (pivotal): {len(piv)}")
