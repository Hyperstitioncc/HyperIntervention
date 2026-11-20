import json, sys, os, re
src, out = sys.argv[1], sys.argv[2]
KEEP = ("train", "validate", "status", "use", "finish", "pose", "compare", "eval", "keep", "discard", "undo", "weight", "exclude")
with open(out, "w") as f:
    r = os.path.basename(os.path.dirname(src)); st = os.path.dirname(src) + "/state.json"
    f.write("# %s: the judge's replies in order\n\n" % r)
    try:
        m = json.load(open(st))["msgs"]; f.write("(The instructions it was given are the first message of state.json, %d characters; not repeated here.)\n\n" % len(m[0]["content"] if isinstance(m[0]["content"], str) else json.dumps(m[0]["content"])))
    except Exception: pass
    for l in open(src):
        if not l.strip(): continue
        e = json.loads(l)
        f.write("\n## turn %s | minute %.0f | tokens used %s | reply took %.0f s\n" % (e.get("turn"), (e.get("clock") or 0) / 60, e.get("own_tokens"), e.get("secs") or 0))
        if e.get("reasoning"): f.write("\nREASONING:\n%s\n" % e["reasoning"].strip())
        f.write("\nREPLY:\n%s\n" % (e.get("reply") or "").strip())
        for a, res in zip(e.get("actions") or [], e.get("results") or []):
            res = str(res); k = a.get("action")
            res = re.sub(r"^\[\{.*?\}\]\n", "", res, count=1, flags=re.S) if res.startswith("[{") else res
            cut = 6000 if k in KEEP else 700
            f.write("\nRESULT of %s:\n%s%s\n" % (json.dumps(a)[:400], res[:cut].strip(), " [... %d more characters cut]" % (len(res) - cut) if len(res) > cut else ""))
