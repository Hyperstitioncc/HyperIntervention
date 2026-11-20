#!/bin/bash
#SBATCH -J jb-null-check
#SBATCH -p long
#SBATCH -c 16
#SBATCH --mem=256G
#SBATCH --gres=gpu:1
#SBATCH -o /home/%u/judge-bench/logs/%x-%j.out
export MATH_PROMPT=${MATH_PROMPT:-plain} MATH_FINISH=${MATH_FINISH:-1} DEV=1 EVAL_FAST=1
source "${REPO:-$HOME/judge-bench/repo}/slurm/common.sh"
export MATH_EVAL_DTYPE=float32
OUT=$WORK_DIR/runs/${NAME:?set NAME}
mkdir -p "$OUT"
free=$(df --output=avail -BG "$OUT" | tail -1 | tr -dc 0-9)
[ "$free" -ge 60 ] || { echo "only ${free} GB free"; exit 1; }
if [ ! -d "$OUT/tr/ckpt-00001" ]; then
  rm -rf "$OUT/tr"
  $PY "$REPO/tools/train_hf.py" --model "$MODEL" --episodes "${EPISODES:?set EPISODES}" --out "$OUT/tr" --lr 0 --stages 1 \
      --pull 0 --down-weight 1 --forward f32 --keep-weight 1 --keep end > "$OUT/train.log" 2>&1 || { echo "trainer failed: $(tail -1 "$OUT/train.log")"; exit 1; }
fi
echo "START dev $(date -u +%H:%M:%SZ)"
[ -f "$OUT/null.scores.jsonl" ] || $PY "$REPO/tools/sample_eval.py" score --model "$OUT/tr/ckpt-00001" --rows "$MATH_DEV_ROWS" --k "${K:-4}" --out "$OUT/null.scores.jsonl" --gpu-mem 0.8 || exit 1
echo "RESULT dev, zero-rate checkpoint against the untrained student: $($PY "$REPO/tools/sample_eval.py" compare "${BASE_SCORES:?set BASE_SCORES}" "$OUT/null.scores.jsonl")"
if [ "${TEST:-0}" != 0 ]; then
  T=${EVALROOT%-dev}-float32/base-test-sampled-k2.jsonl
  echo "START test $(date -u +%H:%M:%SZ)"
  [ -f "$OUT/null.test.jsonl" ] || $PY "$REPO/tools/sample_eval.py" score --model "$OUT/tr/ckpt-00001" --set test --k 2 --out "$OUT/null.test.jsonl" --gpu-mem 0.8 || exit 1
  echo "RESULT test, zero-rate checkpoint against the cached untrained score: $($PY "$REPO/tools/sample_eval.py" compare "$T" "$OUT/null.test.jsonl")"
fi
rm -rf "$OUT/tr"
echo "null check done"
