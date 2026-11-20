#!/bin/bash
export MATH_PROMPT=plain MATH_FINISH=1 MATH_MIX=1 WORK_DIR=${WORK_DIR} REPO=${WORK_DIR}/repo
source $REPO/slurm/common.sh
set +e
export MATH_EVAL_DTYPE=$COLLECT_DTYPE
OUT=$EVALROOT-$COLLECT_DTYPE; BASE=$OUT/base-test-sampled-k2-p95.jsonl; R=${RUN:?}; v=${VERSION:?}
d=${WEIGHTS_DIR}/runs/$R/versions/$v; out=$OUT/$R-$v-test-sampled-k2-p95.jsonl; tmp=$out.one
echo "$(TZ=America/Los_Angeles date +'%H:%M:%S PST') $R $v on GPUs $CUDA_VISIBLE_DEVICES"
[ -f $out ] && { echo "already scored"; exit 0; }
[ -f $d/model.safetensors ] || { echo "no weights at $d"; exit 1; }
$PY $REPO/tools/sample_eval.py score --model $d --set test --k 2 --out $tmp --gpu-mem 0.8 --texts --gpus "$CUDA_VISIBLE_DEVICES" > $WORK_DIR/logs/one-$R-$v.log 2>&1 < /dev/null || { echo FAILED; tail -3 $WORK_DIR/logs/one-$R-$v.log; exit 1; }
if [ -f $out ]; then echo "scored by another job meanwhile"; rm -f $tmp $tmp.answers; else mv $tmp $out; [ -f $tmp.answers ] && mv $tmp.answers $out.answers; fi
echo "$(TZ=America/Los_Angeles date +'%H:%M:%S') $R $v: $($PY $REPO/tools/sample_eval.py compare $BASE $out)"
