set -euo pipefail
WORK_DIR=${WORK_DIR:-$HOME/judge-bench}
REPO=${REPO:-$WORK_DIR/repo}
STUDENT=${STUDENT:-qwen2.5-7b}
MODEL=${MODEL:-$WORK_DIR/models/$STUDENT}
EVALROOT=$WORK_DIR/eval
[ "$STUDENT" = qwen2.5-7b ] || EVALROOT=$WORK_DIR/eval-$STUDENT
[ "${MATH_SHOT:-0}" != 0 ] && EVALROOT=$EVALROOT-shot${MATH_SHOT#1}
export MATH_PROMPT=${MATH_PROMPT:-}
[ -n "$MATH_PROMPT" ] && EVALROOT=$EVALROOT-$MATH_PROMPT
export MATH_FINISH=${MATH_FINISH:-0}
[ "$MATH_FINISH" != 0 ] && EVALROOT=$EVALROOT-fin
export MATH_MIX=${MATH_MIX:-0}
[ "$MATH_MIX" != 0 ] && EVALROOT=$EVALROOT-mix$MATH_MIX
if [ "$MATH_MIX" != 0 ]; then export MATH_TOP_P=${MATH_TOP_P:-0.95}; else export MATH_TOP_P=${MATH_TOP_P:-0}; fi
if [ "${DEV:-0}" != 0 ]; then
  export MATH_DEV_ROWS=$WORK_DIR/dev-rows.jsonl
  [ "$MATH_MIX" != 0 ] && export MATH_DEV_ROWS=$WORK_DIR/dev-rows-mix$MATH_MIX.jsonl
  [ -f "$MATH_DEV_ROWS" ] || $WORK_DIR/venv/bin/python "$REPO/tools/math_eval.py" list --n 1500 --seed 4 --skip 6000 --out "$MATH_DEV_ROWS"
  EVALROOT=$EVALROOT-dev
fi
if [ -f "$WORK_DIR/pack/env.tar" ] && [ ! -x "$HOME/judge-bench/venv/bin/python" ]; then
  mkdir -p "$HOME/judge-bench"
  ( flock 9; [ -x "$HOME/judge-bench/venv/bin/python" ] || { echo "unpacking the Python environment on $(hostname)"; tar -xf "$WORK_DIR/pack/env.tar" -C "$HOME"; } ) 9> "$HOME/judge-bench/.env.lock"
fi
PY=$WORK_DIR/venv/bin/python
export PATH=$WORK_DIR/venv/bin:$PATH
export MATH_SHOT=${MATH_SHOT:-0}
TEMPLATE=$(PYTHONPATH=$REPO/tools $WORK_DIR/venv/bin/python -c "import json, math_eval; print(json.dumps(math_eval.TEMPLATE)[1:-1])")
COLLECT_DTYPE=${COLLECT_DTYPE:-float32}
EVAL_DTYPES=${EVAL_DTYPES:-float32}
export MATH_EVAL_MAX_TOKENS=${MATH_EVAL_MAX_TOKENS:-4096}
[ -d "$WORK_DIR/hf" ] && export HF_HOME=$WORK_DIR/hf
export HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false
export HF_DATASETS_OFFLINE=1

if command -v nvidia-smi >/dev/null && [ -n "${SLURM_JOB_ID:-}" ]; then
  for _ in $(seq 36); do
    used=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | sort -n | tail -1)
    [ "${used:-0}" -gt 2000 ] || break
    sleep 5
  done
  if [ "${used:-0}" -gt 2000 ]; then
    echo "the GPU assigned to job $SLURM_JOB_ID on $(hostname) already holds ${used} MiB from a process outside Slurm; not starting"
    exit 75
  fi
fi

write_config() {
  mkdir -p "$1"
  cat > "$1/ui.toml" <<TOML
model = "$MODEL"
run_dir = "$1"
prompts = "$2"
template = "$TEMPLATE"
[episode]
eos = [151643, 151645]
[episode.sampling]
temperature = 0.0
max_new_tokens = ${MATH_EVAL_MAX_TOKENS:-4096}
TOML
}

start_server() {
  local run=$1; shift
  PORT=$((20000 + SLURM_JOB_ID % 12000))
  URL=http://127.0.0.1:$PORT
  $PY "$REPO/tools/student_server.py" --config "$run/ui.toml" --port "$PORT" --dtype "$COLLECT_DTYPE" "$@" \
      > "$run/server-$SLURM_JOB_ID.log" 2>&1 &
  SERVER_PID=$!
  trap 'kill $SERVER_PID 2>/dev/null || true' EXIT
  for _ in $(seq 360); do
    if curl -s -m 2 "$URL/api/progress" >/dev/null 2>&1; then echo "server up at $URL"; return 0; fi
    if ! kill -0 $SERVER_PID 2>/dev/null; then echo "server died:"; tail -40 "$run/server-$SLURM_JOB_ID.log"; return 1; fi
    sleep 5
  done
  echo "server did not come up"; tail -40 "$run/server-$SLURM_JOB_ID.log"; return 1
}
