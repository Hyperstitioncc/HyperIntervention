import argparse, glob, json, os, re, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--episodes", required=True, help="comma-separated collected.jsonl files")
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", default=os.path.join(HERE, "../experiments/math/batch.toml"))
    ap.add_argument("--lrs", default="3e-7,1e-6,3e-6")
    ap.add_argument("--stages", default="5,15", help="cumulative epochs at which to grade")
    ap.add_argument("--binary", default=os.path.join(HERE, "../target/release/manual-rl"))
    ap.add_argument("--base", default=os.path.expanduser("~/models/qwen2.5-7b-instruct"))
    ap.add_argument("--near", default=os.path.expanduser("~/mathx/pick-near-ui.jsonl"))
    ap.add_argument("--keep-final", action="store_true",
                    help="keep each lr's final checkpoint (its exported weights; the optimizer state is deleted)")
    ap.add_argument("--eval-out", default=os.path.expanduser("~/mathx/editor1-eval"), help="holds the cached base results")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    batch = os.path.join(a.out, "batch.jsonl")
    seen = set()
    with open(batch, "w") as f:
        for path in a.episodes.split(","):
            for line in open(path):
                e = json.loads(line)
                pid = e["episode"]["prompt"]["id"]
                if pid not in seen:
                    seen.add(pid)
                    f.write(line)
    forced = sum(len(json.loads(l)["episode"]["forks"]) for l in open(batch))
    print(f"batch: {len(seen)} problems, {forced} forced tokens", file=sys.stderr, flush=True)

    base_cfg = open(a.config).read()
    stages = [int(x) for x in a.stages.split(",")]
    for lr in a.lrs.split(","):
        run = os.path.join(a.out, f"lr{lr}")
        done = 0
        for ep in stages:
            cfg = os.path.join(a.out, f"lr{lr}-{ep}.toml")
            text = re.sub(r"(?m)^lr = .*$", f"lr = {lr}", base_cfg)
            text = re.sub(r"(?m)^epochs = .*$", f"epochs = {ep - done}", text)
            open(cfg, "w").write(text)
            res = subprocess.run([a.binary, "train", cfg, "--episodes", batch, "--out", run],
                                 check=True, capture_output=True, text=True)
            stats = json.loads(res.stdout)
            cks = sorted(glob.glob(os.path.join(run, "ckpt-*")))
            for old in cks[:-1]:
                shutil.rmtree(old)
            tag = f"{os.path.basename(os.path.normpath(a.out))}-lr{lr}-ep{ep}"
            subprocess.run([sys.executable, os.path.join(HERE, "eval_watch.py"), "one", "--ckpt", cks[-1], "--tag", tag,
                            "--episodes", batch, "--base", a.base, "--near", a.near, "--out", a.eval_out],
                           check=True)
            rep = json.load(open(os.path.join(a.eval_out, f"{tag}.json")))
            line = {"lr": float(lr), "epochs": ep, "problems": len(seen), "forced_tokens": forced,
                    "forced_logp_before": stats.get("forced_logp_before"), "forced_logp_after": stats.get("forced_logp_after"),
                    "eval": rep}
            with open(os.path.join(a.out, "sweep.jsonl"), "a") as f:
                f.write(json.dumps(line) + "\n")
            t = rep["arms"]["trained"]["test"]
            c = rep["arms"]["control"]["test"]
            print(f"lr {lr} epochs {ep}: forced logp {stats.get('forced_logp_before'):.2f} -> {stats.get('forced_logp_after'):.2f}; "
                  f"test {t['acc'] - t['base']:+d} (+{t['up']}/-{t['down']}), control {c['acc'] - c['base']:+d}",
                  file=sys.stderr, flush=True)
            done = ep
        cks = sorted(glob.glob(os.path.join(run, "ckpt-*")))
        for ck in cks[:-1] if a.keep_final else cks:
            shutil.rmtree(ck)
        if a.keep_final and cks:
            for f in ("optimizer.safetensors", "trainable.safetensors"):
                if os.path.exists(os.path.join(cks[-1], f)):
                    os.remove(os.path.join(cks[-1], f))


if __name__ == "__main__":
    main()
