#!/bin/bash
#SBATCH -J jb-base-eval
#SBATCH -p long
#SBATCH -c 16
#SBATCH --mem=128G
#SBATCH --gres=gpu:1
#SBATCH -o /home/%u/judge-bench/logs/%x-%j.out
source "${REPO:-$HOME/judge-bench/repo}/slurm/common.sh"
if [ ! -f "$MODEL/config.json" ]; then
  [ -n "${HF_REPO:-}" ] || { echo "no $MODEL and no HF_REPO to fetch it from"; exit 1; }
  HF_HUB_OFFLINE=0 $PY -c "import sys; from huggingface_hub import snapshot_download; print(snapshot_download(sys.argv[1], local_dir=sys.argv[2]))" "$HF_REPO" "$MODEL"
fi
for d in ${EVAL_DTYPES//,/ }; do
  export MATH_EVAL_DTYPE=$d
  mkdir -p "$EVALROOT-$d"
  for s in test train; do
    [ -f "$EVALROOT-$d/base-$s.jsonl" ] || $PY "$REPO/tools/eval_watch.py" arm --model "$MODEL" --set $s \
        --out "$EVALROOT-$d/base-$s.jsonl" --gpu-mem 0.8
    $PY - "$EVALROOT-$d/base-$s.jsonl" <<'PY'
import json, sys
r = [json.loads(l) for l in open(sys.argv[1])]
boxed = sum("\\boxed" in x["completion"] for x in r)
print(f"{sys.argv[1]}: {sum(x['correct'] for x in r)}/{len(r)} correct, {boxed} boxed")
PY
  done
done
$PY - "$EVALROOT" <<'PY'
import json, os, sys
a, b = (os.path.join(f"{sys.argv[1]}-{d}", "base-test.jsonl") for d in ("float32", "bfloat16"))
if os.path.exists(a) and os.path.exists(b):
    A = {r["id"]: r for r in map(json.loads, open(a))}
    B = {r["id"]: r for r in map(json.loads, open(b))}
    same = sum(A[i]["completion"] == B[i]["completion"] for i in A)
    up = sum(B[i]["correct"] and not A[i]["correct"] for i in A)
    down = sum(A[i]["correct"] and not B[i]["correct"] for i in A)
    print(f"base, 32-bit vs 16-bit on test: {same}/{len(A)} identical answers; correctness flips +{up}/-{down} (16-bit relative to 32-bit)")
PY
echo "base eval done"
