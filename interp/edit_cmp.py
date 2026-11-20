import json, math, torch
E = torch.load("/home/mason/mathx/interp/edits.pt")
fit, held = set(E["fit_ids"]), set(E["held_ids"])
B = {json.loads(l)["id"]: json.loads(l)["correct"] for l in open("/home/mason/mathx/base-eval/base-test.jsonl")}
print(f"base: {sum(B.values())}/{len(B)}")
for e in ("reader100", "reader1000", "random1000"):
    R = {json.loads(l)["id"]: json.loads(l)["correct"] for l in open(f"/home/mason/mathx/interp/eval-{e}/results.jsonl")}
    ids = [i for i in B if i in R and i not in fit]
    up = sum(1 for i in ids if R[i] and not B[i]); dn = sum(1 for i in ids if B[i] and not R[i])
    hu = sum(1 for i in held if i in R and R[i] and not B[i])
    print(f"{e:11s} params {E['edits'][e]['params']:>9,d} | test excl. {len(fit)} fit problems ({len(ids)}): "
          f"{up - dn:+d} (+{up}/-{dn}), z {(up - dn) / math.sqrt(max(up + dn, 1)):+.1f} | held-out 29 decisions' problems now right: {hu}/29")
