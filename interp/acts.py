import json, sys, torch
from transformers import AutoModelForCausalLM, AutoTokenizer
sys.path.insert(0, "/home/mason/manual-rl/tools")
from math_eval import TEMPLATE, load

base_m, trained_m, control_m, pf = sys.argv[1:5]
ids = sys.argv[5:]
tok = AutoTokenizer.from_pretrained(base_m)
items = {json.loads(l)["id"]: json.loads(l) for l in open(pf)}
probs = {r["id"]: r for r in load("test")}


def acts(model_dir, seqs):
    m = AutoModelForCausalLM.from_pretrained(model_dir, torch_dtype=torch.bfloat16).cuda().eval()
    got, outs = {}, []
    hooks = [l.mlp.down_proj.register_forward_hook(lambda mod, inp, o, li=li: got.__setitem__(li, inp[0][0].float()))
             for li, l in enumerate(m.model.layers)]
    for s in seqs:
        with torch.no_grad():
            logits = m(torch.tensor([s], device="cuda")).logits[0].float()
        outs.append((torch.stack([got[li] for li in range(len(m.model.layers))], 0).cpu(),
                     torch.log_softmax(logits, -1).cpu()))
    for h in hooks:
        h.remove()
    wd = torch.stack([l.mlp.down_proj.weight.float().cpu() for l in m.model.layers])
    wu = m.lm_head.weight.float().cpu() * m.model.norm.weight.float().cpu()
    del m
    torch.cuda.empty_cache()
    return outs, wd, wu


seqs = []
for i in ids:
    it = items[i]
    p = tok(TEMPLATE.replace("{prompt}", probs[i]["prompt"]), add_special_tokens=False)["input_ids"]
    seqs.append(p + it["prefix"])
A = {k: acts(m, seqs) for k, m in (("base", base_m), ("trained", trained_m), ("control", control_m))}
wd, wu = A["base"][1], A["base"][2]


def stats(d):
    flat = d.flatten().abs()
    tot = (flat ** 2).sum()
    s = flat.sort(descending=True).values
    share = lambda k: float((s[:k] ** 2).sum() / tot)
    kurt = float(((flat - flat.mean()) ** 4).mean() / flat.var() ** 2)
    return f"|Δ|₂ {tot.sqrt():.3f}  top10 {share(10):.1%}  top100 {share(100):.1%}  top1000 {share(1000):.1%}  kurtosis {kurt:.0f}"


for n, i in enumerate(ids):
    it = items[i]
    ab, at, ac = A["base"][0][n][0], A["trained"][0][n][0], A["control"][0][n][0]
    lb, lt = A["base"][0][n][1], A["trained"][0][n][1]
    last = ab.shape[1] - 1
    print(f"\n===== {i}  pivot at answer token {it['pos']}: base {it['tb_text']!r} vs trained {it['tt_text']!r}")
    print("   context:", repr(it["ctx"][-160:]))
    for name, l in (("base", lb), ("trained", lt)):
        print(f"   {name:8s} p(base tok) {l[last, it['tb']].exp():.3f}  p(trained tok) {l[last, it['tt']].exp():.3f}")
    dt, dc = (at - ab)[:, last], (ac - ab)[:, last]
    early = list(range(max(0, last - 200), last - 20, 20))
    print("   trained − base @pivot   :", stats(dt))
    print("   control − base @pivot   :", stats(dc))
    for e in early[:3]:
        print(f"   trained − base @pos-{last - e:3d}:", stats((at - ab)[:, e]))
    per_layer = dt.norm(dim=1)
    print("   per-layer |Δ| (trained):", " ".join(f"{x:.2f}" for x in per_layer.tolist()))
    flat = dt.flatten()
    top = flat.abs().topk(12).indices
    for t in top.tolist():
        li, ni = divmod(t, dt.shape[1])
        eff = wu @ wd[li][:, ni]
        up = [tok.decode([x]) for x in eff.topk(6).indices.tolist()]
        dn = [tok.decode([x]) for x in (-eff).topk(4).indices.tolist()]
        dlogit = float(eff[it['tt']] - eff[it['tb']])
        print(f"   L{li:2d} n{ni:5d}  base {ab[li, last, ni]:+.3f} → trained {at[li, last, ni]:+.3f} (Δ {flat[t]:+.3f})"
              f"  direct logit(tt−tb)/unit {dlogit:+.3f}  pushes up {up}  down {dn}")
