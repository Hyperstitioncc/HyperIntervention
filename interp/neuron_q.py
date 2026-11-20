"""
usage: neuron_q.py BASE TRAINED PIVOT.jsonl FEWNEURONS.jsonl
"""

import json, sys, random, torch
import torch.nn.functional as Fn
from transformers import AutoModelForCausalLM, AutoTokenizer, AttentionInterface
from transformers.masking_utils import AttentionMaskInterface, eager_mask
from transformers.models.qwen2.modeling_qwen2 import repeat_kv
sys.path.insert(0, "/home/mason/manual-rl/tools")
from math_eval import TEMPLATE, load

base_m, trained_m, pf, ff = sys.argv[1:5]
tok = AutoTokenizer.from_pretrained(base_m)
FW = {json.loads(l)["id"]: json.loads(l) for l in open(ff)}
use = [i for i in FW if FW[i]["ld_all"] - FW[i]["ld_base"] > 0.3]
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
H = {"mode": None, "act": {}, "res": {}, "patch": None}


def act_hook(li):
    def f(mod, args):
        x = args[0]
        if H["mode"] == "record":
            H["act"][li] = x
            if x.requires_grad:
                x.retain_grad()
        elif H["mode"] == "patch" and li in H["patch"]:
            idx, val = H["patch"][li]
            x = x.clone()
            x[0, -1, idx] = val.to(x.device, x.dtype)
            return (x,)
    return f


def res_hook(li):
    def f(mod, args):
        if H["mode"] == "record":
            H["res"][li] = args[0][0, -1].detach().float().cpu()
    return f


def load_model(p):
    m = AutoModelForCausalLM.from_pretrained(p, torch_dtype=torch.bfloat16, attn_implementation="f32").cuda().eval()
    for prm in m.parameters():
        prm.requires_grad_(False)
    for li, l in enumerate(m.model.layers):
        l.mlp.down_proj.register_forward_pre_hook(act_hook(li))
        l.post_attention_layernorm.register_forward_pre_hook(res_hook(li))
    return m


def ld_out(logits, i):
    lp = torch.log_softmax(logits[0, -1].float(), -1)
    return lp[items[i]["tt"]] - lp[items[i]["tb"]]


def record(m):
    out = {}
    for i in use:
        emb = m.model.embed_tokens(torch.tensor([seq[i]], device="cuda")).detach().requires_grad_(True)
        H.update(mode="record", act={}, res={})
        ld = ld_out(m(inputs_embeds=emb).logits, i)
        ld.backward()
        L = len(H["act"])
        out[i] = dict(a=torch.stack([H["act"][l][0, -1].detach().float().cpu() for l in range(L)]),
                      g=torch.stack([H["act"][l].grad[0, -1].float().cpu() for l in range(L)]),
                      r=torch.stack([H["res"][l] for l in range(L)]), ld=float(ld))
        H.update(mode=None, act={}, res={})
    return out


def preacts(m, R):
    G, U = [], []
    for l, layer in enumerate(m.model.layers):
        r = R[l].cuda()
        n = r * torch.rsqrt(r.pow(2).mean() + m.config.rms_norm_eps) * layer.post_attention_layernorm.weight.float()
        G.append((layer.mlp.gate_proj.weight.float() @ n).cpu())
        U.append((layer.mlp.up_proj.weight.float() @ n).cpu())
    return torch.stack(G), torch.stack(U)


def ld_patched(m, i, flat, vals):
    N = vals.shape[1]
    p = {}
    lay = flat // N
    for l in lay.unique().tolist():
        sel = flat[lay == l] % N
        p[l] = (sel.cuda(), vals[l, sel])
    H.update(mode="patch", patch=p)
    with torch.no_grad():
        v = float(ld_out(m(torch.tensor([seq[i]], device="cuda")).logits, i))
    H["mode"] = None
    return v


m = load_model(trained_m)
T = record(m)
for i in use:
    T[i]["G"], T[i]["U"] = preacts(m, T[i]["r"])
del m
torch.cuda.empty_cache()
m = load_model(base_m)
B = record(m)
for i in use:
    B[i]["G"], B[i]["U"] = preacts(m, B[i]["r"])
    B[i]["Gx"], B[i]["Ux"] = preacts(m, T[i]["r"])
del m
torch.cuda.empty_cache()
m = load_model(trained_m)
for i in use:
    T[i]["Gx"], T[i]["Ux"] = preacts(m, B[i]["r"])
L, N = B[use[0]]["a"].shape
act = lambda g, u: Fn.silu(g) * u

KS = [1, 3, 10, 30, 100, 300, 1000, 3000, 10000]
print(f"{len(use)} contested positions (where all-neuron patching moves base by > 0.3)")
print("\n1) NECESSITY — trained model, revert top-k neurons to base values (ranked by (a_t-a_b)*grad in trained)")
nec = {k: [] for k in KS + ["all"]}
for i in use:
    t, b = T[i], B[i]
    score = ((t["a"] - b["a"]) * t["g"]).flatten()
    order = score.argsort(descending=True)
    for k in KS:
        nec[k].append(ld_patched(m, i, order[:k], b["a"]))
    nec["all"].append(ld_patched(m, i, torch.arange(L * N), b["a"]))
ldt = [T[i]["ld"] for i in use]
ldb = [B[i]["ld"] for i in use]
print(f"   clean trained mean ld {sum(ldt)/len(ldt):+.2f}, picks trained token {sum(x > 0 for x in ldt)}/{len(use)}; "
      f"clean base {sum(ldb)/len(ldb):+.2f}")
for k in KS + ["all"]:
    v = nec[k]
    print(f"   revert {str(k):>6s}: mean ld {sum(v)/len(v):+.2f}, still picks trained token {sum(x > 0 for x in v)}/{len(use)}")
del m
torch.cuda.empty_cache()

m = load_model(base_m)
print("\n2) UNIQUENESS — base model, patch disjoint rank bands of the base-side attribution ranking (trained values)")
bands = [(0, 10), (10, 20), (20, 30), (30, 40), (40, 50), (50, 100), (100, 200)]
uni = {bd: [] for bd in bands}
for i in use:
    t, b = T[i], B[i]
    order = ((t["a"] - b["a"]) * b["g"]).flatten().argsort(descending=True)
    for bd in bands:
        uni[bd].append(ld_patched(m, i, order[bd[0]:bd[1]], t["a"]) - b["ld"])
for bd in bands:
    v = uni[bd]
    print(f"   ranks {bd[0]+1:>3d}-{bd[1]:<3d}: mean gain {sum(v)/len(v):+.2f}, "
          f"flips {sum(B[i]['ld'] + x > 0 for i, x in zip(use, v))}/{len(use)}")

print("\n3) GENERAL SET — one fixed set by summed base-side attribution on half the positions, tested on the other half")
random.seed(0)
order_ids = use[:]
random.shuffle(order_ids)
halves = [order_ids[: len(use) // 2], order_ids[len(use) // 2:]]
for K in [10, 30, 100, 300, 1000, 3000, 10000, 30000, 100000]:
    gain, flips, n = 0.0, 0, 0
    for a, bh in ((0, 1), (1, 0)):
        tot = sum(((T[i]["a"] - B[i]["a"]) * B[i]["g"]) for i in halves[a]).flatten()
        S = tot.argsort(descending=True)[:K]
        for i in halves[bh]:
            v = ld_patched(m, i, S, T[i]["a"])
            gain += v - B[i]["ld"]
            flips += v > 0
            n += 1
    print(f"   K={K:>6d}: held-out mean gain {gain/n:+.2f}, flips {flips}/{n}")
print(f"   (all 530k: mean gain {sum(FW[i]['ld_all'] - FW[i]['ld_base'] for i in use)/len(use):+.2f}, "
      f"flips {sum(FW[i]['ld_all'] > 0 for i in use)}/{len(use)})")

print("\n4) MECHANISM — patch into base, for the top-k neurons (base-side attribution) or all neurons, values from:")
variants = {
    "trained (recomputed)":         lambda t, b: act(t["G"], t["U"]),
    "trained weights, base resid":  lambda t, b: act(t["Gx"], t["Ux"]),
    "base weights, trained resid":  lambda t, b: act(b["Gx"], b["Ux"]),
    "trained gate row only":        lambda t, b: act(t["Gx"], b["U"]),
    "trained up row only":          lambda t, b: act(b["G"], t["Ux"]),
    "base (recomputed; null)":      lambda t, b: act(b["G"], b["U"]),
}
for k in (10, 30, "all"):
    print(f"   top-{k}:")
    for name, fn in variants.items():
        gains, flips = [], 0
        for i in use:
            t, b = T[i], B[i]
            order = ((t["a"] - b["a"]) * b["g"]).flatten().argsort(descending=True)
            sel = order[:k] if k != "all" else torch.arange(L * N)
            v = ld_patched(m, i, sel, fn(t, b))
            gains.append(v - b["ld"])
            flips += v > 0
        print(f"      {name:30s} mean gain {sum(gains)/len(gains):+.2f}, flips {flips}/{len(use)}")
fw, fr = [], []
for i in use:
    t, b = T[i], B[i]
    order = ((t["a"] - b["a"]) * b["g"]).flatten().argsort(descending=True)[:10]
    d = (act(t["G"], t["U"]) - act(b["G"], b["U"])).flatten()[order]
    dw = (act(t["Gx"], t["Ux"]) - act(b["G"], b["U"])).flatten()[order]
    dr = (act(b["Gx"], b["Ux"]) - act(b["G"], b["U"])).flatten()[order]
    fw += (dw / d).tolist()
    fr += (dr / d).tolist()
med = lambda x: sorted(x)[len(x) // 2]
print(f"   top-10 neurons' activation change: median share from weights alone {med(fw):.0%}, from residual alone {med(fr):.0%}")
