import argparse, glob, json, os, random, shutil, subprocess, sys, time
sys.path.insert(0, os.path.dirname(__file__))
from math_eval import grade, load

HERE = os.path.abspath(__file__)


def train_rows(n=1500, seed=0, levels="3,4,5"):
    lv = {f"Level {x}" for x in levels.split(",")}
    rows = [r for r in load("train") if r["answer"] and r["level"] in lv]
    random.Random(seed).shuffle(rows)
    return rows[:n]


def rows_for(which):
    return load("test") if which == "test" else train_rows()


def arm(a):
    from math_eval import make_llm, generate
    from concurrent.futures import ProcessPoolExecutor
    rows = [r for r in rows_for(a.set) if r["answer"]]
    texts = [t[0] for t in generate(make_llm(a.model, gpu_mem=a.gpu_mem), rows)]
    with ProcessPoolExecutor(16) as ex:
        ok = list(ex.map(grade, [r["answer"] for r in rows], texts, chunksize=32))
    with open(a.out + ".tmp", "w") as f:
        for r, t, c in zip(rows, texts, ok):
            f.write(json.dumps({"id": r["id"], "correct": c, "completion": t}) + "\n")
    os.replace(a.out + ".tmp", a.out)


def run_arm(model, which, out, gpu_mem):
    if os.path.exists(out):
        return
    subprocess.run([sys.executable, HERE, "arm", "--model", model, "--set", which, "--out", out,
                    "--gpu-mem", str(gpu_mem)], check=True)


def results(path):
    return {r["id"]: r["correct"] for r in map(json.loads, open(path))}


def model_dir(ckpt, dst):
    os.makedirs(dst, exist_ok=True)
    for f in os.listdir(ckpt):
        if f == "model.safetensors" or (f.endswith((".json", ".txt", ".model")) and f != "state.json"):
            if not os.path.exists(os.path.join(dst, f)):
                os.link(os.path.join(ckpt, f), os.path.join(dst, f))
    return dst


def control_dir(base, trained, dst, seed):
    import torch
    from safetensors import safe_open
    from safetensors.torch import save_file
    os.makedirs(dst, exist_ok=True)
    for f in os.listdir(trained):
        if f.endswith((".json", ".txt", ".model")) and not os.path.exists(os.path.join(dst, f)):
            shutil.copy(os.path.join(trained, f), dst)
    where = {}
    for shard in glob.glob(os.path.join(base, "*.safetensors")):
        with safe_open(shard, "pt") as h:
            for k in h.keys():
                where[k] = shard
    g = torch.Generator().manual_seed(seed)
    out, dn, bn = {}, 0.0, 0.0
    with safe_open(os.path.join(trained, "model.safetensors"), "pt") as t:
        for k in t.keys():
            w1 = t.get_tensor(k).float()
            with safe_open(where[k], "pt") as h:
                w0 = h.get_tensor(k).float()
            d = (w1 - w0).norm()
            r = torch.randn(w0.shape, generator=g)
            out[k] = (w0 + r * (d / r.norm())).to(t.get_tensor(k).dtype).contiguous()
            dn += float(d) ** 2
            bn += float(w0.norm()) ** 2
    save_file(out, os.path.join(dst, "model.safetensors"), metadata={"format": "pt"})
    return (dn / bn) ** 0.5


def flips(base, arm_, ids):
    up = sum(1 for i in ids if arm_[i] and not base[i])
    down = sum(1 for i in ids if base[i] and not arm_[i])
    return {"n": len(ids), "base": sum(base[i] for i in ids), "acc": sum(arm_[i] for i in ids), "up": up, "down": down}


def note(url, text):
    from llm_editor import Server
    print(text, file=sys.stderr, flush=True)
    try:
        Server(url)("note", text=text)
    except Exception as e:
        print(f"(note failed: {e})", file=sys.stderr)


def trained_ids(run, round_):
    p = os.path.join(run, "episodes.jsonl")
    if not os.path.exists(p):
        return set()
    return {e["episode"]["prompt"]["id"] for e in map(json.loads, open(p)) if e["round"] < round_}


def setup(a):
    os.makedirs(a.out, exist_ok=True)
    base = {s: os.path.join(a.out, f"base-{s}.jsonl") for s in ("test", "train")}
    for s in base:
        run_arm(a.base, s, base[s], a.gpu_mem)
    B = {s: results(base[s]) for s in base}
    print(f"base: test {sum(B['test'].values())}/{len(B['test'])}, train {sum(B['train'].values())}/{len(B['train'])}",
          file=sys.stderr, flush=True)
    return B, {json.loads(l)["id"] for l in open(a.near)}


def evaluate(a, B, near, ck, tag, trained, seed):
    t0 = time.time()
    work = os.path.join(a.out, f"work-{tag}")
    model = model_dir(ck, os.path.join(work, "trained"))
    size = control_dir(a.base, model, os.path.join(work, "control"), seed=seed)
    rep = {"tag": tag, "ckpt": ck, "relative_change": size, "arms": {}}
    for name in ("trained", "control"):
        R = {}
        for s in ("test", "train"):
            run_arm(os.path.join(work, name), s, os.path.join(a.out, f"{tag}-{name}-{s}.jsonl"), a.gpu_mem)
            R[s] = results(os.path.join(a.out, f"{tag}-{name}-{s}.jsonl"))
        ids = list(B["train"])
        rep["arms"][name] = {
            "test": flips(B["test"], R["test"], list(B["test"])),
            "train_trained": flips(B["train"], R["train"], [i for i in ids if i in trained]),
            "train_near_untrained": flips(B["train"], R["train"], [i for i in ids if i in near and i not in trained]),
            "train_other": flips(B["train"], R["train"], [i for i in ids if i not in near and i not in trained]),
        }
    rep["secs"] = round(time.time() - t0)
    json.dump(rep, open(os.path.join(a.out, f"{tag}.json"), "w"), indent=1)
    with open(os.path.join(a.out, "summary.jsonl"), "a") as f:
        f.write(json.dumps(rep) + "\n")
    shutil.rmtree(work)

    def fmt(x):
        return f"{x['acc'] - x['base']:+d} (+{x['up']}/−{x['down']})"
    t, c = rep["arms"]["trained"], rep["arms"]["control"]
    note(a.url, f"eval {tag} (Δweights {size:.1e} rel, {rep['secs'] // 60} min): "
         f"MATH test {t['test']['acc']}/{t['test']['n']} vs base {t['test']['base']}: trained {fmt(t['test'])}, "
         f"random control {fmt(c['test'])} · near-miss untrained: trained {fmt(t['train_near_untrained'])}, "
         f"control {fmt(c['train_near_untrained'])} · trained-on problems ({t['train_trained']['n']}): "
         f"{fmt(t['train_trained'])} · other train: trained {fmt(t['train_other'])}, control {fmt(c['train_other'])}")
    return rep


def watch(a):
    B, near = setup(a)
    while True:
        cks = sorted(glob.glob(os.path.join(a.run, "ckpt-*")))
        todo = [c for c in cks if not os.path.exists(os.path.join(a.out, f"round-{int(c[-5:])}.json"))]
        if not todo:
            time.sleep(30)
            continue
        ck = todo[-1]
        n = int(ck[-5:])
        try:
            evaluate(a, B, near, ck, f"round-{n}", trained_ids(a.run, n), seed=n)
        except FileNotFoundError:
            time.sleep(30)


def one(a):
    B, near = setup(a)
    trained = {json.loads(l)["episode"]["prompt"]["id"] for l in open(a.episodes)}
    evaluate(a, B, near, a.ckpt, a.tag, trained, seed=a.seed)


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd")
    w = sp.add_parser("watch")
    w.add_argument("--run", required=True); w.add_argument("--base", required=True)
    w.add_argument("--near", required=True, help="the editor's problem list")
    w.add_argument("--out", required=True); w.add_argument("--gpu-mem", type=float, default=0.3)
    w.add_argument("--url", default="http://127.0.0.1:8765")
    o = sp.add_parser("one")
    o.add_argument("--ckpt", required=True); o.add_argument("--tag", required=True)
    o.add_argument("--episodes", required=True, help="the episodes it was trained on")
    o.add_argument("--base", required=True); o.add_argument("--near", required=True)
    o.add_argument("--out", required=True); o.add_argument("--gpu-mem", type=float, default=0.3)
    o.add_argument("--seed", type=int, default=0); o.add_argument("--url", default="http://127.0.0.1:8765")
    r = sp.add_parser("arm")
    r.add_argument("--model", required=True); r.add_argument("--set", choices=["test", "train"], required=True)
    r.add_argument("--out", required=True); r.add_argument("--gpu-mem", type=float, default=0.3)
    a = ap.parse_args()
    {"arm": arm, "one": one, "watch": watch}[a.cmd](a)


if __name__ == "__main__":
    main()
