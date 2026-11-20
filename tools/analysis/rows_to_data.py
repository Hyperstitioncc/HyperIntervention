import json, sys, csv, os
src, out = sys.argv[1], sys.argv[2]
rows = [json.loads(l[6:]) for l in open(src) if l.startswith("CURVE ")]
rows.sort(key=lambda r: (r["s"], int(r["v"][1:])))
os.makedirs(out, exist_ok=True)
with open(out + "/versions.jsonl", "w") as f:
    for r in rows: f.write(json.dumps(r) + "\n")
cols = [("session", lambda r: r["s"]), ("version", lambda r: r["v"]), ("built_on", lambda r: r["parent"]), ("corrections_in_batch", lambda r: r["corr"]),
        ("minutes_on_clock_when_made", lambda r: r["min"]), ("judge_tokens_when_made", lambda r: r["tok"]), ("judge_usd_when_made", lambda r: r["usd"]),
        ("student_answers_in_forks", lambda r: (r.get("ans") or {}).get("fork")), ("student_answers_in_validation", lambda r: (r.get("ans") or {}).get("val")),
        ("student_answers_in_pool_checks", lambda r: (r.get("ans") or {}).get("pool")), ("student_tokens_in_fork_continuations", lambda r: r.get("forktok")),
        ("validation_gain_points", lambda r: r["val"]), ("validation_answers_per_problem", lambda r: r["nans"]), ("test_gain_points", lambda r: r["test"]), ("test_gain_standard_error_points", lambda r: r.get("test_se")), ("test_gain_50pct_interval_half_width", lambda r: None if r.get("test_se") is None else round(0.674 * r["test_se"], 3)), ("handed_in", lambda r: int(bool(r["handed"])))]
with open(out + "/versions.csv", "w", newline="") as f:
    w = csv.writer(f); w.writerow([c for c, _ in cols])
    for r in rows: w.writerow(["" if g(r) is None else g(r) for _, g in cols])
print("versions: %d | with a test score: %d | sessions: %d" % (len(rows), sum(1 for r in rows if r["test"] is not None), len({r["s"] for r in rows})))
