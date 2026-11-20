cd ${WORK_DIR}; echo "NODE $(hostname | cut -c1-4)"
python3 - <<'PY'
import json, glob, os, re
E = "eval-plain-fin-mix1-float32"
def load(f):
    d = {}
    for l in open(f):
        try: r = json.loads(l)
        except Exception: continue
        d[r.get("id")] = r
    return d
def frac(r):
    for k in ("p", "frac", "acc", "mean", "correct"):
        if isinstance(r.get(k), (int, float)): return float(r[k])
    for k in ("marks", "right", "scores"):
        v = r.get(k)
        if isinstance(v, str) and v: return v.count("1") / len(v)
        if isinstance(v, list) and v: return sum(map(float, v)) / len(v)
    return None
base = load(E + "/base-test-sampled-k2-p95.jsonl"); probe = next(iter(base.values()))
print("KEYS", sorted(probe.keys()))
bsum = sum(frac(r) or 0 for r in base.values())
for d in sorted(glob.glob("runs/*")):
    r = os.path.basename(d)
    if not os.path.exists(d + "/state.json") or not os.path.getsize(d + "/agent.jsonl"): continue
    st = json.load(open(d + "/state.json")); V = st["versions"]
    made = {}; ans = {"fork": 0, "val": 0, "pool": 0}; ftok = 0
    for l in open(d + "/agent.jsonl"):
        if not l.strip(): continue
        e = json.loads(l); t = e.get("clock") or 0; took = e.get("took") or []
        for i, (a, res) in enumerate(zip(e.get("actions") or [], e.get("results") or [])):
            k = a.get("action"); res = str(res)
            if k == "fork":
                n = int(a.get("tries") or 1); ans["fork"] += n
                m = re.search(r"from the fork at (\d+) on:\s*(\d+) tokens", res)
                ftok += n * (max(int(m.group(2)) - int(m.group(1)), 0) if m else 300)
            elif k == "validate": ans["val"] += 500 * int(a.get("k") or 4)
            elif k == "eval": ans["val"] += int(a.get("n") or 500) * int(a.get("k") or 4)
            elif k == "pose": ans["fork"] += 4
            elif k == "train":
                vs = re.findall(r"Trained (v\d+) from", res)
                for v in vs:
                    ans["pool"] += 4 * len((V.get(v) or {}).get("pool") or {})
                    if a.get("validate"): ans["val"] += 500 * int(a.get("validate"))
                for v in vs:
                    made[v] = ((t - sum(x or 0 for x in took[i + 1:])) / 60, e.get("own_tokens"), e.get("usd"), dict(ans), ftok)
    b = [x for x in (V["v0"].get("marks") or {}).values() if x]; bv = sum(x.count("1") / len(x) for x in b) / len(b) if b else 0
    hand = json.load(open(d + "/result.json")).get("handed_in") if os.path.exists(d + "/result.json") else st.get("handed")
    if not hand and os.path.exists(d + "/HANDED"): hand = open(d + "/HANDED").read().strip()
    for v in sorted((n for n in V if n[1:].isdigit() and n != "v0"), key=lambda n: int(n[1:])):
        f = "%s/%s-%s-test-sampled-k2-p95.jsonl" % (E, r, v)
        m = [x for x in (V[v].get("marks") or {}).values() if x]
        val = (sum(x.count("1") / len(x) for x in m) / len(m) - bv) * 100 if m else None
        test = tse = None
        if os.path.exists(f):
            T = load(f); ids = [i for i in T if i in base]
            if len(ids) >= 4900:
                dd = [(frac(T[i]) or 0) - (frac(base[i]) or 0) for i in ids]; mu = sum(dd) / len(dd)
                test = mu * 100; tse = (sum((x - mu) ** 2 for x in dd) / (len(dd) - 1) / len(dd)) ** 0.5 * 100
        mk = made.get(v, (None, None, None, None, None))
        print("CURVE " + json.dumps({"s": r, "v": v, "min": None if mk[0] is None else round(mk[0], 1), "tok": mk[1], "usd": mk[2], "val": None if val is None else round(val, 2), "nans": len(m[0]) if m else 0, "test": None if test is None else round(test, 2), "test_se": None if tse is None else round(tse, 3), "parent": V[v].get("parent"), "corr": V[v].get("episodes"), "handed": v == hand, "ans": mk[3], "forktok": mk[4]}))
PY
