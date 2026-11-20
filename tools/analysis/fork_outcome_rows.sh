cd ${WORK_DIR}; echo "NODE $(hostname | cut -c1-4)"
python3 - <<'PY'
import json, glob, os, re
OUT = set()
for p in sorted(glob.glob("runs/*/agent.jsonl")):
    r = p.split("/")[1]
    if r in OUT or not os.path.getsize(p): continue
    base = {}; F = []
    for l in open(p):
        if not l.strip(): continue
        e = json.loads(l)
        for a, res in zip(e.get("actions") or [], e.get("results") or []):
            res = str(res); k = a.get("action")
            if k == "open":
                m = re.search(r"got it right in (\d+) of (\d+) sampled answers", res)
                if m: base[a.get("idx")] = int(m.group(1)) / int(m.group(2))
            elif k == "fork":
                m = re.search(r"(\d+) of (\d+) match the reference", res); q = re.search(r"\((\d+)% of the way through the answer\) had probability ([\d.]+)%", res)
                if m and a.get("idx") in base and a.get("idx") < 2000: F.append((base[a["idx"]], int(m.group(1)), int(m.group(2)), int(q.group(1)) if q else None))
    if not F: continue
    n = sum(f[2] for f in F); ok = sum(f[1] for f in F); b = sum(f[0] * f[2] for f in F) / n
    z = [f for f in F if f[0] == 0]; nz = sum(f[2] for f in z); okz = sum(f[1] for f in z)
    print("LIFT " + json.dumps({"s": r, "forks": len(F), "cont": n, "right": ok, "base": round(b, 4), "zero_forks": len(z), "zero_cont": nz, "zero_right": okz}))
PY
