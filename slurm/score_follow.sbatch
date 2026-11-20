#!/bin/bash
export MATH_PROMPT=plain MATH_FINISH=1 MATH_MIX=1 WORK_DIR=${WORK_DIR} REPO=${WORK_DIR}/repo
source $REPO/slurm/common.sh
set +e
export MATH_EVAL_DTYPE=$COLLECT_DTYPE
OUT=$EVALROOT-$COLLECT_DTYPE; BASE=$OUT/base-test-sampled-k2-p95.jsonl
R=${RUN:?set RUN}; V=${WEIGHTS_DIR}/runs/$R/versions
echo "$(TZ=America/Los_Angeles date +'%H:%M:%S PST') following $R on $(hostname), GPUs $CUDA_VISIBLE_DEVICES"
end=$(( $(date +%s) + 3 * 3600 ))
while [ $(date +%s) -lt $end ]; do
  CHAIN=$(python3 - "$WORK_DIR/runs/$R/state.json" <<'PY'
import json, sys
try: st = json.load(open(sys.argv[1]))
except Exception: sys.exit(0)
V = st["versions"]; best = None; out = []
for name in sorted((n for n in V if n[1:].isdigit() and n != "v0"), key=lambda n: int(n[1:])):
    m = [x for x in (V[name].get("marks") or {}).values() if x]
    if V[name].get("late"): continue
    s = sum(x.count("1") / len(x) for x in m) / len(m) if m else None
    if name == "v1" or (s is not None and (best is None or s > best)): out.append(name)
    if s is not None and (best is None or s > best): best = s
h = st.get("handed")
if h and h != "v0" and h not in out: out.append(h)
print(" ".join(out))
PY
)
  left=0
  for name in $CHAIN; do d=$V/$name; out=$OUT/$R-$name-test-sampled-k2-p95.jsonl
    [ -f $out ] && continue
    [ -f $d/model.safetensors ] || continue
    left=1
    $PY $REPO/tools/sample_eval.py score --model $d --set test --k 2 --out $out.tmp --gpu-mem 0.8 --texts --gpus "$CUDA_VISIBLE_DEVICES" > $WORK_DIR/logs/follow-$R-$name.log 2>&1 < /dev/null
    if [ $? = 0 ] && [ ! -f $out ]; then mv $out.tmp $out; [ -f $out.tmp.answers ] && mv $out.tmp.answers $out.answers
      echo "$(TZ=America/Los_Angeles date +'%H:%M:%S') $R $name: $($PY $REPO/tools/sample_eval.py compare $BASE $out)"
    else echo "$(TZ=America/Los_Angeles date +'%H:%M:%S') $R $name: not kept (failed, or the session scored it meanwhile)"; rm -f $out.tmp $out.tmp.answers $out.tmp.shard*; fi
    break
  done
  if [ $left = 0 ]; then
    [ -f $WORK_DIR/runs/$R/result.json ] && { echo "CHAIN $R: $CHAIN"; echo "ALL SCORED $R"; exit 0; }
    sleep 45
  fi
done
echo "INCOMPLETE $R (three hours)"
