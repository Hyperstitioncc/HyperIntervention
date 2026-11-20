import json, sys, collections, os
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
rows_file, outdir, note = sys.argv[1], sys.argv[2], sys.argv[3]; os.makedirs(outdir, exist_ok=True)
SURF, INK, INK2, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e6e5e0"
MODELS = [("fable", "Fable 5.2", "#2a78d6"), ("opus", "Opus 5.5", "#eb6834"), ("carbon", "Gemini 4 Carbon", "#eda100"),
          ("kimi", "Kimi K3.1 Pro", "#e87ba4"), ("neonmed", "Gemini 4 Neon (medium)", "#008300"), ("astra", "GPT-6.2 Astra", "#6250d6"), ("deepseek", "DeepSeek v4.1 flash", "#e34948")]
SKIP = set()
END_MIN = {"opus-a": 110, "neonmed-c": 162}; HAND0 = {"deepseek-a"}
BANDS = os.environ.get("BANDS") == "1"
rows = [json.loads(l[6:]) for l in open(rows_file) if l.startswith("CURVE ")]
S = collections.defaultdict(list)
for r in rows:
    if r["s"] not in SKIP: S[r["s"]].append(r)
def student(r):
    a = r.get("ans") or {}
    return ((r.get("forktok") or 0) + 400 * (a.get("val", 0) + a.get("pool", 0))) / 1e6
def student_noval(r):
    a = r.get("ans") or {}
    return ((r.get("forktok") or 0) + 400 * a.get("pool", 0)) / 1e6
def student_forks(r):
    return (r.get("forktok") or 0) / 1e6
AXES = [("time", lambda r: r["min"], "minutes on the session's clock"), ("judge", lambda r: (r["tok"] or 0) / 1e6, "judge tokens, millions (input counted once, plus output)"),
        ("student", student, "student rollout tokens on the judge's orders, millions (forks, validation, pool checks; estimated)"),
        ("student-excl-validation", student_noval, "student rollout tokens excluding validation runs, millions (fork continuations and the pool checks after training; estimated)"),
        ("student-forks-only", student_forks, "student rollout tokens in fork continuations only, millions (what the corrections are made from; exact)")]
def model_of(s):
    return s.rsplit("-", 1)[0]
def path(s, fx, timeaxis):
    sess = [r for r in S[s] if r["min"] is not None]; best, pts, hand = 0.0, [(0.0, 0.0)], None
    for r in sorted(sess, key=lambda r: r["min"]):
        if r["handed"] and r["test"] is not None: hand = r["test"]
        if r["val"] is None or r["test"] is None: continue
        if r["val"] > best:
            pts.append((fx(r), r["test"])); best = r["val"]
    end = (END_MIN.get(s, 180) if timeaxis else max([fx(r) for r in sess] + [0]))
    if s in HAND0: hand = 0.0
    if hand is not None: pts.append((max(end, pts[-1][0]), hand))
    return pts
def at(pts, x):
    y = 0.0
    for px, py in pts:
        if px <= x: y = py
        else: break
    return y
for name, fx, xlabel in AXES:
    P = {s: path(s, fx, name == "time") for s in S}; P = {s: p for s, p in P.items() if len(p) > 1 or s in HAND0}
    xm = 180 if name == "time" else max(p[-1][0] for p in P.values())
    grid = [xm * i / 720 for i in range(721)]
    fig, ax = plt.subplots(figsize=(11.5, 6.6), facecolor=SURF); ax.set_facecolor(SURF)
    ax.axhline(0, color=MUTED, lw=1, zorder=1)
    ends = []
    for key, title, col in MODELS:
        mine = sorted(s for s in P if model_of(s) == key)
        if not mine: continue
        ys = [[at(P[s], x) for x in grid] for s in mine]
        mean = [sum(c) / len(c) for c in zip(*ys)]
        if BANDS and len(mine) > 1: ax.fill_between(grid, [min(c) for c in zip(*ys)], [max(c) for c in zip(*ys)], color=col, alpha=0.09, lw=0, zorder=2)
        ax.plot(grid, mean, color=col, lw=2.2, zorder=4, solid_capstyle="round")
        ends.append([mean[-1], title, col, len(mine)])
    ends.sort(key=lambda e: -e[0]); lab = [e[0] for e in ends]; gap = 0.52
    for i in range(1, len(lab)):
        if lab[i - 1] - lab[i] < gap: lab[i] = lab[i - 1] - gap
    for (y, title, col, n), ly in zip(ends, lab):
        ax.plot([xm * 1.012, xm * 1.035], [ly, ly], color=col, lw=3, clip_on=False, solid_capstyle="round")
        ax.annotate(f"{title}   {y:+.1f}" + (f"  (n={n})" if "session" not in title else ""), (xm * 1.045, ly), va="center", fontsize=9.5, color=INK, annotation_clip=False)
    ax.grid(axis="y", color=GRID, lw=0.8); ax.set_axisbelow(True)
    for sp in ax.spines.values(): sp.set_visible(False)
    ax.tick_params(colors=MUTED, labelsize=9.5, length=0)
    ax.set_xlim(0, xm); ax.set_ylim(-4.5, 7); ax.set_xlabel(xlabel, fontsize=10, color=INK2); ax.set_ylabel("test gain, points (mean over the model's sessions)", fontsize=10, color=INK2)
    fig.suptitle("Test gain against " + {"time": "time", "judge": "judge tokens", "student": "student rollout tokens", "student-excl-validation": "student rollout tokens (excluding validation runs)", "student-forks-only": "student rollout tokens (fork continuations only)"}[name] + ", by judge model", x=0.012, ha="left", fontsize=14, color=INK, fontweight="bold", y=0.98)
    fig.text(0.012, 0.905, "Line: mean over the model's sessions of the test score of the version a session would have handed in at that point (5,000 test problems)." + ("  Band: lowest to highest session." if BANDS else "") + "\n"
             "Right edge: mean test gain of the versions handed in.  PRELIMINARY: " + note, fontsize=8.8, color=INK2, ha="left", linespacing=1.5)
    fig.subplots_adjust(left=0.06, right=0.72, top=0.86, bottom=0.09)
    out = os.path.join(outdir, f"test-gain-vs-{name}-stepped.png"); fig.savefig(out, dpi=140, facecolor=SURF); plt.close(fig); print(out)
