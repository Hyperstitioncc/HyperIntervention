#!/bin/bash
#SBATCH -J jb-direct
#SBATCH -p long
#SBATCH -c 16
#SBATCH --mem=256G
#SBATCH --gres=gpu:1
#SBATCH --open-mode=append
#SBATCH -o /home/%u/judge-bench/logs/%x-%j.out
export MATH_PROMPT=${MATH_PROMPT-plain} MATH_FINISH=${MATH_FINISH:-1} EVAL_FAST=${EVAL_FAST:-1} FINAL_GREEDY=${FINAL_GREEDY:-0}
source "${REPO:-$HOME/judge-bench/repo}/slurm/common.sh"
RUN=$WORK_DIR/runs/${RUN:?set RUN}
[ -e "$RUN/result.json" ] && { echo "$RUN holds a finished session; use a new RUN name"; exit 1; }
[ -e "$RUN/state.json" ] && echo "== resuming the session in $RUN"
[ -f "$EVALROOT-$COLLECT_DTYPE/base-test.jsonl" ] || { echo "no cached base score in $EVALROOT-$COLLECT_DTYPE: run slurm/base_eval.sbatch with the same MATH_PROMPT / MATH_FINISH first"; exit 1; }
mkdir -p "$RUN"
LOCAL_DIR=${LOCAL_DIR-/scratch/$USER/judge-bench}
if [ -n "$LOCAL_DIR" ] && mkdir -p "$LOCAL_DIR/models" 2>/dev/null && [ -w "$LOCAL_DIR/models" ]; then
  ( flock 9
    [ -f "$LOCAL_DIR/models/$STUDENT/.copied" ] || { echo "copying $STUDENT to $LOCAL_DIR on $(hostname)"; rm -rf "$LOCAL_DIR/models/$STUDENT"
      cp -r "$MODEL" "$LOCAL_DIR/models/$STUDENT" && touch "$LOCAL_DIR/models/$STUDENT/.copied"; } ) 9> "$LOCAL_DIR/models/.lock"
  [ -f "$LOCAL_DIR/models/$STUDENT/.copied" ] && MODEL=$LOCAL_DIR/models/$STUDENT
  EXTRA="${EXTRA:-} --local-dir $LOCAL_DIR/runs/$(basename "$RUN")"
fi
if [ "${OWN_POOL:-0}" != 0 ]; then
  [ -f "$RUN/prompts.jsonl" ] || : > "$RUN/prompts.jsonl"
  EXTRA="${EXTRA:-} --own-pool"
fi
[ "${POSE:-0}" != 0 ] && EXTRA="${EXTRA:-} --pose"
[ -n "${REPLAY:-}" ] && EXTRA="${EXTRA:-} --replay $REPLAY"
[ -n "${CANDIDATES:-}" ] && EXTRA="${EXTRA:-} --candidates $CANDIDATES"
[ "${TRAIN_GPUS:-1}" != 1 ] && EXTRA="${EXTRA:-} --train-gpus $TRAIN_GPUS"
[ "${NO_ARCHIVE:-0}" != 0 ] && EXTRA="${EXTRA:-} --no-archive"
[ -n "${PROVIDER_SORT:-}" ] && EXTRA="${EXTRA:-} --provider-sort $PROVIDER_SORT"
[ -n "${CALL_DEADLINE:-}" ] && EXTRA="${EXTRA:-} --call-deadline $CALL_DEADLINE"
[ -n "${REASONING_EFFORT:-}" ] && EXTRA="${EXTRA:-} --reasoning-effort $REASONING_EFFORT"
[ "${GRAD_BUDGET:-0}" != 0 ] && EXTRA="${EXTRA:-} --grad-budget $GRAD_BUDGET"
[ -n "${AUTO_CAP:-}" ] && EXTRA="${EXTRA:-} --auto-cap $AUTO_CAP"
[ -n "${GEN_THREADS:-}" ] && EXTRA="${EXTRA:-} --gen-threads $GEN_THREADS"
[ -n "${MAX_NUM_SEQS:-}" ] && EXTRA="${EXTRA:-} --max-num-seqs $MAX_NUM_SEQS"
[ -n "${GPU_MEM:-}" ] && EXTRA="${EXTRA:-} --gpu-mem $GPU_MEM"
[ "${POOL_SAMPLE:-0}" != 0 ] && EXTRA="${EXTRA:-} --pool-sample $POOL_SAMPLE"
[ "${KEEP_VERSIONS:-0}" != 0 ] && EXTRA="${EXTRA:-} --keep-versions"
[ -n "${DEFAULT_OPT:-}" ] && EXTRA="${EXTRA:-} --default-optimizer $DEFAULT_OPT"
[ "${MAX_TRAININGS:-0}" != 0 ] && EXTRA="${EXTRA:-} --max-trainings $MAX_TRAININGS"
[ "${WALL_HOURS:-0}" != 0 ] && EXTRA="${EXTRA:-} --wall-min $((WALL_HOURS * 60))"
[ "${POOL_K:-0}" != 0 ] && EXTRA="${EXTRA:-} --pool-k $POOL_K"
[ "${WORKFLOWS:-1}" = 1 ] && [ -z "${CANDIDATES:-}" ] && EXTRA="${EXTRA:-} --spread"
[ "${WORKFLOWS:-1}" != 1 ] && EXTRA="${EXTRA:-} --workflows $WORKFLOWS"
[ "${KEY:-1}" != 0 ] && EXTRA="${EXTRA:-} --key"
[ -f "$RUN/prompts.jsonl" ] || $PY "$REPO/tools/math_eval.py" list --n "${N:-600}" --seed "${SEED:-4}" --out "$RUN/prompts.jsonl"
NVAL=${NVAL:-500}; VAL_SKIP=5000; [ "$NVAL" -gt 1000 ] && VAL_SKIP=$((6000 - NVAL))
[ "${N:-600}" -le "$VAL_SKIP" ] || { echo "the pool (N=${N:-600}) runs into the validation problems (from position $VAL_SKIP)"; exit 1; }
[ -f "$RUN/val.jsonl" ] || $PY "$REPO/tools/math_eval.py" list --n "$NVAL" --seed "${SEED:-4}" --skip "$VAL_SKIP" --out "$RUN/val.jsonl"
write_config "$RUN" "$RUN/prompts.jsonl"
TURNS_DEFAULT=600
case "${JUDGE:-deepseek/deepseek-v4.1-flash}" in
  scripted:*) TURNS_DEFAULT=100000 ;;
  *) EXTRA="${EXTRA:-} --token-budget ${TOKEN_BUDGET:-2000000}"
     [ "${TOKEN_BUDGET:-2000000}" != 0 ] && TURNS_DEFAULT=100000
     [ "${TIME_BUDGET_MIN:-0}" != 0 ] && { EXTRA="$EXTRA --time-budget-min $TIME_BUDGET_MIN"; TURNS_DEFAULT=100000; }
     KEY_FILE=${KEY_FILE:-$HOME/judge-bench/openrouter.key}
     [ -s "$KEY_FILE" ] || { echo "$KEY_FILE is missing or empty"; exit 1; }
     set +x
     export OPENROUTER_API_KEY=$(cat "$KEY_FILE") ;;
esac
export EDITOR_COST_CAP=${COST_CAP:-0}
$PY "$REPO/tools/judge_session.py" --config "$RUN/ui.toml" --val "$RUN/val.jsonl" --run "$RUN" \
    --eval-out "$EVALROOT-$COLLECT_DTYPE" --dtype "$COLLECT_DTYPE" --model "${JUDGE:-deepseek/deepseek-v4.1-flash}" \
    --port $((20000 + (SLURM_JOB_ID % 1500) * 8)) --gen-budget "${GEN_BUDGET:-150000}" --train-units "${TRAIN_UNITS:-25000000}" --val-looks "${VAL_LOOKS:-30}" --problem-checks "${CHECKS:-300}" --turns "${TURNS:-$TURNS_DEFAULT}" \
    --providers "${PROVIDERS-Together,BaseTen,Fireworks}" --sample-k "${SAMPLE_K:-2}" --seed "${SEED2:-0}" --eval-k "${EVAL_K:-4}" --val-k-max "${VAL_K_MAX:-0}" ${EXTRA:-}
echo "result:"; head -c 3000 "$RUN/result.json"; echo
echo "direct session done"
