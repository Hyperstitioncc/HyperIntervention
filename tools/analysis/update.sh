#!/bin/bash
P=$(cd "$(dirname "$0")" && pwd); R=${1:?the report folder, for example reports}; T=$R/data/version_rows.txt
mkdir -p "$R/data" "$R/figures"; : > "$T.new"; ok=0; want=$(echo ${NODES:?set NODES} | wc -w)
for h in $NODES; do
  for try in 1 2 3 4; do
    o=$(ssh "$h" 'bash -s' < "$P/version_rows.sh" 2>&1)
    case "$o" in *NODE*) echo "$o" >> "$T.new"; ok=$((ok + 1)); break;; *) sleep 8;; esac
  done
done
if [ "$ok" = "$want" ]; then mv "$T.new" "$T"; else echo "only $ok of $want nodes answered: keeping the earlier rows"; rm -f "$T.new"; fi
python3 "$P/rows_to_data.py" "$T" "$R/data"
for s in plot_model_means.py plot_model_means_stepped.py plot_sessions.py; do
  python3 "$P/$s" "$T" "$R/figures" "$2" "${3:-}" > /dev/null 2>&1 || echo "$s failed"
done
echo "figures: $(ls "$R"/figures/*.png | wc -l | tr -d ' ') | data: $(ls "$R/data" | tr '\n' ' ')"
