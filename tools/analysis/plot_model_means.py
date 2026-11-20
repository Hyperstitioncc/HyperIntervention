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
CONF = int(os.environ.get("CONF", "50"))
TMULT = {30: {1: 0.510, 2: 0.445, 3: 0.424, 4: 0.414, "z": 0.385}, 50: {1: 1.000, 2: 0.816, 3: 0.765, 4: 0.741, "z": 0.674},
         68: {1: 1.819, 2: 1.312, 3: 1.189, 4: 1.134, "z": 0.994}, 80: {1: 3.078, 2: 1.886, 3: 1.638, 4: 1.533, "z": 1.282},
         90: {1: 6.314, 2: 2.920, 3: 2.353, 4: 2.132, "z": 1.645}, 95: {1: 12.71, 2: 4.303, 3: 3.182, 4: 2.776, "z": 1.960}}[CONF]
MEAS_SE = 0.43
def halfwidth(vals):
    n = len(vals)
    if n < 2: return TMULT["z"] * MEAS_SE, 0.0, MEAS_SE
    mu = sum(vals) / n; sd = (sum((v - mu) ** 2 for v in vals) / (n - 1)) ** 0.5; se = sd / n ** 0.5
    return TMULT[min(n - 1, 4)] * se, sd, se
CURVE_ROWS = []
import numpy as np
def pchip(xs, ys, n=40):
    xs, ys = np.asarray(xs, float), np.asarray(ys, float); h = np.diff(xs); d = np.diff(ys) / h; m = np.zeros(len(xs))
    m[0], m[-1] = d[0], d[-1]
    for i in range(1, len(xs) - 1):
        m[i] = 0 if d[i - 1] * d[i] <= 0 else (2 * d[i - 1] * d[i]) / (d[i - 1] + d[i])
    X, Y = [], []
    for i in range(len(xs) - 1):
        t = np.linspace(0, 1, n, endpoint=(i == len(xs) - 2)); h00 = 2 * t**3 - 3 * t**2 + 1; h10 = t**3 - 2 * t**2 + t; h01 = -2 * t**3 + 3 * t**2; h11 = t**3 - t**2
        X.extend(xs[i] + t * h[i]); Y.extend(h00 * ys[i] + h10 * h[i] * m[i] + h01 * ys[i + 1] + h11 * h[i] * m[i + 1])
    return X, Y
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
    fig, ax = plt.subplots(figsize=(12.6, 6.9), facecolor=SURF); ax.set_facecolor(SURF)
    ax.axhline(0, color=MUTED, lw=1, zorder=1)
    ends = []
    for key, title, col in MODELS:
        mine = sorted(s for s in P if model_of(s) == key)
        if not mine: continue
        ys = [[at(P[s], x) for x in grid] for s in mine]
        mean = [sum(c) / len(c) for c in zip(*ys)]
        if BANDS and len(mine) > 1: ax.fill_between(grid, [min(c) for c in zip(*ys)], [max(c) for c in zip(*ys)], color=col, alpha=0.09, lw=0, zorder=2)
        ev = sorted(set(round(p[0], 4) for s in mine for p in P[s][1:]))
        ax_x = [0.0] + ev; per = [[0.0] * len(mine)] + [[at(P[s], x) for s in mine] for x in ev]
        keep = [0] + [i for i in range(1, len(ax_x)) if ax_x[i] - ax_x[i - 1] > xm * 0.004] ; ax_x = [ax_x[i] for i in keep]; per = [per[i] for i in keep]
        if ax_x[-1] < xm: ax_x.append(xm); per.append([P[s][-1][1] for s in mine])
        ax_y = [sum(v) / len(v) for v in per]; hw = [0.0] + [halfwidth(v)[0] for v in per[1:]]
        for x, v in zip(ax_x, per):
            h, sd, se = halfwidth(v) if x > 0 else (0.0, 0.0, 0.0)
            CURVE_ROWS.append([name, key, title, round(x, 5), round(sum(v) / len(v), 3), len(v), round(sd, 3), round(se, 3), CONF, round(h, 3), " ".join("%+.2f" % y for y in v)])
        sx, sy = pchip(ax_x, ax_y)
        lx, ly = pchip(ax_x, [y - h for y, h in zip(ax_y, hw)]); ux, uy = pchip(ax_x, [y + h for y, h in zip(ax_y, hw)])
        if BANDS: ax.fill_between(lx, ly, uy, color=col, alpha=0.13, lw=0, zorder=2)
        final_hw = hw[-1]
        ax.plot(sx, sy, color=col, lw=2.2, zorder=4, solid_capstyle="round")
        ax.plot(ax_x[1:-1] if ax_x[-1] == xm else ax_x[1:], ax_y[1:-1] if ax_x[-1] == xm else ax_y[1:], "o", ms=5.5, color=col, mec=SURF, mew=1.4, zorder=5)
        ends.append([mean[-1], title, col, len(mine), final_hw])
    ends.sort(key=lambda e: -e[0]); lab = [e[0] for e in ends]; gap = 0.52
    for i in range(1, len(lab)):
        if lab[i - 1] - lab[i] < gap: lab[i] = lab[i - 1] - gap
    for (y, title, col, n, fh), ly in zip(ends, lab):
        ax.plot([xm * 1.012, xm * 1.035], [ly, ly], color=col, lw=3, clip_on=False, solid_capstyle="round")
        ax.annotate(f"{title}   {y:+.1f}" + (f" ± {fh:.1f}" if BANDS else "") + f"  (n={n})", (xm * 1.045, ly), va="center", fontsize=9.5, color=INK, annotation_clip=False)
    ax.grid(axis="y", color=GRID, lw=0.8); ax.set_axisbelow(True)
    for sp in ax.spines.values(): sp.set_visible(False)
    ax.tick_params(colors=MUTED, labelsize=9.5, length=0)
    ax.set_xlim(0, xm); ax.set_ylim(-4.5, 7); ax.set_xlabel(xlabel, fontsize=10, color=INK2); ax.set_ylabel("test gain, points (mean over the model's sessions)", fontsize=10, color=INK2)
    fig.suptitle("Test gain against " + {"time": "time", "judge": "judge tokens", "student": "student rollout tokens", "student-excl-validation": "student rollout tokens (excluding validation runs)", "student-forks-only": "student rollout tokens (fork continuations only)"}[name] + ", by judge model", x=0.012, ha="left", fontsize=14, color=INK, fontweight="bold", y=0.98)
    cap = "Points: each time one of the model's sessions has a new version to hand in, the mean over its sessions of that version's test score (5,000 problems). Lines join the points.\n"
    if BANDS: cap += f"Band and ±: {CONF}% confidence interval for the model's mean, from the spread between its sessions (n = 1: that version's measurement error only).\n"
    cap += "Right edge: the mean test gain of the versions handed in.  " + note
    fig.text(0.012, 0.875, cap, fontsize=8.8, color=INK2, ha="left", linespacing=1.5)
    fig.subplots_adjust(left=0.055, right=0.675, top=0.835, bottom=0.085)
    out = os.path.join(outdir, f"test-gain-vs-{name}.png"); fig.savefig(out, dpi=140, facecolor=SURF); plt.close(fig); print(out)

import csv
with open(os.path.join(os.path.dirname(outdir.rstrip("/")), "data", "model_curves.csv"), "w", newline="") as f:
    w = csv.writer(f); w.writerow(["axis", "model", "model_name", "x", "mean_test_gain_points", "sessions", "sd_between_sessions", "standard_error", "confidence_percent", "interval_half_width", "session_values"])
    w.writerows(CURVE_ROWS)
