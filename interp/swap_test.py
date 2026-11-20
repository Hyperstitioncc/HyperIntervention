"""
usage: swap_test.py BASE TRAINED PIVOT.jsonl OUT.jsonl [ID ...]   (no IDs: every causally pivotal item)
SWAP_DTYPE=float32 loads both models in f32 (needed for an f32-trained update; bf16 rounds most of it away).
"""

import json, os, sys, torch
from transformers import AutoModelForCausalLM, AutoTokenizer, AttentionInterface
from transformers.models.qwen2.modeling_qwen2 import repeat_kv
sys.path.insert(0, "/home/mason/manual-rl/tools")
from math_eval import TEMPLATE, load

base_m, trained_m, pf, out = sys.argv[1:5]
want = set(sys.argv[5:])
tok = AutoTokenizer.from_pretrained(base_m)
items = [json.loads(l) for l in open(pf)]
items = [it for it in items if (it["id"] in want) if want] or \
        [it for it in items if it["base_with_tt"] and not it["trained_with_tb"] and it["retrained_correct"]]
probs = {r["id"]: r for r in load("test")}
seqs = [tok(TEMPLATE.replace("{prompt}", probs[it["id"]]["prompt"]), add_special_tokens=False)["input_ids"] + it["prefix"]
        for it in items]

ATT = {"mode": None, "rec": {}, "src": None}


def attn(module, q, k, v, mask, scaling, dropout=0.0, **kw):
    k, v = repeat_kv(k, module.num_key_value_groups), repeat_kv(v, module.num_key_value_groups)
    # scores, softmax and the weighted sum in f32: eager's bf16 scores flip near-tied tokens (as candle's did)
    w = torch.matmul(q.float(), k.float().transpose(2, 3)) * scaling
    if mask is not None:
        w = w + mask
    w = torch.softmax(w, dim=-1)
    li = module.layer_idx
    if ATT["mode"] == "record":
        ATT["rec"][li] = w.detach().cpu()
    elif ATT["mode"] == "replace":
        w = ATT["src"][li].to(w.device)
    return torch.matmul(w, v.float()).to(q.dtype).transpose(1, 2).contiguous(), w


AttentionInterface.register("swap", attn)
# an unregistered implementation gets NO mask (transformers assumes a backend that masks itself): use eager's causal mask
from transformers.masking_utils import AttentionMaskInterface, eager_mask
AttentionMaskInterface.register("swap", eager_mask)

MLP = {"mode": None, "rec": {}, "src": None, "last_only": False}


def mlp_pre(li):
    def hook(mod, args):
        x = args[0]
        if MLP["mode"] == "record":
            MLP["rec"][li] = x.detach().cpu()
        elif MLP["mode"] == "replace":
            s = MLP["src"][li].to(x.device)
            if MLP["last_only"]:
                x = x.clone()
                x[:, -1] = s[:, -1]
            else:
                x = s
            return (x,)
    return hook


def load_model(path):
    dt = getattr(torch, os.environ.get("SWAP_DTYPE", "bfloat16"))
    m = AutoModelForCausalLM.from_pretrained(path, torch_dtype=dt, attn_implementation="swap").cuda().eval()
    for li, l in enumerate(m.model.layers):
        l.mlp.down_proj.register_forward_pre_hook(mlp_pre(li))
    return m


def run(m, s, att=None, mlp=None, last_only=False, record=False):
    ATT.update(mode="record" if record else ("replace" if att is not None else None), rec={}, src=att)
    MLP.update(mode="record" if record else ("replace" if mlp is not None else None), rec={}, src=mlp, last_only=last_only)
    with torch.no_grad():
        lp = torch.log_softmax(m(torch.tensor([s], device="cuda")).logits[0, -1].float(), -1).cpu()
    rec = (dict(ATT["rec"]), dict(MLP["rec"])) if record else None
    ATT["mode"] = MLP["mode"] = None
    return lp, rec


def process(chunk):
    recs = {"base": {}, "trained": {}}
    m = load_model(base_m)
    for k in chunk:
        lp, rec = run(m, seqs[k], record=True)
        res[k]["clean base"] = lp
        recs["base"][k] = rec
    del m
    torch.cuda.empty_cache()
    m = load_model(trained_m)
    for k in chunk:
        s = seqs[k]
        lp, rec = run(m, s, record=True)
        res[k]["clean trained"] = lp
        recs["trained"][k] = rec
        a, n = recs["base"][k]
        res[k]["trained <- base attn"] = run(m, s, att=a)[0]
        res[k]["trained <- base mlp-all"] = run(m, s, mlp=n)[0]
        res[k]["trained <- base mlp-last"] = run(m, s, mlp=n, last_only=True)[0]
    del m
    torch.cuda.empty_cache()
    m = load_model(base_m)
    for k in chunk:
        s = seqs[k]
        a, n = recs["trained"][k]
        res[k]["base <- trained attn"] = run(m, s, att=a)[0]
        res[k]["base <- trained mlp-all"] = run(m, s, mlp=n)[0]
        res[k]["base <- trained mlp-last"] = run(m, s, mlp=n, last_only=True)[0]
    del m
    torch.cuda.empty_cache()


res = [dict(id=it["id"], tb=it["tb"], tt=it["tt"], tb_text=it["tb_text"], tt_text=it["tt_text"]) for it in items]
CH = int(os.environ.get("SWAP_CHUNK", "20"))  # items per model load; recordings are held in host RAM
for c0 in range(0, len(items), CH):
    process(range(c0, min(len(items), c0 + CH)))

conds = ["clean base", "clean trained", "trained <- base attn", "base <- trained attn", "trained <- base mlp-all",
         "base <- trained mlp-all", "trained <- base mlp-last", "base <- trained mlp-last"]
with open(out, "w") as f:
    for r in res:
        row = {k: r[k] for k in ("id", "tb_text", "tt_text")}
        for c in conds:
            lp = r[c]
            row[c] = {"p_tt": float(lp[r["tt"]].exp()), "p_tb": float(lp[r["tb"]].exp()),
                      "argmax": tok.decode([int(lp.argmax())])}
        f.write(json.dumps(row) + "\n")

print(f"{len(res)} contested positions")
print(f"{'condition':28s} {'picks tt over tb':>17s} {'mean log p(tt)/p(tb)':>21s}")
for c in conds:
    lo = torch.tensor([float(r[c][r["tt"]] - r[c][r["tb"]]) for r in res])
    print(f"{c:28s} {float((lo > 0).float().mean()):>16.0%} {float(lo.mean()):>+21.2f}")
show = {"test/precalculus/529", "test/prealgebra/461", "test/number_theory/259", "test/algebra/212"}
for r in res:
    if r["id"] in show:
        print(f"\n{r['id']}: base {r['tb_text']!r} vs trained {r['tt_text']!r}")
        for c in conds:
            print(f"   {c:28s} p(tt) {r[c][r['tt']].exp():.3f}  p(tb) {r[c][r['tb']].exp():.3f}")
