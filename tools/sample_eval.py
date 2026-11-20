import argparse, json, math, os, subprocess, sys
sys.path.insert(0, os.path.dirname(__file__))


def join(parts, ids, out):
    got = {}
    for p in parts:
        for line in open(p):
            r = json.loads(line)
            assert r["id"] not in got, f"{r['id']} is in two shards"
            got[r["id"]] = line if line.endswith("\n") else line + "\n"
    missing = [i for i in ids if i not in got]
    assert not missing and len(got) == len(ids), f"{len(missing)} problems missing from the shards, {len(got)} results for {len(ids)} problems"
    with open(out + ".tmp", "w") as f:
        f.writelines(got[i] for i in ids)
    os.replace(out + ".tmp", out)
    for p in parts:
        os.remove(p)


def compare(a, b):
    ra, rb = ({r["id"]: r for r in map(json.loads, open(p))} for p in (a, b))
    A, B = ({i: r["frac"] for i, r in d.items()} for d in (ra, rb))
    ids = [i for i in A if i in B]
    chars = None
    if ids and all("chars" in ra[i] and "chars" in rb[i] for i in ids):
        chars = (sum(ra[i]["chars"] for i in ids) / len(ids), sum(rb[i]["chars"] for i in ids) / len(ids))
    d = [B[i] - A[i] for i in ids]
    n = len(d)
    mean = sum(d) / n
    se = math.sqrt(sum((x - mean) ** 2 for x in d) / (n - 1) / n) if n > 1 else 0.0
    return {"n": n, "base": sum(A[i] for i in ids), "acc": sum(B[i] for i in ids), "change": sum(d), "se": se * n,
            "up": sum(x > 0 for x in d), "down": sum(x < 0 for x in d), "chars": chars}


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("score")
    s.add_argument("--model", required=True); s.add_argument("--set", choices=["test", "train"], default="test")
    s.add_argument("--rows"); s.add_argument("--k", type=int, default=4); s.add_argument("--temperature", type=float, default=0.7)
    s.add_argument("--out", required=True); s.add_argument("--gpu-mem", type=float, default=0.8); s.add_argument("--seed", type=int, default=0)
    s.add_argument("--gpus", default=os.environ.get("CUDA_VISIBLE_DEVICES", ""),
                   help="comma-separated GPUs: with more than one, the problems are split over them (default: every GPU the job has)")
    s.add_argument("--shard", default="", help="I/N: only every N-th problem, starting at the I-th (what --gpus starts on each GPU)")
    s.add_argument("--texts", action="store_true", help="also keep every answer's text, in OUT.answers (one line a problem)")
    c = sp.add_parser("compare"); c.add_argument("a"); c.add_argument("b")
    a = ap.parse_args()
    if a.cmd == "compare":
        r = compare(a.a, a.b)
        print(f"{r['n']} problems: expected solved {r['base']:.1f} -> {r['acc']:.1f}, change {r['change']:+.1f} "
              f"(paired standard error {r['se']:.1f}); {r['up']} problems up, {r['down']} down"
              + (f"; mean answer length {r['chars'][0]:.0f} -> {r['chars'][1]:.0f} characters" if r["chars"] else ""))
        return
    from concurrent.futures import ProcessPoolExecutor
    from math_eval import generate, grade, load, make_llm, scored_set
    rows = [json.loads(l) for l in open(a.rows)] if a.rows else scored_set() if a.set == "test" else load(a.set)
    rows = [r for r in rows if r["answer"]]
    gpus = [g for g in a.gpus.split(",") if g]
    if len(gpus) > 1 and not a.shard:
        parts = [f"{a.out}.shard{j}" for j in range(len(gpus))]
        procs = [subprocess.Popen([sys.executable, os.path.abspath(__file__), "score", "--model", a.model, "--k", str(a.k),
                                   "--temperature", str(a.temperature), "--seed", str(a.seed), "--gpu-mem", str(a.gpu_mem),
                                   "--out", parts[j], "--shard", f"{j}/{len(gpus)}", "--gpus", g,
                                   *(["--texts"] if a.texts else []), *(["--rows", a.rows] if a.rows else ["--set", a.set])],
                                  env=dict(os.environ, CUDA_VISIBLE_DEVICES=g)) for j, g in enumerate(gpus)]
        codes = [p.wait() for p in procs]
        if any(codes):
            sys.exit(f"scoring failed on {sum(c != 0 for c in codes)} of {len(gpus)} GPUs (exit codes {codes})")
        if a.texts:
            got = {}
            for p in parts:
                for line in open(p + ".answers"):
                    got[json.loads(line)["id"]] = line
                os.remove(p + ".answers")
            with open(a.out + ".answers", "w") as f:
                f.writelines(got[r["id"]] for r in rows)
        join(parts, [r["id"] for r in rows], a.out)
        return
    if a.shard:
        i, n = map(int, a.shard.split("/"))
        rows = rows[i::n]
    outs = generate(make_llm(a.model, gpu_mem=a.gpu_mem), rows, n=a.k, temperature=a.temperature, seed=a.seed)
    flat_a = [r["answer"] for r, ts in zip(rows, outs) for _ in ts]
    flat_t = [t for ts in outs for t in ts]
    with ProcessPoolExecutor(16) as ex:
        ok = list(ex.map(grade, flat_a, flat_t, chunksize=64))
    with open(a.out + ".tmp", "w") as f:
        k = 0
        for r, ts in zip(rows, outs):
            f.write(json.dumps({"id": r["id"], "frac": sum(ok[k:k + len(ts)]) / len(ts), "k": len(ts),
                                "chars": sum(map(len, ts)) / len(ts),
                                "marks": "".join("1" if x else "0" for x in ok[k:k + len(ts)])}) + "\n")
            k += len(ts)
    if a.texts:
        with open(a.out + ".answers", "w") as f:
            f.writelines(json.dumps({"id": r["id"], "answers": ts}) + "\n" for r, ts in zip(rows, outs))
    os.replace(a.out + ".tmp", a.out)


if __name__ == "__main__":
    main()
