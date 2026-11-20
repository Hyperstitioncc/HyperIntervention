import json, sys

for line in sys.stdin:
    r = json.loads(line)
    if r["type"] == "step":
        out = {"action": "force", "text": " banana"} if r["position"] == 2 else {"action": "accept"}
    else:
        out = {"verdict": "done"}
    print(json.dumps(out), flush=True)
