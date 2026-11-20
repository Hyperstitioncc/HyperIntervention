"""
usage: shared.py BASE TRAINED PIVOT.jsonl FEWNEURONS.jsonl
"""

import json, sys, random, torch
from collections import Counter
from transformers import AutoModelForCausalLM, AutoTokenizer, AttentionInterface
from transformers.masking_utils import AttentionMaskInterface, eager_mask
from transformers.models.qwen2.modeling_qwen2 import repeat_kv
sys.path.insert(0, "/home/mason/manual-rl/tools")
from math_eval import TEMPLATE, load

base_m, trained_m, pf, ff = sys.argv[1:5]
tok = AutoTokenizer.from_pretrained(base_m)
F = {json.loads(l)["id"]: json.loads(l) for l in open(ff)}
use = [i for i in F if F[i]["ld_all"] - F[i]["ld_base"] > 0.3]
items = {json.loads(l)["id"]: json.loads(l) for l in open(pf)}
probs = {r["id"]: r for r in load("test")}
seq = {i: tok(TEMPLATE.replace("{prompt}", probs[i]["prompt"]), add_special_tokens=False)["input_ids"] + items[i]["prefix"]
       for i in use}


def attn(module, q, k, v, mask, scaling, dropout=0.0, **kw):
    k, v = repeat_kv(k, module.num_key_value_groups), repeat_kv(v, module.num_key_value_groups)
    w = torch.matmul(q.float(), k.float().transpose(2, 3)) * scaling
    if mask is not None:
        w = w + mask
    w = torch.softmax(w, dim=-1)
    return torch.matmul(w, v.float()).to(q.dtype).transpose(1, 2).contiguous(), w


AttentionInterface.register("f32", attn)
AttentionMaskInterface.register("f32", eager_mask)
H = {"mode": None, "rec": {}, "patch": None}


def hook(li):
    def f(mod, args):
        x = args[0]
        if H["mode"] == "record":
            H["rec"][li] = x[0].float().cpu()
        elif H["mode"] == "patch" and li in H["patch"]:
            idx, val = H["patch"][li]
            x = x.clone()
            x[0, -1, idx] = val.to(x.dtype)
            return (x,)
    return f


def load_model(p):
    m = AutoModelForCausalLM.from_pretrained(p, torch_dtype=torch.bfloat16, attn_implementation="f32").cuda().eval()
    for li, l in enumerate(m.model.layers):
        l.mlp.down_proj.register_forward_pre_hook(hook(li))
    return m


def record(m, ids):
    out = {}
    for i in ids:
        H.update(mode="record", rec={})
        with torch.no_grad():
            m(torch.tensor([seq[i]], device="cuda"))
        out[i] = torch.stack([H["rec"][li] for li in range(len(H["rec"]))])
    H["mode"] = None
    return out


m = load_model(trained_m)
AT = record(m, use)
del m
torch.cuda.empty_cache()
m = load_model(base_m)
AB = record(m, use)
wd = torch.stack([l.mlp.down_proj.weight.float().cpu() for l in m.model.layers])
wu = m.lm_head.weight.float().cpu() * m.model.norm.weight.float().cpu()


def ld(i, patch=None):
    H.update(mode="patch" if patch else None, patch=patch)
    with torch.no_grad():
        lp = torch.log_softmax(m(torch.tensor([seq[i]], device="cuda")).logits[0, -1].float(), -1)
    H["mode"] = None
    it = items[i]
    return float(lp[it["tt"]] - lp[it["tb"]])


def patch_set(i, neurons):
    p = {}
    for (l, n) in neurons:
        p.setdefault(l, []).append(n)
    return {l: (torch.tensor(ns).cuda(), AT[i][l, -1, ns].cuda()) for l, ns in p.items()}


random.seed(0)
order = use[:]
random.shuffle(order)
halves = [order[: len(order) // 2], order[len(order) // 2:]]
print(f"{len(use)} positions, 2-fold: choose the shared set on one half, test on the other")
print(f"{'set size':>8s} | {'held-out mean gain':>18s} {'flips':>7s} | {'same-size random-set gain':>25s}")
for size in (1, 3, 5, 10, 20, 50, 100):
    gain, flips, rgain, n = 0.0, 0, 0.0, 0
    for a, b in ((0, 1), (1, 0)):
        cnt = Counter((l, nn) for i in halves[a] for (l, nn, *_) in F[i]["top_attr"][:10])
        S = [x for x, _ in cnt.most_common(size)]
        R = [(random.randrange(28), random.randrange(18944)) for _ in range(size)]
        for i in halves[b]:
            base = F[i]["ld_base"]
            v = ld(i, patch_set(i, S))
            gain += v - base
            flips += v > 0
            rgain += ld(i, patch_set(i, R)) - base
            n += 1
    print(f"{size:>8d} | {gain / n:>+18.2f} {flips:>4d}/{n} | {rgain / n:>+25.2f}")
print(f"(for scale: all 530k neurons at that position: mean gain {sum(F[i]['ld_all'] - F[i]['ld_base'] for i in use) / len(use):+.2f})")

cnt = Counter((l, nn) for i in use for (l, nn, *_) in F[i]["top_attr"][:10])
print("\nmost shared neurons (in a position's top-10 by attribution):")
for (l, n), c in cnt.most_common(8):
    rms = torch.cat([AB[i][l, :, n] for i in use]).pow(2).mean().sqrt()
    d_piv = torch.tensor([float(AT[i][l, -1, n] - AB[i][l, -1, n]) for i in use]) / rms
    d_all = torch.cat([AT[i][l, :, n] - AB[i][l, :, n] for i in use]) / rms
    eff = wu @ wd[l][:, n]
    up = [tok.decode([x]) for x in eff.topk(8).indices.tolist()]
    dn = [tok.decode([x]) for x in (-eff).topk(8).indices.tolist()]
    print(f"  L{l} n{n}: top-10 at {c}/{len(use)} positions; base rms {rms:.3f}; training shift at contested pos "
          f"mean {d_piv.mean():+.2f} rms (same sign at {float((d_piv.sign() == d_piv.mean().sign()).float().mean()):.0%}); "
          f"over all positions mean {d_all.mean():+.2f} rms\n      writes up {up}\n      writes down {dn}")
