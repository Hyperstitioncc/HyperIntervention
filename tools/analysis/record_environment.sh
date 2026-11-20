#!/bin/bash
W=${WORK_DIR:-$HOME/judge-bench}; OUT=${1:?the folder to write environment.txt into, for example reports}; REQ=${2:-requirements.txt}
S=${STUDENT:-qwen2.5-7b}; LAB=${LAB:-$W/harvey-labs}
mkdir -p "$OUT"
"$W/venv/bin/python" -m pip freeze > "$REQ"
{
  echo "python"; "$W/venv/bin/python" -c "import sys, platform; print(sys.version); print(platform.platform())"
  echo; echo "gpu"; nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader | sort | uniq -c
  echo; echo "student model: $S; its config.json:"; cat "$W/models/$S/config.json"
  echo; echo "dataset: $(find "$W/hf/datasets" -maxdepth 4 -mindepth 4 -type d 2>/dev/null | head -1 | sed "s|$W/hf/datasets/||")"
  [ -d "$LAB/.git" ] && { echo; echo "harvey-labs: commit $(git -C "$LAB" rev-parse HEAD)"; echo "pandoc: $(pandoc --version 2>/dev/null | head -1)"; }
  echo; echo "packages: see $REQ"
} > "$OUT/environment.txt"
echo "wrote $OUT/environment.txt and $REQ"
