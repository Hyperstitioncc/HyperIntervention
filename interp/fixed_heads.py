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

random.seed(0)
ids=[r["id"] for r in rows]; random.shuffle(ids)
halves=[ids[:len(ids)//2], ids[len(ids)//2:]]
R={r["id"]: r for r in rows}
def patch_heads(i, heads):
    t=T[i]; pz=defaultdict(list)
    for (l,h) in heads: pz[l].append(h)
    H["pa"]={}; H["pz"]={}
    for l,hs in pz.items():
        cols=torch.cat([torch.arange(h*HD,(h+1)*HD) for h in hs]); H["pz"][l]=(cols.cuda(), t["z"][l, cols])
    H["mode"]="patch"
    with torch.no_grad(): v=float(ld_of(m(torch.tensor([seq[i]],device="cuda")).logits,i))
    H["mode"]=None; return v
print("fixed head set chosen by top-10 recurrence on one half, patched (trained head outputs -> base, contested pos) on the other half")
for K in (1,2,3,5,10,20,50):
    g=0; fl=0; n=0; gr=0
    for a,bh in ((0,1),(1,0)):
        cnt=Counter()
        for i in halves[a]:
            for k,v in sorted(R[i]["comp"].items(), key=lambda kv:-kv[1])[:10]:
                if k[0]=="attn": cnt[(k[1],k[2])]+=1
        S=[x for x,_ in cnt.most_common(K)]
        Rn=[(random.randrange(L), random.randrange(NH)) for _ in range(K)]
        for i in halves[bh]:
            v=patch_heads(i,S); g+=v-B[i]["ld"]; fl+=v>0; n+=1
            gr+=patch_heads(i,Rn)-B[i]["ld"]
        if K<=5: print("   fold set:", [f"L{l}h{h}" for l,h in S])
    print(f"  K={K:>3d} heads: held-out mean gain {g/n:+.2f}, flips {fl}/{n}; random heads {gr/n:+.2f}")
print("all 784 heads:", end=" ")
g=0; fl=0
for i in ids:
    v=patch_heads(i,[(l,h) for l in range(L) for h in range(NH)]); g+=v-B[i]["ld"]; fl+=v>0
print(f"mean gain {g/len(ids):+.2f}, flips {fl}/{len(ids)}")
