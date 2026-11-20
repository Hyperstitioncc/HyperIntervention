"""
usage: fewneurons.py BASE TRAINED PIVOT.jsonl OUT.jsonl
"""

import json, sys, torch
from transformers import AutoModelForCausalLM, AutoTokenizer, AttentionInterface
from transformers.masking_utils import AttentionMaskInterface, eager_mask
from transformers.models.qwen2.modeling_qwen2 import repeat_kv
sys.path.insert(0, "/home/mason/manual-rl/tools")
from math_eval import TEMPLATE, load

base_m, trained_m, pf, out = sys.argv[1:5]
tok = AutoTokenizer.from_pretrained(base_m)
items = [json.loads(l) for l in open(pf)]
items = [it for it in items if it["base_with_tt"] and not it["trained_with_tb"] and it["retrained_correct"]]
probs = {r["id"]: r for r in load("test")}
seqs = [tok(TEMPLATE.replace("{prompt}", probs[it["id"]]["prompt"]), add_special_tokens=False)["input_ids"] + it["prefix"]
        for it in items]


def attn(module, q, k, v, mask, scaling, dropout=0.0, **kw):  # f32 scores (bf16 eager flips near-ties)
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
            H["rec"][li] = x
            if x.requires_grad:
                x.retain_grad()
        elif H["mode"] == "patch" and li in H["patch"]:
            idx, val = H["patch"][li]
            x = x.clone()
            x[0, -1, idx] = val.to(x.dtype)
            return (x,)
    return f


def load_model(p):
    m = AutoModelForCausalLM.from_pretrained(p, torch_dtype=torch.bfloat16, attn_implementation="f32").cuda().eval()
    for prm in m.parameters():
        prm.requires_grad_(False)
    for li, l in enumerate(m.model.layers):
        l.mlp.down_proj.register_forward_pre_hook(hook(li))
    return m


def ld_of(logits, it):
    lp = torch.log_softmax(logits[0, -1].float(), -1)
    return lp[it["tt"]] - lp[it["tb"]]


m = load_model(trained_m)
A_t = []
for s in seqs:
    H.update(mode="record", rec={})
    with torch.no_grad():
        m(torch.tensor([s], device="cuda"))
    A_t.append(torch.stack([H["rec"][li][0, -1].float().cpu() for li in range(len(m.model.layers))]))
H["mode"] = None
del m
torch.cuda.empty_cache()

m = load_model(base_m)
L, N = A_t[0].shape
KS = [1, 3, 10, 30, 100, 300, 1000, 3000, 10000, 30000, 100000, L * N]
g = torch.Generator().manual_seed(0)
rows = []
for it, s, at in zip(items, seqs, A_t):
    emb = m.model.embed_tokens(torch.tensor([s], device="cuda")).detach().requires_grad_(True)
    H.update(mode="record", rec={})
    ld0 = ld_of(m(inputs_embeds=emb).logits, it)
    ld0.backward()
    ab = torch.stack([H["rec"][li][0, -1].detach().float().cpu() for li in range(L)])
    grad = torch.stack([H["rec"][li].grad[0, -1].float().cpu() for li in range(L)])
    H.update(mode=None, rec={})
    d = at - ab
    scores = {"attr": d * grad, "delta": d.abs(), "random": torch.rand(L, N, generator=g)}

    def patched(flat_idx):
        H["patch"] = {}
        li_all = flat_idx // N
        for li in li_all.unique().tolist():
            sel = flat_idx[li_all == li] % N
            H["patch"][li] = (sel.cuda(), at[li, sel].cuda())
        H["mode"] = "patch"
        with torch.no_grad():
            v = float(ld_of(m(torch.tensor([s], device="cuda")).logits, it))
        H["mode"] = None
        return v

    row = {"id": it["id"], "tb": it["tb_text"], "tt": it["tt_text"], "ld_base": float(ld0),
           "ld_all": patched(torch.arange(L * N)), "attr_pred_total": float((d * grad).sum())}
    for name, sc in scores.items():
        order = sc.flatten().argsort(descending=True) if name != "attr" else sc.flatten().argsort(descending=True)
        row[name] = {k: patched(order[:k]) for k in KS[:-1]}
        if name == "attr":
            row["top_attr"] = [(int(i // N), int(i % N), float(sc.flatten()[i]), float(ab.flatten()[i]), float(at.flatten()[i]))
                               for i in order[:15]]
    rows.append(row)
with open(out, "w") as f:
    for r in rows:
        f.write(json.dumps(r) + "\n")

use = [r for r in rows if r["ld_all"] - r["ld_base"] > 0.3]
print(f"{len(rows)} contested positions; {len(use)} where patching all neurons at that position moves log-odds by > 0.3")
print(f"mean ld: base {sum(r['ld_base'] for r in use)/len(use):+.2f}, all-patched {sum(r['ld_all'] for r in use)/len(use):+.2f}; "
      f"all-patched picks tt in {sum(r['ld_all'] > 0 for r in use)}/{len(use)}")
print(f"{'k neurons':>10s} | " + " | ".join(f"{n:>22s}" for n in ("attr: recovered / flips", "delta", "random")))
for k in KS[:-1]:
    cells = []
    for n in ("attr", "delta", "random"):
        rec = sorted((r[n][k] - r["ld_base"]) / (r["ld_all"] - r["ld_base"]) for r in use)
        med = rec[len(rec) // 2]
        flips = sum(r[n][k] > 0 for r in use)
        cells.append(f"{med:>8.0%} med, {flips:>3d} flip")
    print(f"{k:>10d} | " + " | ".join(f"{c:>22s}" for c in cells))
