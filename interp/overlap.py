import json, itertools
from collections import Counter
F = {json.loads(l)["id"]: json.loads(l) for l in open("/home/mason/mathx/interp/fewneurons.jsonl")}
use = [i for i in F if F[i]["ld_all"] - F[i]["ld_base"] > 0.3]
for k in (10, 15):
    S = {i: set((l, n) for l, n, *_ in F[i]["top_attr"][:k]) for i in use}
    cnt = Counter(x for s in S.values() for x in s)
    pairs = [len(S[a] & S[b]) for a, b in itertools.combinations(use, 2)]
    print(f"top-{k} per position, {len(use)} positions: {len(use) * k} slots, {len(cnt)} distinct neurons")
    print(f"   neurons in >=2 positions: {sum(c >= 2 for c in cnt.values())}, >=3: {sum(c >= 3 for c in cnt.values())}, "
          f">=5: {sum(c >= 5 for c in cnt.values())}, >=10: {sum(c >= 10 for c in cnt.values())}")
    print(f"   intersection of all {len(use)}: {len(set.intersection(*S.values()))}")
    print(f"   pairs of positions sharing >=1 neuron: {sum(p > 0 for p in pairs) / len(pairs):.0%}; mean shared per pair "
          f"{sum(pairs) / len(pairs):.2f} (chance {k * k / 530432:.5f})")
    print("   most shared:", [(f"L{l} n{n}", c) for (l, n), c in cnt.most_common(8)])
    sh = [sum(cnt[x] >= 3 for x in S[i]) / k for i in use]
    print(f"   per position, fraction of its top-{k} recurring in >=3 positions: median {sorted(sh)[len(sh) // 2]:.0%}")
