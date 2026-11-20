"""
usage: rankcheck.py BASE TRAINED BATCH.jsonl
  update   = W_trained - W_base   (10 AdamW epochs on the 93-solve batch)
  sign(gradient) (Adam's first step is about lr * sign(grad))
"""

import json, sys, torch
from safetensors import safe_open
from transformers import AutoModelForCausalLM

base_m, trained_m, batch = sys.argv[1:4]
names = ["model.layers.19.mlp.down_proj.weight", "model.layers.19.self_attn.o_proj.weight",
         "model.layers.26.mlp.up_proj.weight", "model.layers.10.self_attn.q_proj.weight",
         "model.layers.5.mlp.gate_proj.weight"]
m = AutoModelForCausalLM.from_pretrained(base_m, torch_dtype=torch.bfloat16).cuda()
P = dict(m.named_parameters())
for n, p in P.items():
    p.requires_grad_(n in names)
eps = [json.loads(l)["episode"] for l in open(batch)]
nf = sum(len(e["forks"]) for e in eps)
for e in eps:  # forced-token CE, summed then averaged over all forced tokens (as in training)
    ids = torch.tensor([e["prompt_tokens"] + e["completion"]], device="cuda")
    lp = torch.log_softmax(m(ids).logits[0].float(), -1)
    P0 = len(e["prompt_tokens"])
    loss = -sum(lp[P0 + f["pos"] - 1, f["forced"][0]] for f in e["forks"]) / nf
    loss.backward()
with safe_open(f"{trained_m}/model.safetensors", "pt", device="cuda") as f:
    T = {n: f.get_tensor(n).float() for n in names}


def spec(M):
    s = torch.linalg.svdvals(M.float())
    e = s.pow(2)
    e = e / e.sum()
    c = e.cumsum(0)
    return (f"top1 {e[0]:.1%}  top10 {c[9]:.1%}  top100 {c[99]:.1%}  dirs for 90%: "
            f"{int((c < 0.9).sum()) + 1:>5d} of {len(s)}")


print(f"{len(eps)} episodes, {nf} forced tokens")
for n in names:
    W = P[n].data.float()
    print(f"\n{n}  {tuple(W.shape)}")
    print("   update  (trained-base):", spec(T[n] - W))
    print("   gradient (SGD dir)    :", spec(P[n].grad))
    print("   sign(gradient)        :", spec(torch.sign(P[n].grad.float())))
    print("   random Gaussian       :", spec(torch.randn_like(W)))
    cos = torch.nn.functional.cosine_similarity((T[n] - W).flatten(), -P[n].grad.float().flatten(), dim=0)
    print(f"   cosine(update, -gradient) {cos:.3f}")
