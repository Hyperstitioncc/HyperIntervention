import json, random
from collections import Counter
F = {json.loads(l)["id"]: json.loads(l) for l in open("/home/mason/mathx/interp/fewneurons.jsonl")}
use = [i for i in F if F[i]["ld_all"] - F[i]["ld_base"] > 0.3]
for k in (10, 15):
    S = {i: [((l, n), s) for l, n, s, *_ in F[i]["top_attr"][:k]] for i in use}
    cnt = Counter(x for s in S.values() for x, _ in s)
    hist = Counter(cnt.values())
    print(f"\ntop-{k}: {len(cnt)} distinct; how many positions each neuron appears in -> number of neurons:")
    print("   " + ", ".join(f"{c}x: {hist[c]}" for c in sorted(hist)))
    f1, f2 = hist[1], hist[2]
    print(f"   Chao1 estimate of the pool these lists draw from: {len(cnt) + f1 * f1 / (2 * f2):.0f}")
    curve = {}
    for _ in range(200):
        o = use[:]; random.shuffle(o); seen = set()
        for j, i in enumerate(o, 1):
            seen |= {x for x, _ in S[i]}
            curve.setdefault(j, []).append(len(seen))
    print("   union after n positions:", ", ".join(f"{n}: {sum(curve[n]) / len(curve[n]):.0f}" for n in (1, 5, 10, 20, 30, 40, 57)))
    print(f"   new neurons added by the last 10 positions: {sum(curve[57]) / 200 - sum(curve[47]) / 200:.0f} "
          f"(of {10 * k} slots)")
    tot = {"1": 0.0, "2-4": 0.0, "5+": 0.0}
    for i in use:
        for x, s in S[i]:
            c = cnt[x]
            tot["1" if c == 1 else "2-4" if c < 5 else "5+"] += s
    z = sum(tot.values())
    print("   share of top-k attribution mass by neuron recurrence:", {g: f"{v / z:.0%}" for g, v in tot.items()})
