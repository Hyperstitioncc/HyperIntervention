import json, sys, collections, os
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
rows_file, outdir = sys.argv[1], sys.argv[2]; os.makedirs(outdir, exist_ok=True)
SURF, INK, INK2, MUTED, GRID, HUE, CTX = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e6e5e0", "#2a78d6", "#d3d2cc"
MODELS = [("fable", "Fable 5.2"), ("opus", "Opus 5.5"), ("astra", "GPT-6.2 Astra"), ("carbon", "Gemini 4 Carbon"),
          ("neonmed", "Gemini 4 Neon (medium)"), ("kimi", "Kimi K3.1 Pro"), ("deepseek", "DeepSeek v4.1 flash")]
SKIP = set()
rows = [json.loads(l[6:]) for l in open(rows_file) if l.startswith("CURVE ")]
S = collections.defaultdict(list)
for r in rows:
    if r["s"] not in SKIP: S[r["s"]].append(r)
def student(r):
    a = r.get("ans") or {}
    return ((r.get("forktok") or 0) + 400 * (a.get("val", 0) + a.get("pool", 0))) / 1e6
AXES = {"time": (lambda r: r["min"], "minutes on the session's clock", 185), "judge": (lambda r: (r["tok"] or 0) / 1e6, "judge tokens, millions", None),
        "student": (student, "student rollout tokens, millions (estimated)", None)}
def path(sess, fx):
    best, pts, hand = 0.0, [(0.0, 0.0)], None
    for r in sorted((r for r in sess if r["min"] is not None), key=lambda r: r["min"]):
        if r["handed"] and r["test"] is not None: hand = (fx(r), r["test"])
        if r["val"] is None or r["test"] is None: continue
        if r["val"] > best:
            pts.append((fx(r), r["test"])); best = r["val"]
    return pts, hand
def model_of(s):
    k = s.rsplit("-", 1)[0]; return {"kimi": "kimi", "neonmed": "neonmed"}.get(k, k)
letter = lambda s: s.rsplit("-", 1)[1][0]
for name, (fx, xlabel, xmax) in AXES.items():
    P = {s: path(S[s], fx) for s in S}
    xm = xmax or max((p[0] for pts, h in P.values() for p in pts), default=1) * 1.08
    fig, axs = plt.subplots(2, 4, figsize=(16.5, 7.6), sharex=True, sharey=True, facecolor=SURF)
    for ax, (key, title) in zip(axs.flat, MODELS):
        ax.set_facecolor(SURF)
        for s, (pts, h) in P.items():
            if model_of(s) != key and len(pts) > 1:
                ax.step([p[0] for p in pts] + [xm], [p[1] for p in pts] + [pts[-1][1]], where="post", color=CTX, lw=1, zorder=1)
        ax.axhline(0, color=MUTED, lw=1, zorder=2)
        mine = sorted(s for s in P if model_of(s) == key)
        for s in mine:
            pts, h = P[s]
            if len(pts) < 2: continue
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            ax.step(xs + [xm], ys + [ys[-1]], where="post", color=HUE, lw=2, zorder=3, solid_capstyle="round")
            ax.plot(xs[1:], ys[1:], "o", ms=5.5, color=HUE, mec=SURF, mew=1.5, zorder=4)
            if h: ax.plot([h[0]], [h[1]], "o", ms=9.5, mfc=SURF, mec=HUE, mew=2.2, zorder=5)
            ax.annotate(letter(s), (xm, ys[-1]), xytext=(4, 0), textcoords="offset points", va="center", fontsize=9, color=INK2, annotation_clip=False)
        n = sum(1 for s in mine if len(P[s][0]) > 1)
        ax.set_title(f"{title}", loc="left", fontsize=11.5, color=INK, pad=6, fontweight="bold")
        ax.text(0.99, 0.04, f"{n} session{'s' if n != 1 else ''}", transform=ax.transAxes, ha="right", fontsize=8.5, color=MUTED)
        ax.grid(axis="y", color=GRID, lw=0.8); ax.set_axisbelow(True)
        for sp in ax.spines.values(): sp.set_visible(False)
        ax.tick_params(colors=MUTED, labelsize=9, length=0)
        ax.set_xlim(0, xm); ax.set_ylim(-5, 7.5)
    for ax in axs[-1]: ax.set_xlabel(xlabel, fontsize=9.5, color=INK2)
    for ax in axs[:, 0]: ax.set_ylabel("test gain, points", fontsize=9.5, color=INK2)
    fig.suptitle("Test gain of the version each session would have handed in, as the session went", x=0.012, ha="left", fontsize=14, color=INK, fontweight="bold", y=0.985)
    fig.text(0.012, 0.915, "Blue: that judge's sessions (a, b, c).  Grey: every other session.  Ring: the version actually handed in.  5,000 test problems, each point give or take 0.4.\n"
             "PRELIMINARY: " + sys.argv[3], fontsize=9, color=INK2, ha="left", linespacing=1.5)
    fig.subplots_adjust(left=0.045, right=0.98, top=0.85, bottom=0.08, hspace=0.26, wspace=0.07)
    out = os.path.join(outdir, f"sessions-test-gain-vs-{name}.png"); fig.savefig(out, dpi=130, facecolor=SURF); plt.close(fig); print(out)
