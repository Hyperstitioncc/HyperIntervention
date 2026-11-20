import argparse, json, os, random, shutil, sys
sys.path.insert(0, os.path.dirname(__file__))
from math_eval import TEMPLATE, grade, load, make_llm, generate


def random_adapter(src, dst, seed):
    import torch
    from safetensors.torch import load_file, save_file
    os.makedirs(dst, exist_ok=True)
    shutil.copy(os.path.join(src, "adapter_config.json"), dst)
    t = load_file(os.path.join(src, "adapter_model.safetensors"))
    g = torch.Generator().manual_seed(seed)
    for k in t:
        if "lora_B" in k:
            r = torch.randn(t[k].shape, generator=g, dtype=torch.float32)
            t[k] = (r * (t[k].float().norm() / r.norm())).to(t[k].dtype).contiguous()
    save_file(t, os.path.join(dst, "adapter_model.safetensors"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--adapter", action="append", default=[], help="name=PEFT dir (repeatable)")
    ap.add_argument("--random", type=int, default=2, help="random-direction controls built from the first adapter")
    ap.add_argument("--n", type=int, default=1500); ap.add_argument("--levels", default="3,4,5")
    ap.add_argument("--seed", type=int, default=0); ap.add_argument("--gpu-mem", type=float, default=0.7)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    levels = {f"Level {x}" for x in a.levels.split(",")}
    rows = [r for r in load("train") if r["answer"] and r["level"] in levels]
    random.Random(a.seed).shuffle(rows)
    rows = rows[:a.n]

    arms = [tuple(x.split("=", 1)) for x in a.adapter]
    for k in range(a.random):
        d = os.path.join(a.out, f"random{k + 1}")
        random_adapter(arms[0][1], d, seed=1000 + k)
        arms.append((f"random{k + 1}", d))

    llm = make_llm(a.model, lora=arms[0][1] if arms else None, gpu_mem=a.gpu_mem)
    correct = {}
    for i, (name, path) in enumerate([("base", None)] + arms):
        if path:
            from vllm import SamplingParams
            from vllm.lora.request import LoRARequest
            outs = llm.generate([TEMPLATE.replace("{prompt}", r["prompt"]) for r in rows],
                                SamplingParams(temperature=0.0, max_tokens=4096), lora_request=LoRARequest(name, i, path))
            texts = [o.outputs[0].text for o in outs]
        else:
            texts = [t[0] for t in generate(llm, rows)]
        correct[name] = [grade(r["answer"], t) for r, t in zip(rows, texts)]
        with open(os.path.join(a.out, f"{name}.jsonl"), "w") as f:
            for r, t, c in zip(rows, texts, correct[name]):
                f.write(json.dumps({"id": r["id"], "correct": c, "completion": t}) + "\n")
        print(f"{name}: {sum(correct[name])}/{len(rows)} correct", file=sys.stderr, flush=True)

    base = correct["base"]
    rep = {"n": len(rows), "base_correct": sum(base), "arms": {}}
    for name in correct:
        if name == "base":
            continue
        c = correct[name]
        up = [rows[i]["id"] for i in range(len(rows)) if c[i] and not base[i]]
        down = [rows[i]["id"] for i in range(len(rows)) if base[i] and not c[i]]
        rep["arms"][name] = {"correct": sum(c), "wrong_to_right": len(up), "right_to_wrong": len(down),
                             "of_base_wrong": len(rows) - sum(base), "of_base_right": sum(base),
                             "up_ids": up, "down_ids": down}
    json.dump(rep, open(os.path.join(a.out, "report.json"), "w"), indent=1)
    for name, r in rep["arms"].items():
        print(f"{name:10s} correct {r['correct']}/{rep['n']} (base {rep['base_correct']}):  "
              f"wrong->right {r['wrong_to_right']}/{r['of_base_wrong']}   right->wrong {r['right_to_wrong']}/{r['of_base_right']}")


if __name__ == "__main__":
    main()
