#!/bin/bash
#SBATCH -J jb-setup
#SBATCH -p long
#SBATCH -c 16
#SBATCH --mem=64G
#SBATCH --gres=gpu:1
#SBATCH -o /home/%u/judge-bench/logs/%x-%j.out
set -euo pipefail
WORK_DIR=${WORK_DIR:-$HOME/judge-bench}
export PATH=$HOME/.local/bin:$PATH
mkdir -p "$WORK_DIR"/{models,runs,logs}
cd "$WORK_DIR"
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
[ -x venv/bin/python ] || uv venv --python 3.12 venv
uv pip install --python venv/bin/python vllm peft datasets "math-verify[antlr4_13_2]" aiohttp accelerate
export PATH=$WORK_DIR/venv/bin:$PATH
uv pip install --python venv/bin/python pip
uv pip uninstall --python venv/bin/python flashinfer-cubin 2>/dev/null || true
vllm download-kernels || flashinfer download-kernels
venv/bin/python - <<'PY'
import torch, vllm, transformers, peft
print("vllm", vllm.__version__, "torch", torch.__version__, "cuda", torch.version.cuda,
      "transformers", transformers.__version__, "peft", peft.__version__)
print("gpu:", torch.cuda.get_device_name(0), round(torch.cuda.get_device_properties(0).total_memory / 2**30), "GiB")
x = torch.randn(4096, 4096, device="cuda", dtype=torch.bfloat16)
print("bf16 matmul ok:", float((x @ x).float().abs().mean()) > 0)
from huggingface_hub import snapshot_download
for repo, name in (("Qwen/Qwen2.5-0.5B", "qwen2.5-0.5b"), ("Qwen/Qwen2.5-7B", "qwen2.5-7b")):
    p = snapshot_download(repo, local_dir=f"models/{name}")
    print("downloaded", repo, "->", p)
from datasets import load_dataset
n = sum(len(load_dataset("EleutherAI/hendrycks_math", s, split=sp)) for s in
        ["algebra", "counting_and_probability", "geometry", "intermediate_algebra", "number_theory", "prealgebra", "precalculus"]
        for sp in ("train", "test"))
print("MATH problems cached:", n)
PY
echo "== vLLM generates with top-20 log-probs (as the steering server asks)"
venv/bin/python - <<'PY'
from vllm import LLM, SamplingParams
llm = LLM(model="models/qwen2.5-0.5b", dtype="float32", gpu_memory_utilization=0.1, max_model_len=2048, max_logprobs=20, seed=0)
o = llm.generate(["The capital of France is"], SamplingParams(temperature=0.0, max_tokens=8, logprobs=20))[0].outputs[0]
print("vllm ok:", repr(o.text), "| candidates at the first token:", len(o.logprobs[0]))
PY
uv pip freeze --python venv/bin/python > "$WORK_DIR/venv-freeze.txt"
echo "setup done"
