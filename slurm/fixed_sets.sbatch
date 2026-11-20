#!/bin/bash
#SBATCH -J jb-fixed-sets
#SBATCH -p long
#SBATCH -c 16
#SBATCH --mem=256G
#SBATCH --gres=gpu:1
#SBATCH -o /home/%u/judge-bench/logs/%x-%j.out
export MATH_PROMPT=${MATH_PROMPT:-plain} MATH_FINISH=${MATH_FINISH:-1} DEV=1 EVAL_FAST=1
source "${REPO:-$HOME/judge-bench/repo}/slurm/common.sh"
export MATH_EVAL_DTYPE=float32
OUT=$WORK_DIR/runs/${NAME:?set NAME}
LR=${LR:-1e-7} K=${K:-4}
if [ -n "${STEPS:-}" ]; then HOW="--stages $STEPS"; CK=ckpt-$(printf %05d "$STEPS"); else HOW="--stages 40 --until-taken --taken-p 0.5"; CK=ckpt-00040; fi
mkdir -p "$OUT/sets"
if [ -n "${PREBUILT:-}" ]; then
  SETS=""
  for f in "$PREBUILT"/*.jsonl; do
    n=$(basename "$f" .jsonl)
    [ -z "${ONLY:-}" ] || case " $ONLY " in *" $n "*) ;; *) continue;; esac
    cp "$f" "$OUT/sets/$n.jsonl"; SETS="$SETS $n=prebuilt"
  done
else
  $PY "$REPO/tools/fixed_sets.py" "$OUT/sets" ${SETS:?set SETS} | tee "$OUT/sets.txt" || exit 1
fi
score() {
  [ -f "$2" ] || $PY "$REPO/tools/sample_eval.py" score --model "$1" --rows "$MATH_DEV_ROWS" --k "$K" --out "$2" --gpu-mem 0.8 || exit 1
}
[ -n "${BASE_SCORES:-}" ] && [ ! -f "$OUT/base.scores.jsonl" ] && cp "$BASE_SCORES" "$OUT/base.scores.jsonl"
echo "START base $(date -u +%H:%M:%SZ)"
score "$MODEL" "$OUT/base.scores.jsonl"
for spec in $SETS; do
  n=${spec%%=*}
  echo "START $n $(date -u +%H:%M:%SZ)"
  if [ ! -f "$OUT/$n.scores.jsonl" ]; then
    free=$(df --output=avail -BG "$OUT" | tail -1 | tr -dc 0-9)
    [ "$free" -ge 60 ] || { echo "only ${free} GB free: stopping before training $n"; exit 1; }
    rm -rf "$OUT/tr-$n"
    $PY "$REPO/tools/train_hf.py" --model "$MODEL" --episodes "$OUT/sets/$n.jsonl" --out "$OUT/tr-$n" --lr "$LR" $HOW \
        --pull 0 --down-weight "${DOWN_WEIGHT:-1}" --forced-weight "${FORCED_WEIGHT:-1}" --forward f32 --keep-weight "${KEEP_WEIGHT:-1}" --keep end > "$OUT/$n.train.log" 2>&1 \
      || { echo "training $n failed: $(tail -1 "$OUT/$n.train.log")"; rm -rf "$OUT/tr-$n"; exit 1; }
    cp "$OUT/tr-$n/$CK/stats.json" "$OUT/$n.stats.json"
    score "$OUT/tr-$n/$CK" "$OUT/$n.scores.jsonl"
  fi
  if [ -n "${GREEDY_BASE:-}" ]; then
    G=$OUT/$n.greedy.jsonl
    if [ ! -f "$G" ]; then
      [ -d "$OUT/tr-$n/$CK" ] || { echo "no checkpoint left for the most-likely-answer score of $n"; exit 1; }
      $PY "$REPO/tools/sample_eval.py" score --model "$OUT/tr-$n/$CK" --rows "$MATH_DEV_ROWS" --k 1 --temperature 0 --out "$G" --gpu-mem 0.8 || exit 1
    fi
    echo "RESULT $n most likely answer: $($PY "$REPO/tools/sample_eval.py" compare "$GREEDY_BASE" "$G")"
  fi
  rm -rf "$OUT/tr-$n"
  echo "RESULT $n (lr $LR${STEPS:+, fixed steps}${FORCED_WEIGHT:+, forced weight $FORCED_WEIGHT}${KEEP_WEIGHT:+, end weight $KEEP_WEIGHT}, $($PY -c "import json;s=json.load(open('$OUT/$n.stats.json'));print(s.get('steps'),'steps')")): $($PY "$REPO/tools/sample_eval.py" compare "$OUT/base.scores.jsonl" "$OUT/$n.scores.jsonl")"
done
echo "fixed sets done"
