import glob, os, shutil, sys, torch
from safetensors import safe_open
from safetensors.torch import save_file
base, edits_pt, out = sys.argv[1:4]
E = torch.load(edits_pt)["edits"]
W = {}
for shard in glob.glob(os.path.join(base, "*.safetensors")):
    with safe_open(shard, "pt") as f:
        for k in f.keys():
            W[k] = f.get_tensor(k)
for name, e in E.items():
    d = os.path.join(out, name)
    os.makedirs(d, exist_ok=True)
    for f in os.listdir(base):
        if f.endswith((".json", ".txt")) and "index" not in f:
            shutil.copy(os.path.join(base, f), d)
    new = dict(W)
    changed = 0
    for part, key in (("g", "gate_proj"), ("u", "up_proj")):
        for l, (idx, dw) in e[part].items():
            k = f"model.layers.{l}.mlp.{key}.weight"
            t = W[k].float().clone()
            t[idx] += dw.float()
            new[k] = t.to(W[k].dtype)
            changed += int((new[k] != W[k]).sum())
    save_file(new, os.path.join(d, "model.safetensors"))
    print(name, "params in edit", e["params"], "| bf16 entries actually changed", changed)
