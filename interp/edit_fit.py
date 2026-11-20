import json, sys, random, math, torch
import torch.nn.functional as Fn
from transformers import AutoModelForCausalLM, AutoTokenizer, AttentionInterface
from transformers.masking_utils import AttentionMaskInterface, eager_mask
from transformers.models.qwen2.modeling_qwen2 import repeat_kv
sys.path.insert(0, "/home/mason/manual-rl/tools")
from math_eval import TEMPLATE, load, grade

base_m, trained_m, pf, ff, out = sys.argv[1:6]
tok = AutoTokenizer.from_pretrained(base_m)
FW = {json.loads(l)["id"]: json.loads(l) for l in open(ff)}
use = [i for i in FW if FW[i]["ld_all"] - FW[i]["ld_base"] > 0.3]
items = {json.loads(l)["id"]: json.loads(l) for l in open(pf)}
probs = {r["id"]: r for r in load("test")}
pid = {i: tok(TEMPLATE.replace("{prompt}", probs[i]["prompt"]), add_special_tokens=False)["input_ids"] for i in use}
seq = {i: pid[i] + items[i]["prefix"] for i in use}
random.seed(0)
ids = use[:]
random.shuffle(ids)
FIT, HELD = ids[: len(ids) // 2], ids[len(ids) // 2:]


def attn(module, q, k, v, mask, scaling, dropout=0.0, **kw):
    k, v = repeat_kv(k, module.num_key_value_groups), repeat_kv(v, module.num_key_value_groups)
    w = torch.matmul(q.float(), k.float().transpose(2, 3)) * scaling
    if mask is not None:
        w = w + mask
    w = torch.softmax(w, dim=-1)
    return torch.matmul(w, v.float()).to(q.dtype).transpose(1, 2).contiguous(), w


AttentionInterface.register("f32", attn)
AttentionMaskInterface.register("f32", eager_mask)
H = {"rec": False, "a": {}, "z": {}, "edit": None}


def install(m):
    for li, l in enumerate(m.model.layers):
        def a_hook(mod, args, li=li):
            if H["rec"]:
                x = args[0]
                H["a"][li] = x
                if x.requires_grad:
                    x.retain_grad()

        def z_hook(mod, args, li=li):
            if H["rec"]:
                x = args[0]
                H["z"][li] = x
                if x.requires_grad:
                    x.retain_grad()

        def gate_out(mod, args, o, li=li):
            e = H["edit"]
            if e and li in e.get("g", {}):
                idx, dw = e["g"][li]
                o = o.clone()
                o[..., idx] = (o[..., idx].float() + args[0].float() @ dw.T).to(o.dtype)
                return o

        def up_out(mod, args, o, li=li):
            e = H["edit"]
            if e and li in e.get("u", {}):
                idx, dw = e["u"][li]
                o = o.clone()
                o[..., idx] = (o[..., idx].float() + args[0].float() @ dw.T).to(o.dtype)
                return o

        def o_out(mod, args, o, li=li):
            e = H["edit"]
            if e and li in e.get("o", {}):
                cols, dw = e["o"][li]
                return (o.float() + args[0][..., cols].float() @ dw.T).to(o.dtype)
        l.mlp.down_proj.register_forward_pre_hook(a_hook)
        l.self_attn.o_proj.register_forward_pre_hook(z_hook)
        l.mlp.gate_proj.register_forward_hook(gate_out)
        l.mlp.up_proj.register_forward_hook(up_out)
        l.self_attn.o_proj.register_forward_hook(o_out)


def load_model(p):
    m = AutoModelForCausalLM.from_pretrained(p, torch_dtype=torch.bfloat16, attn_implementation="f32").cuda().eval()
    for prm in m.parameters():
        prm.requires_grad_(False)
    install(m)
    return m


def lp_last(m, s):
    return torch.log_softmax(m(torch.tensor([s], device="cuda")).logits[0].float(), -1).clamp_min(-1e4)


m = load_model(trained_m)
TA, TZ, TLP = {}, {}, {}
with torch.no_grad():
    for i in use:
        H.update(rec=True, a={}, z={})
        lp = torch.log_softmax(m(torch.tensor([seq[i]], device="cuda")).logits[0].float(), -1).clamp_min(-1e4)
        if i in HELD:
            TLP[i] = lp[len(pid[i]) - 1: len(seq[i]) - 1].cpu()
        TA[i] = torch.stack([H["a"][l][0] for l in range(len(H["a"]))]).cpu()
        TZ[i] = torch.stack([H["z"][l][0, -1].float() for l in range(len(H["z"]))]).cpu()
        H.update(rec=False, a={}, z={})
del m
torch.cuda.empty_cache()
m = load_model(base_m)
cfg = m.config
L, N, NH = cfg.num_hidden_layers, cfg.intermediate_size, cfg.num_attention_heads
HD = cfg.hidden_size // NH
BA, attr_n, attr_h = {}, torch.zeros(L, N), torch.zeros(L, NH)
for i in use:
    emb = m.model.embed_tokens(torch.tensor([seq[i]], device="cuda")).detach().requires_grad_(True)
    H.update(rec=True, a={}, z={})
    lp = torch.log_softmax(m(inputs_embeds=emb).logits[0, -1].float(), -1)
    ld = lp[items[i]["tt"]] - lp[items[i]["tb"]]
    ld.backward()
    a = torch.stack([H["a"][l][0].detach().float() for l in range(L)]).cpu()
    BA[i] = a.to(torch.bfloat16)
    if i in FIT:
        ga = torch.stack([H["a"][l].grad[0, -1].float() for l in range(L)]).cpu()
        gz = torch.stack([H["z"][l].grad[0, -1].float() for l in range(L)]).cpu()
        zb = torch.stack([H["z"][l][0, -1].detach().float() for l in range(L)]).cpu()
        attr_n += (TA[i][:, -1].float() - a[:, -1]) * ga
        attr_h += ((TZ[i] - zb) * gz).view(L, NH, HD).sum(-1)
    H.update(rec=False, a={}, z={})
rms = torch.sqrt(sum(BA[i][:, len(pid[i]) - 1:].float().pow(2).sum(1) for i in use) /
                 sum(BA[i][:, len(pid[i]) - 1:].shape[1] for i in use)).clamp_min(1e-3)


def contested(m, edit):
    H["edit"] = edit
    lds, kls = {}, []
    with torch.no_grad():
        for i in HELD:
            lp = lp_last(m, seq[i])
            lds[i] = float(lp[-1, items[i]["tt"]] - lp[-1, items[i]["tb"]])
            ordinary = slice(len(pid[i]) - 1, len(seq[i]) - 1)
            if BLP[i].shape[0]:
                kls.append(float((BLP[i].exp() * (BLP[i] - lp[ordinary])).sum(-1).mean()))
    H["edit"] = None
    return lds, sum(kls) / len(kls)


BLP = {}
with torch.no_grad():
    for i in HELD:
        BLP[i] = lp_last(m, seq[i])[len(pid[i]) - 1: len(seq[i]) - 1]
base_ld, _ = contested(m, None)


def fit(kind, sel, steps=200, lr=1e-4):
    tgt = sel if kind == "reader" else TGT
    by_l = {}
    for (l, n) in tgt:
        by_l.setdefault(l, []).append(n)
    tl = {l: torch.tensor(ns) for l, ns in by_l.items()}
    edit, params = {}, []
    if kind == "reader":
        edit["g"], edit["u"] = {}, {}
        for l, ns in tl.items():
            dg = torch.zeros(len(ns), cfg.hidden_size, device="cuda", requires_grad=True)
            du = torch.zeros(len(ns), cfg.hidden_size, device="cuda", requires_grad=True)
            edit["g"][l], edit["u"][l] = (ns.cuda(), dg), (ns.cuda(), du)
            params += [dg, du]
    else:
        edit["o"] = {}
        hb = {}
        for (l, h) in sel:
            hb.setdefault(l, []).append(h)
        for l, hs in hb.items():
            cols = torch.cat([torch.arange(h * HD, (h + 1) * HD) for h in hs]).cuda()
            do = torch.zeros(cfg.hidden_size, len(cols), device="cuda", requires_grad=True)
            edit["o"][l] = (cols, do)
            params.append(do)
    opt = torch.optim.Adam(params, lr=lr)
    npar = sum(p.numel() for p in params)
    for step in range(steps):
        i = FIT[step % len(FIT)]
        H.update(edit=edit, rec=True, a={}, z={})
        m(torch.tensor([seq[i]], device="cuda"))
        ans = slice(len(pid[i]) - 1, len(seq[i]))
        loss = 0.0
        for l, ns in tl.items():
            a = H["a"][l][0, ans][:, ns.cuda()].float()
            t = TA[i][l, ans][:, ns].float().cuda()
            loss = loss + (((a - t) / rms[l, ns].cuda()) ** 2).mean() * len(ns)
        loss = loss / len(tgt)
        H.update(rec=False, a={}, z={}, edit=None)
        if step == 0:
            loss0 = float(loss)
        if not torch.isfinite(loss):
            print(f"      non-finite loss at step {step}; stopping this fit", flush=True)
            break
        opt.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm_(params, 1.0)
        opt.step()
    for d in edit.values():
        for l in d:
            d[l] = (d[l][0], d[l][1].detach())
    return edit, npar, (loss0, float(loss))


def report(name, edit, npar, losses):
    loss = losses[1]
    lds, kl = contested(m, edit)
    gain = [lds[i] - base_ld[i] for i in HELD]
    flips = sum(lds[i] > 0 for i in HELD)
    print(f"   {name:34s} params {npar:>10,d}  fit-loss {losses[0]:6.3f}->{loss:6.3f}  held-out gain {sum(gain)/len(gain):+.2f}  "
          f"flips {flips}/{len(HELD)}  side-effect KL {kl:.4f}", flush=True)
    return dict(name=name, params=npar, gain=sum(gain) / len(gain), flips=flips, kl=kl)


rows = []
print(f"{len(use)} examples: fit on {len(FIT)}, held out {len(HELD)}")
kh = [i for i in HELD if BLP[i].shape[0]]
tkl = sum(float((BLP[i].exp() * (BLP[i] - TLP[i].cuda())).sum(-1).mean()) for i in kh) / len(kh)
print(f"   full trained model on held-out: side-effect KL {tkl:.4f}")
print(f"   base held-out mean ld {sum(base_ld.values())/len(HELD):+.2f}, picks trained token {sum(v > 0 for v in base_ld.values())}/{len(HELD)}")
order_n = attr_n.flatten().argsort(descending=True)
order_h = attr_h.flatten().argsort(descending=True)
TGT = [(int(f // N), int(f % N)) for f in order_n[:300]]
for K in (10, 30, 100, 300, 1000):
    sel = [(int(f // N), int(f % N)) for f in order_n[:K]]
    rows.append(report(f"reader: gate/up rows of {K} neurons", *fit("reader", sel)))
    rsel = [(random.randrange(L), random.randrange(N)) for _ in range(K)]
    rows.append(report(f"  random reader, {K} neurons", *fit("reader", rsel)))
for h in (1, 3, 10, 30):
    sel = [(int(f // NH), int(f % NH)) for f in order_h[:h]]
    rows.append(report(f"supplier: o_proj of {h} heads", *fit("supplier", sel)))
    rsel = [(random.randrange(L), random.randrange(NH)) for _ in range(h)]
    rows.append(report(f"  random supplier, {h} heads", *fit("supplier", rsel)))
print("   top heads by fit-half attribution:", [f"L{int(f // NH)}h{int(f % NH)}" for f in order_h[:10]])
json.dump(rows, open(out, "w"))
