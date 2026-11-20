import json, glob, os, sys
for p in sorted(glob.glob("runs/*/state.json")):
    r = p.split("/")[1]; st = json.load(open(p)); V = st["versions"]
    L = [json.loads(l) for l in open(f"runs/{r}/agent.jsonl")]
    f = lambda n, k: sum(x[:k].count("1") / k for x in V[n]["marks"].values())
    rows = []
    for n, v in V.items():
        if n == "v0": continue
        c = v["settings"]
        if v.get("marks"):
            k = min(len(next(iter(v["marks"].values()))), len(next(iter(V["v0"]["marks"].values()))))
            sc = "%+.0f at %d answers" % (f(n, k) - f("v0", k), k)
        else:
            sc = "no score"
        rows.append("%s from %s: %d corr (%d trained tokens), %s lr %s, %s steps, until %s -> %s" % (n, v["parent"], len(v.get("included") or []), v.get("grads", 0), c.get("optimizer"), c.get("lr"), c.get("passes"), c.get("until"), sc))
    tr = [(json.dumps({k: v for k, v in a.items() if k != "action"})[:230], round(t / 60)) for e in L for a, t in zip(e["actions"], e.get("took", [])) if a.get("action") in ("train", "validate")]
    if rows or tr:
        print("==", r, "| live", st["live"], "| trainings (asked, minutes):", tr[-2:])
        for x in rows[-7:]: print("    ", x)
