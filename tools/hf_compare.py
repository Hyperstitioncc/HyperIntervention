import json, sys
import torch
from transformers import AutoModelForCausalLM

model_dir, score = sys.argv[1], json.load(open(sys.argv[2]))
ids = torch.tensor([score["ids"]], device="cuda")
for dtype in (torch.float32, torch.bfloat16):
    m = AutoModelForCausalLM.from_pretrained(model_dir, torch_dtype=dtype).cuda().eval()
    with torch.no_grad():
        lp = torch.log_softmax(m(ids).logits.float(), -1)[0, :-1].gather(-1, ids[0, 1:, None])[:, 0]
    hf = lp.cpu()
    for path in ("train", "sample"):
        ours = torch.tensor(score[path])
        d = (ours - hf).abs()
        print(f"hf {str(dtype):15s} vs {path:6s}: max |diff| {d.max():.4f}  mean {d.mean():.4f}  "
              f"(sum logp hf {hf.sum():.2f} ours {ours.sum():.2f})")
    del m
