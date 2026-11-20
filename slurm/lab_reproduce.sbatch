#!/bin/bash
#SBATCH -J jb-lab-reproduce
#SBATCH -p long
#SBATCH -c 64
#SBATCH --mem=900G
#SBATCH --gres=gpu:8
#SBATCH -o /home/%u/judge-bench/logs/%x-%j.out
export STUDENT=${STUDENT:-qwen3-30b-a3b-instruct-2507}
source "${REPO:-$HOME/judge-bench/repo}/slurm/common.sh"
export STUDENT_MOE_BACKEND=${STUDENT_MOE_BACKEND:-triton}
LAB=${LAB:-$WORK_DIR/harvey-labs}
REC=$REPO/reports/lab
OUT=$WORK_DIR/runs/${NAME:-lab-reproduce}
EG=${EVAL_GPUS:-2}
mkdir -p "$OUT"
[ -s "$WORK_DIR/openrouter.key" ] || { echo "$WORK_DIR/openrouter.key is missing or empty"; exit 1; }
export OPENROUTER_API_KEY=$(cat "$WORK_DIR/openrouter.key")
[ "$(git -C "$LAB" rev-parse HEAD 2>/dev/null | cut -c1-7)" = 7f9b1ee ] || { echo "$LAB is not harvey-labs at 7f9b1ee"; exit 1; }

[ -s "$OUT/tasks.jsonl" ] || $PY "$REPO/tools/lab_tasks.py" build --lab "$LAB" --out "$OUT/tasks.jsonl" --tokenizer "$MODEL"
[ -s "$OUT/split/val.jsonl" ] || $PY "$REPO/tools/lab_tasks.py" split --rows "$OUT/tasks.jsonl" --out "$OUT/split" --max-tokens 240000
$PY -c "import json, sys; want = open(sys.argv[2]).read().split(); rows = {json.loads(l)['id']: l for l in open(sys.argv[1])}
missing = [i for i in want if i not in rows]; open(sys.argv[3], 'w').writelines(rows[i] for i in want if i in rows)
sys.exit('the split does not hold these published validation tasks: ' + ' '.join(missing) if missing else 0)" "$OUT/split/val.jsonl" "$REC/validation-tasks.txt" "$OUT/val.jsonl"
[ -s "$OUT/episodes.jsonl" ] || $PY "$REPO/tools/lab_tasks.py" episodes --corrections "$REC/corrections.jsonl.gz" \
    --rows "$OUT/split/pool.jsonl" --model "$MODEL" --out "$OUT/episodes.jsonl"

CK=$OUT/tr/ckpt-00010
if [ ! -f "$CK/stats.json" ]; then
  $PY -m torch.distributed.run --standalone --nproc-per-node="${GPUS:-8}" "$REPO/tools/train_hf.py" --fsdp --model "$MODEL" \
      --episodes "$OUT/episodes.jsonl" --out "$OUT/tr" --lr 4e-7 --stages 10 --until-taken --taken-p 0.5 \
      --keep-weight 1 --keep end --forward bf16 > "$OUT/train.log" 2>&1 || { tail -20 "$OUT/train.log"; exit 1; }
fi
grep -E "^epoch" "$OUT/train.log" | sed -n '1p;$p'

answer() {
  local model=$1 out=$2 parts=$((${GPUS:-8} / EG)) pids=()
  [ -s "$out" ] && [ "$(wc -l < "$out")" -ge "$(wc -l < "$OUT/val.jsonl")" ] && return 0
  for j in $(seq 0 $((parts - 1))); do
    awk -v j=$j -v p=$parts 'NR % p == j' "$OUT/val.jsonl" > "$out.rows$j"
    CUDA_VISIBLE_DEVICES=$(seq -s, $((j * EG)) $((j * EG + EG - 1))) $PY "$REPO/tools/lab_eval.py" generate \
        --rows "$out.rows$j" --model "$model" --out "$out.$j" --gpus "$EG" > "$out.log$j" 2>&1 &
    pids+=($!)
  done
  for p in "${pids[@]}"; do wait $p || { tail -5 "$out".log*; exit 1; }; done
  cat "$out".? > "$out"
}
grade() {
  $PY "$REPO/tools/lab_eval.py" grade --rows "$OUT/val.jsonl" --answers "$1" --lab "$LAB" --out "$2" 2>&1 | tail -1
}
if [ "${BASE:-published}" = published ]; then
  zcat "$REC/untrained-verdicts.jsonl.gz" > "$OUT/untrained-verdicts.jsonl"
else
  answer "$MODEL" "$OUT/untrained-answers.jsonl"
  grade "$OUT/untrained-answers.jsonl" "$OUT/untrained-verdicts.jsonl"
fi
answer "$CK" "$OUT/trained-answers.jsonl"
grade "$OUT/trained-answers.jsonl" "$OUT/trained-verdicts.jsonl"
$PY "$REPO/tools/lab_eval.py" compare --rows "$OUT/val.jsonl" --base "$OUT/untrained-verdicts.jsonl" \
    --trained "$OUT/trained-verdicts.jsonl" --answers "$OUT/trained-answers.jsonl"
