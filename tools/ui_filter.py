import argparse, json, sys, time, urllib.request
sys.path.insert(0, __import__("os").path.dirname(__file__))
from math_eval import grade


def call(url, path, body):
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST")
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8766")
    ap.add_argument("--prompts", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--start", type=int, default=0, help="resume: skip problems already in OUT.results.jsonl")
    ap.add_argument("--end", type=int, default=0, help="stop before this problem (0 = the end): with --start, one shard")
    a = ap.parse_args()
    rows = [json.loads(l) for l in open(a.prompts)]
    t0 = time.time()
    with open(a.out + ".results.jsonl", "a" if a.start else "w") as log:
        for i, r in enumerate(rows):
            if i < a.start or (a.end and i >= a.end):
                continue
            d = call(a.url, "/api/open", {"idx": i})["draft"]
            assert d["id"] == r["id"], (d["id"], r["id"])
            text = "".join(t["text"] for t in d["tokens"])
            ok = grade(r["answer"], text)
            log.write(json.dumps({"id": r["id"], "ui_correct": ok, "tokens": len(d["tokens"]),
                                  "ended_by": d["ended_by"], "solved": r.get("solved")}) + "\n")
            log.flush()
            print(f"{i + 1}/{len(rows)} {r['id']} {'right' if ok else 'WRONG'} "
                  f"({len(d['tokens'])} tok, {time.time() - t0:.0f}s)", file=sys.stderr, flush=True)
    right = {x["id"] for x in map(json.loads, open(a.out + ".results.jsonl")) if x["ui_correct"]}
    kept = [r for r in rows if r["id"] not in right]
    with open(a.out, "w") as f:
        for r in kept:
            f.write(json.dumps(r) + "\n")
    print(f"{len(kept)}/{len(rows)} wrong under manual-rl greedy -> {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
