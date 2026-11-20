import json, sys, random, torch
from collections import Counter
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
H = {"mode": None, "a": {}, "patch": None}


def hook(li):
    def f(mod, args):
        x = args[0]
        if H["mode"] == "record":
            H["a"][li] = x
            if x.requires_grad:
                x.retain_grad()
        elif H["mode"] == "patch" and li in H["patch"]:
            idx, val = H["patch"][li]
            x = x.clone(); x[0, -1, idx] = val.to(x.device, x.dtype); return (x,)
    return f


def load_model(p):
    m = AutoModelForCausalLM.from_pretrained(p, torch_dtype=torch.bfloat16, attn_implementation="f32").cuda().eval()
    for prm in m.parameters():
        prm.requires_grad_(False)
    for li, l in enumerate(m.model.layers):
        l.mlp.down_proj.register_forward_pre_hook(hook(li))
    return m


def ld_of(logits, i):
    lp = torch.log_softmax(logits[0, -1].float(), -1)
    return lp[items[i]["tt"]] - lp[items[i]["tb"]]


m = load_model(trained_m)
AT = {}
with torch.no_grad():
    for i in use:
        H.update(mode="record", a={})
        m(torch.tensor([seq[i]], device="cuda"))
        AT[i] = torch.stack([H["a"][l][0, -1].float() for l in range(len(H["a"]))]).cpu()
H["mode"] = None
del m
torch.cuda.empty_cache()
m = load_model(base_m)
ATTR, LD0 = {}, {}
for i in use:
    emb = m.model.embed_tokens(torch.tensor([seq[i]], device="cuda")).detach().requires_grad_(True)
    H.update(mode="record", a={})
    ld = ld_of(m(inputs_embeds=emb).logits, i)
    ld.backward()
    L = len(H["a"])
    ab = torch.stack([H["a"][l][0, -1].detach().float() for l in range(L)]).cpu()
    g = torch.stack([H["a"][l].grad[0, -1].float() for l in range(L)]).cpu()
    ATTR[i] = ((AT[i] - ab) * g).flatten()
    LD0[i] = float(ld)
    H.update(mode=None, a={})
N = AT[use[0]].shape[1]


def patched(i, flat):
    p = {}
    lay = flat // N
    for l in lay.unique().tolist():
        sel = flat[lay == l] % N
        p[l] = (sel.cuda(), AT[i][l, sel])
    H.update(mode="patch", patch=p)
    with torch.no_grad():
        v = float(ld_of(m(torch.tensor([seq[i]], device="cuda")).logits, i))
    H["mode"] = None
    return v


random.seed(0)
ids = use[:]
random.shuffle(ids)
halves = [ids[: len(ids) // 2], ids[len(ids) // 2:]]
pair = lambda i: (items[i]["tb"], items[i]["tt"])
rows = []
for a, b in ((0, 1), (1, 0)):
    fit, held = halves[a], halves[b]
    seen_pairs = {pair(i) for i in fit}
    occ = Counter(int(x) for i in fit for x in ATTR[i].argsort(descending=True)[:15])
    mass = sum(ATTR[i] for i in fit)
    pools = {f"in >={c} fit lists": torch.tensor([x for x, n in occ.items() if n >= c]) for c in (3, 2, 1)}
    for M in (100, 300, 1000, 3000, 10000):
        pools[f"top {M} by fit mass"] = mass.argsort(descending=True)[:M]
    for i in held:
        seen = pair(i) in seen_pairs
        order = ATTR[i].argsort(descending=True)
        for k in (10, 30):
            v = patched(i, order[:k])
            rows.append(("free (all 530k)", N * 28, k, seen, v - LD0[i], v > 0))
        for name, P in pools.items():
            if len(P) == 0:
                continue
            within = P[ATTR[i][P].argsort(descending=True)]
            for k in (10, 30):
                v = patched(i, within[:k])
                rows.append((name, len(P), k, seen, v - LD0[i], v > 0))
            v = patched(i, P)
            rows.append((name, len(P), "whole pool", seen, v - LD0[i], v > 0))

nseen = sum(1 for r in rows if r[0] == "free (all 530k)" and r[2] == 10 and r[3])
print(f"{len(use)} positions, 2-fold; held-out positions with a token pair seen in the fit half: {nseen}, novel: {len(use) - nseen}")
print(f"{'pool':22s} {'size':>7s} {'k':>10s} | {'seen choice: gain, flips':>26s} | {'novel choice: gain, flips':>26s}")
names = ["free (all 530k)"] + [f"in >={c} fit lists" for c in (3, 2, 1)] + [f"top {M} by fit mass" for M in (100, 300, 1000, 3000, 10000)]
for name in names:
    for k in (10, 30, "whole pool"):
        sel = [r for r in rows if r[0] == name and r[2] == k]
        if not sel:
            continue
        size = sum(r[1] for r in sel) / len(sel)
        cells = []
        for s in (True, False):
            g = [r for r in sel if r[3] == s]
            cells.append(f"{sum(r[4] for r in g) / max(len(g), 1):+.2f}, {sum(r[5] for r in g):>2d}/{len(g)}")
        print(f"{name:22s} {size:>7.0f} {str(k):>10s} | {cells[0]:>26s} | {cells[1]:>26s}")
