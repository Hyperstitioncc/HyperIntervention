import glob, sys, torch
from safetensors import safe_open
base, trained = sys.argv[1:3]
names = ["model.layers.19.mlp.down_proj.weight", "model.layers.19.self_attn.o_proj.weight",
         "model.layers.5.mlp.gate_proj.weight", "model.layers.10.self_attn.q_proj.weight"]
B = {}
for sh in glob.glob(f"{base}/*.safetensors"):
    with safe_open(sh, "pt") as f:
        for k in f.keys():
            if k in names:
                B[k] = f.get_tensor(k)
with safe_open(f"{trained}/model.safetensors", "pt") as f:
    T = {k: f.get_tensor(k) for k in names}
for n in names:
    b, t = B[n].float(), T[n].float()
    d = t - b
    ch = d != 0
    ulp = (b.abs() * 2 ** -7).clamp_min(1e-30)  # bf16 spacing at |b| (8 significant bits)
    q = [0.25, 0.5, 0.75]
    absb = b.abs().flatten()
    qs = torch.quantile(absb[torch.randperm(absb.numel())[:1_000_000]], torch.tensor(q))
    qc = torch.quantile(b.abs()[ch][torch.randperm(int(ch.sum()))[:1_000_000]], torch.tensor(q))
    print(f"{n}: entries changed {ch.float().mean():.2%}; |change|/spacing median {(d.abs() / ulp)[ch].median():.2f}; "
          f"|w| quartiles all {[round(x, 4) for x in qs.tolist()]} vs changed {[round(x, 4) for x in qc.tolist()]}")
