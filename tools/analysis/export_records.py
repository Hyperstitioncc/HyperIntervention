import csv, glob, gzip, json, os, re, sys

runs, evals, logs, out, prefix = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
names = [n for n in sys.argv[6].split(",") if n]
for d in ("actions", "corrections", "transcripts"):
    os.makedirs(os.path.join(out, d), exist_ok=True)


def rate(marks):
    m = [x for x in marks.values() if x]
    return sum(x.count("1") / len(x) for x in m) / len(m) if m else None


def load_marks(path):
    rows = {}
    for line in open(path):
        try:
            r = json.loads(line)
        except Exception:
            continue
        rows[r["id"]] = r.get("marks") or ""
    return rows


base_test = load_marks(os.path.join(evals, "base-test-sampled-k2-p95.jsonl"))
test_cols, val_cols, sessions = {"untrained": base_test}, {}, []
for name in names:
    d = os.path.join(runs, prefix + name)
    entries = [json.loads(l) for l in open(os.path.join(d, "agent.jsonl")) if l.strip()]
    state = json.load(open(os.path.join(d, "state.json")))
    config = json.loads(open(os.path.join(d, "config.jsonl")).readline())
    versions = state["versions"]
    with open(os.path.join(out, "actions", name + ".jsonl"), "w") as f:
        for e in entries:
            took = e.get("took") or []
            for i, a in enumerate(e.get("actions") or []):
                f.write(json.dumps({"turn": e["turn"], "minute": round((e.get("clock") or 0) / 60, 1), "seconds": took[i] if i < len(took) else None, **a}, separators=(",", ":")) + "\n")
    kept = os.path.join(d, "collected.jsonl")
    n_kept = 0
    if os.path.exists(kept):
        with open(os.path.join(out, "corrections", name + ".jsonl"), "w") as f:
            for line in open(kept):
                r = json.loads(line)
                ep = r.get("episode") or {}
                ep.pop("prompt_tokens", None)
                meta = {k: v for k, v in (r.get("meta") or {}).items() if k != "editor"}
                f.write(json.dumps({"index": r.get("index"), "round": r.get("round"), "correction": ep, "meta": meta}, separators=(",", ":")) + "\n")
                n_kept += 1
    for v in versions:
        if v == "v0":
            if "untrained" not in val_cols:
                val_cols["untrained"] = versions[v].get("marks") or {}
            continue
        val_cols[name + "/" + v] = versions[v].get("marks") or {}
        p = os.path.join(evals, "%s%s-%s-test-sampled-k2-p95.jsonl" % (prefix, name, v))
        if os.path.exists(p):
            test_cols[name + "/" + v] = load_marks(p)
    result = json.load(open(os.path.join(d, "result.json"))) if os.path.exists(os.path.join(d, "result.json")) else {}
    handed = result.get("handed_in") or (open(os.path.join(d, "HANDED")).read().strip() if os.path.exists(os.path.join(d, "HANDED")) else state.get("handed"))
    note = " ".join(open(os.path.join(d, f)).read().strip() for f in ("WRAPPED", "NOTE") if os.path.exists(os.path.join(d, f)))
    note = re.sub(r"r\d+-", "", note)
    refused = 0
    for lf in glob.glob(os.path.join(logs, "j-*.out")):
        text = open(lf, errors="replace").read()
        if ("runs/" + prefix + name + " ") in text or ("runs/" + prefix + name + "/") in text:
            refused += text.count("content_filter")
    base_val = rate(versions["v0"].get("marks") or {})
    hv = versions.get(handed) if handed else None
    val_gain = (rate(hv.get("marks") or {}) - base_val) * 100 if hv and handed != "v0" and rate(hv.get("marks") or {}) is not None else 0.0
    tm = test_cols.get(name + "/" + handed) if handed and handed != "v0" else None
    test_gain = test_se = None
    if tm:
        ids = [i for i in tm if i in base_test and tm[i] and base_test[i]]
        diffs = [tm[i].count("1") / len(tm[i]) - base_test[i].count("1") / len(base_test[i]) for i in ids]
        mu = sum(diffs) / len(diffs)
        test_gain = mu * 100
        test_se = (sum((x - mu) ** 2 for x in diffs) / (len(diffs) - 1) / len(diffs)) ** 0.5 * 100
    elif handed == "v0":
        test_gain, test_se = 0.0, 0.0
    args = config.get("args") or {}
    last = entries[-1] if entries else {}
    sessions.append({"session": name, "judge_model": args.get("model") or result.get("editor"), "reasoning_effort": args.get("reasoning_effort"),
                     "providers": " ".join(sorted({str(e.get("provider")) for e in entries if e.get("provider")})),
                     "time_limit_minutes": args.get("time_budget_min"), "last_reply_minute": round((last.get("clock") or 0) / 60, 1),
                     "replies": len(entries), "actions": sum(len(e.get("actions") or []) for e in entries), "judge_tokens": last.get("own_tokens"),
                     "judge_usd": last.get("usd"), "corrections_kept": n_kept, "versions_made": len(versions) - 1, "handed_in": handed,
                     "validation_gain_points": None if val_gain is None else round(val_gain, 2), "test_gain_points": None if test_gain is None else round(test_gain, 2),
                     "test_gain_standard_error": None if test_se is None else round(test_se, 3), "calls_refused_by_content_filter": refused, "note": note})

with open(os.path.join(out, "sessions.csv"), "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(sessions[0].keys()))
    w.writeheader()
    w.writerows(sessions)
for fname, cols in (("test_marks.csv.gz", test_cols), ("validation_marks.csv.gz", val_cols)):
    ids = sorted({i for c in cols.values() for i in c})
    with gzip.open(os.path.join(out, fname), "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["problem"] + list(cols))
        for i in ids:
            w.writerow([i] + [cols[c].get(i, "") for c in cols])
print("sessions %d | test columns %d | validation columns %d" % (len(sessions), len(test_cols), len(val_cols)))
