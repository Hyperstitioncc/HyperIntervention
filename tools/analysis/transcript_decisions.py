import json, sys, os, re
src, out = sys.argv[1], sys.argv[2]
FULL = {"train": 2500, "validate": 900, "compare": 900, "eval": 900, "status": 1200, "finish": 600, "use": 400, "pose": 500}
with open(out, "w") as f:
    f.write("# %s: decisions view (full record: ../transcripts/%s.md; raw: ../runs/%s/agent.jsonl)\n" % ((os.path.basename(os.path.dirname(src)),) * 3))
    for l in open(src):
        if not l.strip(): continue
        e = json.loads(l)
        f.write("\n## turn %s | minute %.0f\n" % (e.get("turn"), (e.get("clock") or 0) / 60))
        if e.get("reasoning"): f.write("REASONING: %s\n" % e["reasoning"].strip())
        text = re.sub(r"```(?:json)?\s*\{.*?\}\s*```", "", e.get("reply") or "", flags=re.S).strip()
        if text: f.write("SAYS: %s\n" % text)
        for a, res in zip(e.get("actions") or [], e.get("results") or []):
            res = str(res); k = a.get("action")
            res = re.sub(r"^\[\{.*?\}\]\n", "", res, count=1, flags=re.S) if res.startswith("[{") else res
            if k == "fork":
                m = re.search(r"\((\d+)% of the way through the answer\) had probability ([\d.]+)%.*?own choice (\S+) had ([\d.]+)%", res, re.S); n = re.search(r"(\d+) of (\d+) match the reference", res)
                f.write("  ACT %s -> %s%s\n" % (json.dumps(a), ("at %s%% of the answer, token p=%s%% (student's own %s p=%s%%)" % m.groups()) if m else res[:120].replace("\n", " "), ("; %s of %s continuations right" % n.groups()) if n else ""))
            elif k in FULL: f.write("  ACT %s\n  -> %s\n" % (json.dumps(a)[:600], res[:FULL[k]].strip().replace("\n", "\n     ")))
            else: f.write("  ACT %s -> %s\n" % (json.dumps(a)[:300], res[:160].replace("\n", " ")))
