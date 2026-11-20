"""
usage: acts2.py BASE TRAINED CONTROL PIVOT.jsonl ID [ID ...]
"""

import json, sys, torch
from transformers import AutoModelForCausalLM, AutoTokenizer
sys.path.insert(0, "/home/mason/manual-rl/tools")
from math_eval import TEMPLATE, load

base_m, trained_m, control_m, pf = sys.argv[1:5]
ids = sys.argv[5:]
tok = AutoTokenizer.from_pretrained(base_m)
items = {json.loads(l)["id"]: json.loads(l) for l in open(pf)}
probs = {r["id"]: r for r in load("test")}
sys_len = len(tok(TEMPLATE.split("{prompt}")[0], add_special_tokens=False)["input_ids"])


def acts(model_dir, seqs):
    m = AutoModelForCausalLM.from_pretrained(model_dir, torch_dtype=torch.bfloat16).cuda().eval()
    got, outs = {}, []
    hs = [l.mlp.down_proj.register_forward_hook(lambda mod, inp, o, li=li: got.__setitem__(li, inp[0][0].float().cpu()))
          for li, l in enumerate(m.model.layers)]
    for s in seqs:
        with torch.no_grad():
            m(torch.tensor([s], device="cuda"))
        outs.append(torch.stack([got[li] for li in range(len(m.model.layers))], 0))
    for h in hs:
        h.remove()
    wd = torch.stack([l.mlp.down_proj.weight.float().cpu() for l in m.model.layers])
    wu = m.lm_head.weight.float().cpu() * m.model.norm.weight.float().cpu()
    del m
    torch.cuda.empty_cache()
    return outs, wd, wu


seqs, prompt_lens = [], []
for i in ids:
    p = tok(TEMPLATE.replace("{prompt}", probs[i]["prompt"]), add_special_tokens=False)["input_ids"]
    seqs.append(p + items[i]["prefix"])
    prompt_lens.append(len(p))
(AB, wd, wu), (AT, _, _), (AC, _, _) = (acts(m, seqs) for m in (base_m, trained_m, control_m))


def cos(a, b):
    return torch.nn.functional.cosine_similarity(a, b, dim=-1)


def summary(z):
    a = z.flatten().abs()
    s = a.sort(descending=True).values
    tot = (s ** 2).sum()
    return (f"median |rel| {a.median():.3f}  frac>1: {(a > 1).float().mean():.2e}  frac>3: {(a > 3).float().mean():.2e}  "
            f"top100 share {(s[:100] ** 2).sum() / tot:.1%}")


for n, i in enumerate(ids):
    it = items[i]
    ab, at, ac = AB[n], AT[n], AC[n]
    T = ab.shape[1]
    piv = T - 1
    lo = sys_len  # skip the shared system prompt (attention-sink positions with huge activations)
    rms = ab[:, lo:].pow(2).mean(1).sqrt().clamp_min(1e-3)
    ordinary = list(range(max(lo, prompt_lens[n]), piv)) or list(range(lo, piv))
    zt = (at - ab) / rms[:, None]
    zc = (ac - ab) / rms[:, None]
    print(f"\n===== {i}: base {it['tb_text']!r} vs trained {it['tt_text']!r}  (answer token {it['pos']}, "
          f"{len(ordinary)} ordinary answer positions before it)")
    print("   context: ..." + repr(it["ctx"][-120:]))
    ct, cc = cos(ab[:, piv], at[:, piv]), cos(ab[:, piv], ac[:, piv])
    co = torch.stack([cos(ab[:, q], at[:, q]) for q in ordinary]).mean(0) if ordinary else ct
    fmt = lambda v: " ".join(f"{x:.4f}" for x in v[[0, 4, 8, 12, 16, 20, 24, 26, 27]].tolist())
    print("   per-layer cosine (layers 0,4,8,12,16,20,24,26,27):")
    print("     trained vs base @pivot     :", fmt(ct))
    print("     trained vs base @ordinary  :", fmt(co), "(mean over ordinary positions)")
    print("     control vs base @pivot     :", fmt(cc))
    print("   relative change, all 530k neurons:")
    print("     trained @pivot   :", summary(zt[:, piv]))
    if ordinary:
        q = ordinary[len(ordinary) // 2]
        print(f"     trained @ordinary (answer pos {q - prompt_lens[n]}):", summary(zt[:, q]))
    print("     control @pivot   :", summary(zc[:, piv]))
    zp = zt[:, piv]
    base_ord = zt[:, ordinary].abs().mean(1) if ordinary else torch.ones_like(zp)
    top = zp.flatten().abs().topk(10).indices.tolist()
    print("   top neurons by |relative change| at the pivot:")
    for t in top:
        li, ni = divmod(t, zp.shape[1])
        eff = wu @ wd[li][:, ni]
        up = [tok.decode([x]) for x in eff.topk(5).indices.tolist()]
        print(f"     L{li:2d} n{ni:5d}  base {ab[li, piv, ni]:+8.3f} → trained {at[li, piv, ni]:+8.3f}  (rms {rms[li, ni]:.3f}, "
              f"rel {zp[li, ni]:+.2f}; specificity {zp[li, ni].abs() / base_ord[li, ni].clamp_min(1e-3):.1f}x)  "
              f"logit(tt−tb)/unit {float(eff[it['tt']] - eff[it['tb']]):+.3f}  pushes {up}")
