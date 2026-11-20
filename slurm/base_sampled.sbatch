#!/bin/bash
#SBATCH -J jb-base-sampled
#SBATCH -p long
#SBATCH -c 16
#SBATCH --mem=128G
#SBATCH --gres=gpu:1
#SBATCH -o /home/%u/judge-bench/logs/%x-%j.out
source "${REPO:-$HOME/judge-bench/repo}/slurm/common.sh"
export MATH_EVAL_DTYPE=$COLLECT_DTYPE
CUT=$($PY -c "import os; p = float(os.environ.get('MATH_TOP_P') or 0); print(f'-p{round(p * 100)}' if p else '')")
OUT=$EVALROOT-$COLLECT_DTYPE/base-test-sampled-k${K:-2}$CUT.jsonl
mkdir -p "$(dirname "$OUT")"
[ -f "$OUT" ] || { $PY "$REPO/tools/sample_eval.py" score --model "$MODEL" --set test --k "${K:-2}" --out "$OUT.tmp" --gpu-mem 0.8 && mv "$OUT.tmp" "$OUT"; }
ls -la "$OUT"; echo "base sampled done"
