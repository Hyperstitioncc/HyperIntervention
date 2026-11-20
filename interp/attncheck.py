import json, sys, torch
from transformers import AutoModelForCausalLM, AutoTokenizer
sys.path.insert(0, "/home/mason/manual-rl/tools")
from math_eval import TEMPLATE, load
tok = AutoTokenizer.from_pretrained("/home/mason/models/qwen2.5-7b")
it = {json.loads(l)["id"]: json.loads(l) for l in open("/home/mason/mathx/interp/pivot.jsonl")}["test/precalculus/529"]
p = {r["id"]: r for r in load("test")}["test/precalculus/529"]
s = tok(TEMPLATE.replace("{prompt}", p["prompt"]), add_special_tokens=False)["input_ids"] + it["prefix"]
print("tail:", repr(tok.decode(s[-12:])), "tb", it["tb"], repr(it["tb_text"]), "tt", it["tt"], repr(it["tt_text"]))
for path in ("/home/mason/models/qwen2.5-7b", "/home/mason/mathx/interp/trained"):
    for impl, dt in (("sdpa", torch.bfloat16), ("eager", torch.bfloat16), ("sdpa", torch.float32)):
        m = AutoModelForCausalLM.from_pretrained(path, torch_dtype=dt, attn_implementation=impl).cuda().eval()
        with torch.no_grad():
            lp = torch.log_softmax(m(torch.tensor([s], device="cuda")).logits[0, -1].float(), -1)
        top = [(tok.decode([int(i)]), round(float(lp[i].exp()), 3)) for i in lp.topk(3).indices]
        print(f"{path.split('/')[-1]:20s} {impl:5s} {str(dt):15s} p(tt) {lp[it['tt']].exp():.3f} p(tb) {lp[it['tb']].exp():.3f} top {top}")
        del m; torch.cuda.empty_cache()
