"""
usage: diverge.py TOKENIZER_DIR COLLECTED[,COLLECTED..] EVAL_TRAIN_JSONL [EVAL_TRAIN_JSONL..]"""

import json, sys, collections
from transformers import AutoTokenizer

tok = AutoTokenizer.from_pretrained(sys.argv[1])
eps = {}
for path in sys.argv[2].split(","):
    for l in open(path):
        e = json.loads(l)["episode"]
        eps[e["prompt"]["id"]] = e

for ev in sys.argv[3:]:
    outs = {}
    for l in open(ev):
        r = json.loads(l)
        if r["id"] in eps:
            outs[r["id"]] = r
    kinds = collections.Counter()
    detail = []
    for pid, e in eps.items():
        if pid not in outs:
            kinds["missing"] += 1
            continue
        steer = e["completion"]
        steer = [x for x in steer if x < 151643]
        got = outs[pid]["completion"]
        forks = sorted(f["pos"] for f in e["forks"])
        ends = []
        for i in range(len(steer)):
            ends.append(len(tok.decode(steer[:i + 1])))
        st = tok.decode(steer)
        c = next((i for i in range(min(len(st), len(got))) if st[i] != got[i]), None)
        d = None if c is None else next(i for i, x in enumerate(ends) if x > c)
        if d is None:
            k = "same" if len(got) >= len(st) - 2 else "same-prefix-shorter"
        elif d < forks[0]:
            k = "before first edit"
        elif d in forks:
            k = f"at an edit (#{forks.index(d) + 1} of {len(forks)})"
            k = "at first edit" if d == forks[0] else "at a later edit"
        elif d > forks[-1]:
            k = "after last edit"
        else:
            k = "between edits"
        kinds[k] += 1
        detail.append((pid, k, d, forks[:4], outs[pid]["correct"]))
    print(f"== {ev.split('/')[-1]}  ({len(outs)} problems)")
    for k, v in kinds.most_common():
        corr = sum(1 for x in detail if x[1] == k and x[4])
        print(f"   {k:22s} {v:3d}   (graded correct: {corr})")
    early = [x for x in detail if x[1] == "before first edit"]
    if early:
        gaps = sorted(x[3][0] - x[2] for x in early)
        print("   'before first edit': tokens short of the first edit, median", gaps[len(gaps) // 2], "examples", early[:3])
