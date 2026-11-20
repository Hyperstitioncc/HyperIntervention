"""
usage: resid_decomp.py BASE TRAINED PIVOT.jsonl FEWNEURONS.jsonl
"""

import json, sys, random, torch
import torch.nn.functional as Fn
from collections import Counter, defaultdict
from safetensors import safe_open
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
H = {"mode": None, "a": {}, "z": {}, "r": {}, "pa": None, "pz": None}


def hooks(m):
    for li, l in enumerate(m.model.layers):
        def a_hook(mod, args, li=li):
            x = args[0]
            if H["mode"] == "record":
                H["a"][li] = x
                if x.requires_grad:
                    x.retain_grad()
            elif H["mode"] == "patch" and li in H["pa"]:
                idx, val = H["pa"][li]
                x = x.clone(); x[0, -1, idx] = val.to(x.device, x.dtype); return (x,)

        def z_hook(mod, args, li=li):
            x = args[0]
            if H["mode"] == "record":
                H["z"][li] = x[0, -1].detach().float()
            elif H["mode"] == "patch" and li in H["pz"]:
                cols, val = H["pz"][li]
                x = x.clone(); x[0, -1, cols] = val.to(x.device, x.dtype); return (x,)

        def r_hook(mod, args, li=li):
            if H["mode"] == "record":
                H["r"][li] = args[0][0, -1].detach().float()
        l.mlp.down_proj.register_forward_pre_hook(a_hook)
        l.self_attn.o_proj.register_forward_pre_hook(z_hook)
        l.post_attention_layernorm.register_forward_pre_hook(r_hook)


def load_model(p):
    m = AutoModelForCausalLM.from_pretrained(p, torch_dtype=torch.bfloat16, attn_implementation="f32").cuda().eval()
    for prm in m.parameters():
        prm.requires_grad_(False)
    hooks(m)
    return m


def ld_of(logits, i):
    lp = torch.log_softmax(logits[0, -1].float(), -1)
    return lp[items[i]["tt"]] - lp[items[i]["tb"]]


def record(m, grad):
    out = {}
    for i in use:
        H.update(mode="record", a={}, z={}, r={})
        if grad:
            emb = m.model.embed_tokens(torch.tensor([seq[i]], device="cuda")).detach().requires_grad_(True)
            ld = ld_of(m(inputs_embeds=emb).logits, i)
            ld.backward()
        else:
            with torch.no_grad():
                ld = ld_of(m(torch.tensor([seq[i]], device="cuda")).logits, i)
        L = len(H["a"])
        out[i] = dict(a=torch.stack([H["a"][l][0, -1].detach().float() for l in range(L)]).cpu(),
                      z=torch.stack([H["z"][l] for l in range(L)]).cpu(),
                      r=torch.stack([H["r"][l] for l in range(L)]).cpu(), ld=float(ld))
        if grad:
            out[i]["g"] = torch.stack([H["a"][l].grad[0, -1].float() for l in range(L)]).cpu()
        H.update(mode=None, a={}, z={}, r={})
    return out


m = load_model(trained_m)
T = record(m, grad=False)
del m
torch.cuda.empty_cache()
m = load_model(base_m)
B = record(m, grad=True)
cfg = m.config
L, NH, HD, N = cfg.num_hidden_layers, cfg.num_attention_heads, cfg.hidden_size // cfg.num_attention_heads, cfg.intermediate_size
Wt = {}
with safe_open(f"{trained_m}/model.safetensors", "pt", device="cuda") as f:
    for l in range(L):
        Wt[("o", l)] = f.get_tensor(f"model.layers.{l}.self_attn.o_proj.weight").float()
        Wt[("d", l)] = f.get_tensor(f"model.layers.{l}.mlp.down_proj.weight").float()
    Wt["emb"] = f.get_tensor("model.embed_tokens.weight")

rows = []
for i in use:
    t, b = T[i], B[i]
    key = ((t["a"] - b["a"]) * b["g"]).flatten().argsort(descending=True)[:10]
    V = torch.zeros(L, cfg.hidden_size, device="cuda")
    for f_ in key.tolist():
        l, n = divmod(f_, N)
        lay = m.model.layers[l]
        r = b["r"][l].cuda()
        D = lay.post_attention_layernorm.weight.float() * torch.rsqrt(r.pow(2).mean() + cfg.rms_norm_eps)
        wg, wu = lay.mlp.gate_proj.weight[n].float(), lay.mlp.up_proj.weight[n].float()
        g, u = wg @ (r * D), wu @ (r * D)
        sg = torch.sigmoid(g)
        V[l] += b["g"][l, n].cuda() * ((sg * (1 + g * (1 - sg))) * u * wg + Fn.silu(g) * wu) * D
    Ua = V.flip(0).cumsum(0).flip(0)
    Um = torch.cat([Ua[1:], torch.zeros_like(Ua[:1])])
    comp = {}
    for j in range(L):
        lay = m.model.layers[j]
        Ob, Ot = lay.self_attn.o_proj.weight.float(), Wt[("o", j)]
        zb, zt = b["z"][j].cuda(), t["z"][j].cuda()
        for h in range(NH):
            s = slice(h * HD, (h + 1) * HD)
            dw = Ot[:, s] @ zt[s] - Ob[:, s] @ zb[s]
            comp[("attn", j, h)] = float(Ua[j] @ dw)
        cb, ct = Um[j] @ lay.mlp.down_proj.weight.float(), Um[j] @ Wt[("d", j)]
        c = (ct * t["a"][j].cuda() - cb * b["a"][j].cuda()).cpu()
        for n in c.abs().topk(300).indices.tolist():
            comp[("mlp", j, n)] = float(c[n])
        comp[("mlp-rest", j, -1)] = float(c.sum()) - sum(float(c[n]) for n in c.abs().topk(300).indices.tolist())
    e = seq[i][-1]
    comp[("emb", 0, 0)] = float(Ua[0] @ (Wt["emb"][e].float() - m.model.embed_tokens.weight[e].float()))
    first_order = float((b["g"].flatten()[key] * (t["a"] - b["a"]).flatten()[key]).sum())
    rows.append(dict(id=i, comp=comp, total=sum(comp.values()), first_order=first_order, key=key.tolist()))

print(f"{len(rows)} contested positions; key neurons = top-10 per position")
tot = [r["total"] for r in rows]
fo = [r["first_order"] for r in rows]
print(f"sum of upstream contributions (linearised through key neurons) mean {sum(tot)/len(tot):+.2f}; "
      f"key neurons' first-order effect from their actual change mean {sum(fo)/len(fo):+.2f}")
attn_s = [sum(v for k, v in r["comp"].items() if k[0] == "attn") for r in rows]
mlp_s = [sum(v for k, v in r["comp"].items() if k[0].startswith("mlp")) for r in rows]
emb_s = [r["comp"][("emb", 0, 0)] for r in rows]
print(f"by type (mean): attention heads {sum(attn_s)/len(rows):+.2f}, MLP neurons {sum(mlp_s)/len(rows):+.2f}, "
      f"embedding {sum(emb_s)/len(rows):+.3f}")
byl = defaultdict(float)
for r in rows:
    for k, v in r["comp"].items():
        byl[(k[0].split("-")[0], k[1])] += v / len(rows)
print("by layer (mean contribution), attn | mlp:")
print("   " + "  ".join(f"L{l}:{byl[('attn', l)]:+.2f}|{byl[('mlp', l)]:+.2f}" for l in range(L)))
shares = {k: [] for k in (1, 3, 10, 30, 100)}
for r in rows:
    vals = sorted((v for k, v in r["comp"].items() if k[0] in ("attn", "mlp")), reverse=True)
    pos = sum(v for v in vals if v > 0)
    for k in shares:
        shares[k].append(sum(vals[:k]) / max(pos, 1e-9))
print("share of the positive contributions held by the top-k individual heads/neurons (median over positions):",
      {k: f"{sorted(v)[len(v)//2]:.0%}" for k, v in shares.items()})
cnt = Counter()
for r in rows:
    for k, v in sorted(r["comp"].items(), key=lambda kv: -kv[1])[:10]:
        if k[0] in ("attn", "mlp"):
            cnt[k] += 1
print("components in a position's top-10 most often:", [(f"{k[0]} L{k[1]} {'h' if k[0]=='attn' else 'n'}{k[2]}", c) for k, c in cnt.most_common(15)])

print("\ncausal check — base, contested position, patch only the top-k upstream components (trained values):")
random.seed(0)
for k in (1, 3, 10, 30, 100, 300):
    for mode in ("top", "random"):
        gains, flips = [], 0
        for r in rows:
            t, b = T[r["id"]], B[r["id"]]
            cands = [c for c in r["comp"] if c[0] in ("attn", "mlp")]
            sel = sorted(cands, key=lambda c: -r["comp"][c])[:k] if mode == "top" else random.sample(cands, k)
            pa, pz = defaultdict(list), defaultdict(list)
            for c in sel:
                (pz if c[0] == "attn" else pa)[c[1]].append(c[2])
            H["pa"] = {l: (torch.tensor(ns).cuda(), t["a"][l, ns]) for l, ns in pa.items()}
            H["pz"] = {}
            for l, hs in pz.items():
                cols = torch.cat([torch.arange(h * HD, (h + 1) * HD) for h in hs])
                H["pz"][l] = (cols.cuda(), t["z"][l, cols])
            H["mode"] = "patch"
            with torch.no_grad():
                v = float(ld_of(m(torch.tensor([seq[r["id"]]], device="cuda")).logits, r["id"]))
            H["mode"] = None
            gains.append(v - b["ld"])
            flips += v > 0
        print(f"   {mode:6s} k={k:>4d}: mean gain {sum(gains)/len(gains):+.2f}, flips {flips}/{len(rows)}")
print("   (for scale: patching the 10 key neurons themselves gave +0.93, 35 flips; all neurons +1.20, 44 flips)")
