#!/bin/bash
#SBATCH -J jb-lab-session
#SBATCH -p long
#SBATCH -c 32
#SBATCH --mem=512G
#SBATCH --gres=gpu:8
#SBATCH --open-mode=append
#SBATCH -o /home/%u/judge-bench/logs/%x-%j.out
export STUDENT=${STUDENT:-qwen3-30b-a3b-instruct-2507}
source "${REPO:-$HOME/judge-bench/repo}/slurm/common.sh"
export STUDENT_MOE_BACKEND=${STUDENT_MOE_BACKEND:-triton}
export MATH_EVAL_MAX_TOKENS=${MAX_NEW:-16384}
LAB=${LAB:-$WORK_DIR/harvey-labs}
RUN=$WORK_DIR/runs/${RUN:?set RUN}
SPLIT=$WORK_DIR/lab/split-$STUDENT-${MAX_TOKENS:-240000}
[ -e "$RUN/summary.json" ] && { echo "$RUN holds a finished collection; use a new RUN name"; exit 1; }
POOL=${POOL:-$SPLIT/pool.jsonl}
[ -f "$POOL" ] || { echo "no $POOL: build the tasks and the split first (tools/lab_tasks.py build, split)"; exit 1; }
mkdir -p "$RUN"
[ -f "$RUN/val.jsonl" ] || head -n 1 "$SPLIT/val.jsonl" > "$RUN/val.jsonl"
TEMPLATE=$($PY -c "import json, sys; from transformers import AutoTokenizer as A
t = A.from_pretrained(sys.argv[1]).apply_chat_template([{'role': 'user', 'content': '{prompt}'}], add_generation_prompt=True, tokenize=False)
print(json.dumps(t)[1:-1])" "$MODEL")
cat > "$RUN/ui.toml" <<TOML
model = "$MODEL"
run_dir = "$RUN"
prompts = "$POOL"
template = "$TEMPLATE"
read = "plain"
[episode]
eos = [151643, 151645]
[episode.sampling]
temperature = 0.0
max_new_tokens = ${MATH_EVAL_MAX_TOKENS}
TOML
LONGEST=$($PY -c "import json, sys; print(max(json.loads(l)['tokens'] for l in open(sys.argv[1])))" "$POOL")
[ -s "$WORK_DIR/openrouter.key" ] || { echo "$WORK_DIR/openrouter.key is missing or empty"; exit 1; }
export OPENROUTER_API_KEY=$(cat "$WORK_DIR/openrouter.key")
export EDITOR_COST_CAP=${COST_CAP:-0}
$PY "$REPO/tools/lab_collect.py" --config "$RUN/ui.toml" --val "$RUN/val.jsonl" --test "$SPLIT/test.jsonl" --run "$RUN"     --eval-out "$WORK_DIR/lab/eval-$STUDENT" --lab "$LAB" --dtype "${COLLECT_DTYPE:-bfloat16}" --model "${JUDGE:-anthropic/claude-sonnet-5.5}"     --port $((20000 + SLURM_JOB_ID % 20000)) --max-model-len $((LONGEST + MATH_EVAL_MAX_TOKENS + 256)) --tasks-per "${TASKS_PER:-1}"     --server-gpus "${SERVER_GPUS:-1}" --gen-budget "${GEN_BUDGET:-400000}" --turns "${TURNS:-600}" --judge-usd "${JUDGE_USD:-10}"     --problem-checks "${CHECKS:-0}" --providers "${PROVIDERS-}" --gpu-mem "${GPU_MEM:-0.85}" --seed "${SEED2:-0}" ${EXTRA:-}
echo "summary:"; cat "$RUN/summary.json"; echo
echo "lab session done"
