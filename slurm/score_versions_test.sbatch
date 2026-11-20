#!/bin/bash
export MATH_PROMPT=plain MATH_FINISH=1 MATH_MIX=1 WORK_DIR=${WORK_DIR} REPO=${WORK_DIR}/repo
source $REPO/slurm/common.sh
set +e
export MATH_EVAL_DTYPE=$COLLECT_DTYPE
OUT=$EVALROOT-$COLLECT_DTYPE; BASE=$OUT/base-test-sampled-k2-p95.jsonl
R=${RUN:?set RUN}; V=${WEIGHTS_DIR}/runs/$R/versions
[ -f "$BASE" ] || { echo "no cached base score at $BASE"; exit 1; }
echo "$(TZ=America/Los_Angeles date +'%H:%M:%S PST') scoring $R on $(hostname), GPUs $CUDA_VISIBLE_DEVICES"
score() {
  $PY $REPO/tools/sample_eval.py score --model "$1" --set test --k 2 --out "$2.tmp" --gpu-mem 0.8 --texts --gpus "$CUDA_VISIBLE_DEVICES" > "$WORK_DIR/logs/score-$3.log" 2>&1 < /dev/null || return 1
  mv "$2.tmp" "$2" || return 1
  [ -f "$2.tmp.answers" ] && mv "$2.tmp.answers" "$2.answers"
  return 0
}
CHAIN=$(python3 - "$WORK_DIR/runs/$R/state.json" <<'PY'
import json, sys
V = json.load(open(sys.argv[1]))["versions"]; best = None; out = []
for name in sorted((n for n in V if n[1:].isdigit() and n != "v0"), key=lambda n: int(n[1:])):
    m = [x for x in (V[name].get("marks") or {}).values() if x]
    if V[name].get("late"): continue
    s = sum(x.count("1") / len(x) for x in m) / len(m) if m else None
    if name == "v1" or (s is not None and (best is None or s > best)): out.append(name)
    if s is not None and (best is None or s > best): best = s
print(" ".join(out))
PY
)
echo "CHAIN $R: $CHAIN"
n=0; ok=0; DONE=""
for name in $CHAIN $(ls -d $V/v[0-9]* 2>/dev/null | sort -V | xargs -n1 basename); do
  d=$V/$name
  case " $DONE " in *" $name "*) continue;; esac; DONE="$DONE $name"
  case $name in *-*) continue;; esac
  [ -f $d/model.safetensors ] || { echo "$name: no weights there, skipped"; continue; }
  n=$((n+1)); out=$OUT/$R-$name-test-sampled-k2-p95.jsonl
  if [ -f $out ]; then echo "$name: already scored by the session"; else
    score $d $out $R-$name || { echo "$name: scoring FAILED"; tail -3 $WORK_DIR/logs/score-$R-$name.log | cut -c1-300; continue; }
  fi
  ok=$((ok+1)); echo "$(TZ=America/Los_Angeles date +'%H:%M:%S') $R $name: $($PY $REPO/tools/sample_eval.py compare $BASE $out)"
done
if [ -n "${CHECK:-}" ] && [ -f $V/$CHECK/model.safetensors ]; then
  score $V/$CHECK $OUT/check-$R-$CHECK-test-sampled-k2-p95.jsonl check-$R-$CHECK && echo "CHECK $R $CHECK scored a second time: $($PY $REPO/tools/sample_eval.py compare $BASE $OUT/check-$R-$CHECK-test-sampled-k2-p95.jsonl)"
fi
echo "versions with weights: $n; scored: $ok"
[ $n -gt 0 ] && [ $n = $ok ] && echo "ALL SCORED $R" || echo "INCOMPLETE $R"
