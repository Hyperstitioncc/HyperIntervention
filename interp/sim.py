import json, itertools
from collections import defaultdict
F = {json.loads(l)["id"]: json.loads(l) for l in open("/home/mason/mathx/interp/fewneurons.jsonl")}
P = {json.loads(l)["id"]: json.loads(l) for l in open("/home/mason/mathx/interp/pivot.jsonl")}
use = [i for i in F if F[i]["ld_all"] - F[i]["ld_base"] > 0.3]
K = 15
S = {i: set((l, n) for l, n, *_ in F[i]["top_attr"][:K]) for i in use}
def kind(i):
    tb, tt = P[i]["tb_text"].strip(), P[i]["tt_text"].strip()
    if P[i]["pos"] == 0:
        return "opening word"
    if any(c in tb + tt for c in "\\$(){}[]") and not any(ch.isalpha() and ch not in "()" for ch in (tb + tt).replace("\\", "")):
        return "latex/format"
    if any(ch.isdigit() for ch in tb + tt) or len(tb + tt) <= 4 and (tb + tt).isupper():
        return "math content"
    return "wording"
pairs = []
for a, b in itertools.combinations(use, 2):
    ov = len(S[a] & S[b])
    pairs.append(dict(a=a, b=b, ov=ov,
        same_tokens=(P[a]["tb"], P[a]["tt"]) == (P[b]["tb"], P[b]["tt"]),
        same_kind=kind(a) == kind(b), kinds=tuple(sorted((kind(a), kind(b)))),
        same_subject=a.split("/")[1] == b.split("/")[1],
        both_start=P[a]["pos"] < 5 and P[b]["pos"] < 5))
def mean(xs): xs = list(xs); return sum(xs) / len(xs) if xs else float("nan")
print(f"{len(use)} tokens, {len(pairs)} pairs; mean overlap of top-{K} sets {mean(p['ov'] for p in pairs):.2f}")
for f in ("same_tokens", "same_kind", "same_subject", "both_start"):
    y = [p["ov"] for p in pairs if p[f]]; n = [p["ov"] for p in pairs if not p[f]]
    print(f"   {f:13s}: yes {mean(y):.2f} (n={len(y)})  no {mean(n):.2f} (n={len(n)})")
print("by kind pair:")
g = defaultdict(list)
for p in pairs: g[p["kinds"]].append(p["ov"])
for k, v in sorted(g.items(), key=lambda kv: -mean(kv[1])):
    print(f"   {k[0]:12s} x {k[1]:12s}: mean overlap {mean(v):.2f} (n={len(v)})")
print("kinds:", {k: sum(kind(i) == k for i in use) for k in ("opening word", "latex/format", "math content", "wording")})
print("\nhighest-overlap pairs:")
for p in sorted(pairs, key=lambda p: -p["ov"])[:14]:
    fa = lambda i: f"{P[i]['tb_text']!r}→{P[i]['tt_text']!r} @{P[i]['pos']} …{P[i]['ctx'][-45:]!r}"
    print(f"   {p['ov']:2d} | {fa(p['a'])}\n      | {fa(p['b'])}")
