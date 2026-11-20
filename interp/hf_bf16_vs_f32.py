import json, sys, torch
from transformers import AutoModelForCausalLM
model_dir, score = sys.argv[1], json.load(open(sys.argv[2]))
ids = torch.tensor([score["ids"]], device="cuda")
lps = {}
for dtype in (torch.float32, torch.bfloat16):
    m = AutoModelForCausalLM.from_pretrained(model_dir, torch_dtype=dtype).cuda().eval()
    with torch.no_grad():
        lps[dtype] = torch.log_softmax(m(ids).logits.float(), -1)[0, :-1].gather(-1, ids[0, 1:, None])[:, 0].cpu()
    del m; torch.cuda.empty_cache()
d = (lps[torch.float32] - lps[torch.bfloat16]).abs()
print(f"HF bf16 vs HF f32: max {d.max():.4f} mean {d.mean():.4f}")
for k in sys.argv[3:]:
    o = torch.tensor(json.load(open(k))["train"])
    d = (o - lps[torch.float32]).abs()
    print(f"{k.split('/')[-1]} vs HF f32: max {d.max():.4f} mean {d.mean():.4f}")
