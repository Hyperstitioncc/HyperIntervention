"""Desktop app for steering by hand (Tkinter, standard library only).

The model runs on the GPU host under `manual-rl ui CONFIG`; this window talks to it over an
ssh tunnel:

    ssh -N -L 8765:localhost:8765 mason@HOST      # in one terminal
    python3 steer_app.py                          # in another (any python3 with tkinter)

Click a token to see the sampler's probabilities there. Click a candidate to fork the answer
at that token (the rest is regenerated); clicking the token already there reinforces it.
"Unselect" reverts a pick. "Apply gradient" trains on every pick so far and regenerates.
Keys: Left/Right move, 0-9 pick candidate, u unselect.
"""
import argparse, bisect, json, queue, threading, urllib.request
import tkinter as tk
from tkinter import messagebox, ttk

MONO = ("Menlo", 13)


def vis(s):
    return s.replace("\n", "↵").replace("\t", "→").replace(" ", "·")


def last_boxed(s):
    i = s.rfind("\\boxed")
    j = s.find("{", i) if i >= 0 else -1
    if j < 0:
        return None
    d = 0
    for k in range(j, len(s)):
        d += {"{": 1, "}": -1}.get(s[k], 0)
        if d == 0:
            return s[j + 1:k]
    return None


def norm(s):
    s = s or ""
    for x in (" ", "\n", "\\!", "\\,", "\\left", "\\right", "$"):
        s = s.replace(x, "")
    return s.replace("\\dfrac", "\\frac").replace("\\tfrac", "\\frac")



SYM = {
    "cdot": "·", "times": "×", "div": "÷", "pm": "±", "mp": "∓", "leq": "≤", "le": "≤", "geq": "≥",
    "ge": "≥", "neq": "≠", "ne": "≠", "approx": "≈", "equiv": "≡", "sim": "∼", "cong": "≅", "propto": "∝",
    "infty": "∞", "to": "→", "rightarrow": "→", "leftarrow": "←", "Rightarrow": "⇒", "implies": "⟹",
    "iff": "⟺", "Leftrightarrow": "⇔", "mapsto": "↦", "in": "∈", "notin": "∉", "subset": "⊂",
    "subseteq": "⊆", "supset": "⊃", "cup": "∪", "cap": "∩", "emptyset": "∅", "varnothing": "∅",
    "forall": "∀", "exists": "∃", "neg": "¬", "land": "∧", "lor": "∨", "wedge": "∧", "vee": "∨",
    "angle": "∠", "triangle": "△", "parallel": "∥", "perp": "⊥", "circ": "∘", "degree": "°",
    "sum": "∑", "prod": "∏", "int": "∫", "partial": "∂", "nabla": "∇", "ldots": "…", "cdots": "⋯",
    "dots": "…", "vdots": "⋮", "ddots": "⋱", "prime": "′", "mid": "∣", "nmid": "∤", "lfloor": "⌊",
    "rfloor": "⌋", "lceil": "⌈", "rceil": "⌉", "langle": "⟨", "rangle": "⟩", "binom": "C",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ", "lambda": "λ",
    "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ",
    "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ",
    "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ",
    "Omega": "Ω", "quad": "  ", "qquad": "    ", ";": " ", ":": " ", ",": "", "!": "", " ": " ",
    "\\": " ", "{": "{", "}": "}", "$": "$", "%": "%", "&": "&", "#": "#", "_": "_", "|": "‖",
    "cr": " ", "displaystyle": "", "textstyle": "", "limits": "", "nolimits": "", "middle": "",
}
FUNCS = {"sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan", "sinh", "cosh", "tanh",
         "log", "ln", "lg", "exp", "lim", "max", "min", "sup", "inf", "gcd", "lcm", "det", "deg", "mod",
         "bmod", "pmod", "arg", "dim", "ker", "Pr"}
SIZERS = {"left", "right", "big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr", "biggl", "biggr"}
TEXTISH = {"text", "textbf", "textit", "textrm", "mathrm", "mathbf", "mathit", "mathsf", "mathtt",
           "operatorname", "mbox", "mathbb", "mathcal", "boldsymbol", "bm", "overline", "underline",
           "hat", "bar", "vec", "tilde", "widehat", "overrightarrow"}
SIMPLE = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789."


class Tex:

    def __init__(self, src):
        self.src, self.out = src, []
        self.run()

    def emit(self, s, e, text, tags):
        self.out.append((s, e, text, tags, text == self.src[s:e]))

    def run(self):
        src, n, i, bold, run = self.src, len(self.src), 0, False, 0

        def flush(upto):
            if upto > run:
                self.emit(run, upto, src[run:upto], ("bold",) if bold else ())

        while i < n:
            for op, cl in (("\\(", "\\)"), ("\\[", "\\]"), ("$$", "$$"), ("$", "$")):
                if src.startswith(op, i):
                    flush(i)
                    j = src.find(cl, i + len(op))
                    end = n if j < 0 else j
                    self.emit(i, i + len(op), "", ())
                    self.seq(i + len(op), end, ("math",))
                    if j >= 0:
                        self.emit(j, j + len(cl), "", ())
                    i = run = n if j < 0 else j + len(cl)
                    break
            else:
                if src.startswith("**", i):
                    flush(i)
                    self.emit(i, i + 2, "", ())
                    bold, i = not bold, i + 2
                    run = i
                else:
                    i += 1
        flush(n)

    def seq(self, i, end, tags, stop=False):
        while i < end:
            if stop and self.src[i] == "}":
                return i
            i = self.atom(i, end, tags)
        return i

    def close_of(self, i, end):
        d, k = 0, i
        while k < end:
            c = self.src[k]
            if c == "\\":
                k += 2
                continue
            d += {"{": 1, "}": -1}.get(c, 0)
            if d == 0:
                return k
            k += 1
        return end

    def group(self, i, end, tags, open_="", close=""):
        self.emit(i, i + 1, open_, tags)
        j = self.seq(i + 1, end, tags, stop=True)
        if j < end:
            self.emit(j, j + 1, close, tags)
            return j + 1
        return j

    def arg(self, i, end, tags, open_="", close="", wrap=False):
        while i < end and self.src[i] == " ":
            self.emit(i, i + 1, "", tags)
            i += 1
        if i >= end:
            return i
        if self.src[i] == "{":
            if wrap:
                inner = self.src[i + 1:self.close_of(i, end)].strip()
                simple = inner and (all(c in SIMPLE for c in inner) or (inner[0] == "\\" and inner[1:].isalpha()))
                if not simple:
                    open_, close = open_ + "(", ")" + close
            return self.group(i, end, tags, open_, close)
        if open_:
            self.emit(i, i, open_, tags)
        if self.src[i] in "\\^_":
            j = self.atom(i, end, tags)
        else:
            self.emit(i, i + 1, self.src[i], tags)
            j = i + 1
        if close:
            self.emit(j, j, close, tags)
        return j

    def atom(self, i, end, tags):
        src, c = self.src, self.src[i]
        if c == "{":
            return self.group(i, end, tags)
        if c in "^_":
            if c == "^" and src.startswith("\\circ", i + 1):
                self.emit(i, i + 6, "°", tags)
                return i + 6
            self.emit(i, i + 1, "", tags)
            return self.arg(i + 1, end, tags + ("sup" if c == "^" else "sub",))
        if c == "\\":
            j = i + 1
            if j < end and src[j].isalpha():
                while j < end and src[j].isalpha():
                    j += 1
            else:
                j = min(j + 1, end)
            return self.command(src[i + 1:j], i, j, end, tags)
        j = i + 1
        while j < end and src[j] not in "{}^_\\":
            j += 1
        self.emit(i, j, src[i:j], tags)
        return j

    def command(self, name, i, j, end, tags):
        if name in ("frac", "dfrac", "tfrac", "cfrac"):
            self.emit(i, j, "", tags)
            k = self.arg(j, end, tags, wrap=True)
            return self.arg(k, end, tags, open_="/", wrap=True)
        if name == "binom":
            self.emit(i, j, "C(", tags)
            k = self.arg(j, end, tags)
            return self.arg(k, end, tags, open_=", ", close=")")
        if name == "sqrt":
            self.emit(i, j, "√", tags)
            if j < end and self.src[j] == "[":
                m = self.src.find("]", j, end)
                m = end - 1 if m < 0 else m
                self.emit(j, m + 1, self.src[j + 1:m], tags + ("sup",))
                j = m + 1
            return self.arg(j, end, tags, wrap=True)
        if name == "boxed":
            self.emit(i, j, "", tags)
            return self.arg(j, end, tags + ("boxed",), open_=" ", close=" ")
        if name in TEXTISH:
            self.emit(i, j, "", tags)
            extra = ("bold",) if "bf" in name or name in ("boldsymbol", "bm") else ()
            t = tuple(x for x in tags if x != "math") + ("upright",) + extra if name.startswith(("text", "math", "op", "mbox")) else tags
            return self.arg(j, end, t)
        if name in SIZERS:
            self.emit(i, j, "", tags)
            if j < end and self.src[j] == ".":
                self.emit(j, j + 1, "", tags)
                j += 1
            return j
        if name in SYM:
            self.emit(i, j, SYM[name], tags)
            return j
        if name in FUNCS:
            self.emit(i, j, name, tags + ("upright",))
            return j
        self.emit(i, j, self.src[i:j], tags)
        return j


class App:
    def __init__(self, root, url):
        self.root, self.url = root, url.rstrip("/")
        self.S, self.sel, self.busy = None, None, False
        self.starts, self.segs, self.seg_src, self.seg_disp = [0], [], [], [0]
        self.results = queue.Queue()
        root.title("manual-rl")
        root.geometry("1400x900")

        outer = ttk.PanedWindow(root, orient="horizontal")
        outer.pack(fill="both", expand=True)

        left = ttk.Frame(outer)
        self.plist = tk.Listbox(left, width=28, font=("Menlo", 11), activestyle="none", exportselection=False,
                                background="white", foreground="#222", selectbackground="#e2dccb", selectforeground="#000")
        sb = ttk.Scrollbar(left, command=self.plist.yview)
        self.plist.config(yscrollcommand=sb.set)
        self.plist.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.plist.bind("<<ListboxSelect>>", self.on_problem)
        outer.add(left, weight=0)

        mid = ttk.Frame(outer)
        # Explicit colours throughout: under macOS dark mode Tk defaults the text to white.
        self.problem = tk.Text(mid, height=7, wrap="word", font=("Helvetica", 13), relief="flat",
                               background="#f4f3ef", foreground="#222", padx=10, pady=8)
        self.problem.pack(fill="x", padx=8, pady=(8, 0))
        self.meta = ttk.Label(mid, text="Pick a problem on the left.", foreground="#555")
        self.meta.pack(fill="x", padx=10, pady=6)
        af = ttk.Frame(mid)
        af.pack(fill="both", expand=True, padx=8)
        self.answer = tk.Text(af, wrap="char", font=MONO, cursor="hand2", padx=10, pady=8, spacing1=2, spacing3=2,
                              background="white", foreground="#222", insertbackground="#222")
        asb = ttk.Scrollbar(af, command=self.answer.yview)
        self.answer.config(yscrollcommand=asb.set)
        self.answer.pack(side="left", fill="both", expand=True)
        asb.pack(side="right", fill="y")
        self.answer.bind("<Button-1>", self.on_token)
        for w, size in ((self.problem, 13), (self.answer, 14)):
            w.tag_configure("prose", font=("Helvetica", size))
            w.tag_configure("math", font=("Times New Roman", size + 1, "italic"))
            w.tag_configure("upright", font=("Times New Roman", size + 1))
            w.tag_configure("bold", font=("Helvetica", size, "bold"))
            w.tag_configure("sup", offset=6, font=("Times New Roman", size - 2, "italic"))
            w.tag_configure("sub", offset=-4, font=("Times New Roman", size - 2, "italic"))
            w.tag_configure("boxed", relief="solid", borderwidth=1)
            w.tag_lower("prose")
        self.answer.tag_configure("pick", background="#cdebd8", foreground="#000", underline=True)
        self.answer.tag_configure("reinf", background="#d6e2f7", foreground="#000", underline=True)
        self.answer.tag_configure("cur", background="#ffd84d", foreground="#000", borderwidth=1, relief="solid")
        self.answer.tag_raise("pick")
        self.answer.tag_raise("reinf")
        self.answer.tag_raise("cur")
        outer.add(mid, weight=1)

        right = ttk.Frame(outer, width=340)
        self.sideh = ttk.Label(right, text="Click a token", font=("Helvetica", 13, "bold"))
        self.sideh.pack(fill="x", padx=8, pady=(10, 4))
        self.cands = ttk.Treeview(right, columns=("k", "tok", "p", "bar"), show="headings", height=20, selectmode="browse")
        for c, w, t in (("k", 28, "#"), ("tok", 120, "token"), ("p", 72, "prob"), ("bar", 100, "")):
            self.cands.heading(c, text=t)
            self.cands.column(c, width=w, anchor="e" if c == "p" else "w", stretch=c == "tok")
        self.cands.tag_configure("here", font=("Helvetica", 12, "bold"), background="#fff4c2", foreground="#000")
        self.cands.tag_configure("orig", foreground="#888")
        self.cands.pack(fill="both", expand=True, padx=8)
        self.cands.bind("<<TreeviewSelect>>", self.on_cand)
        self.unpick_btn = ttk.Button(right, text="Unselect", command=self.do_unpick)
        self.unpick_btn.pack(fill="x", padx=8, pady=8)
        ttk.Label(right, wraplength=320, foreground="#666", justify="left", text=(
            "Bold row = token in the answer now. Click another row to fork there; click the bold row "
            "to reinforce it.\nGreen = changed, blue = reinforced, yellow = the token you clicked.\n"
            "Keys: ←/→ move, 0–9 pick, u unselect.")).pack(fill="x", padx=8, pady=(0, 10))
        outer.add(right, weight=0)

        bar = ttk.Frame(root)
        bar.pack(fill="x", side="bottom")
        self.regen_btn = ttk.Button(bar, text="Regenerate", command=self.do_regen)
        self.regen_btn.pack(side="left", padx=8, pady=8)
        self.status = ttk.Label(bar, text="connecting…", foreground="#555")
        self.status.pack(side="left", fill="x", expand=True)
        self.latex = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="LaTeX (off = raw tokens)", variable=self.latex,
                        command=lambda: self.S and self.render()).pack(side="right", padx=8)
        self.apply_btn = ttk.Button(bar, text="Apply gradient", command=self.do_apply)
        self.apply_btn.pack(side="right", padx=8, pady=8)

        for key in ("<Left>", "<Right>"):
            root.bind(key, self.on_arrow)
        root.bind("<Key>", self.on_key)
        self.call("state")


    def call(self, path, body=None):
        if self.busy:
            return
        self.busy = True
        self.set_status("⏳ working… (the model is writing; a long answer can take a minute)")
        self.root.config(cursor="watch")
        for b in (self.regen_btn, self.apply_btn, self.unpick_btn):
            b.state(["disabled"])

        def run():
            try:
                req = urllib.request.Request(f"{self.url}/api/{path}", data=json.dumps(body or {}).encode(), method="POST")
                with urllib.request.urlopen(req, timeout=600) as r:
                    self.results.put(json.loads(r.read()))
            except urllib.error.HTTPError as e:
                self.results.put(json.loads(e.read() or b'{"error":"http error"}'))
            except Exception as e:  # noqa: BLE001
                self.results.put({"error": f"{e} (is the ssh tunnel up?)"})
        threading.Thread(target=run, daemon=True).start()
        self.root.after(100, self.poll)

    def poll(self):
        try:
            j = self.results.get_nowait()
        except queue.Empty:
            self.root.after(100, self.poll)
            return
        self.busy = False
        self.root.config(cursor="")
        for b in (self.regen_btn, self.apply_btn, self.unpick_btn):
            b.state(["!disabled"])
        if "error" in j:
            self.render_status()
            messagebox.showerror("manual-rl", j["error"])
        else:
            self.S = j
            try:
                self.render()
            except Exception as e:  # noqa: BLE001 (show it rather than freeze the window)
                import traceback
                traceback.print_exc()
                messagebox.showerror("manual-rl", f"drawing failed: {e!r}")


    def set_status(self, s):
        self.status.config(text=s)

    def render_status(self):
        if not self.S:
            return self.set_status("not connected")
        st = self.S.get("last_stats")
        s = f"round {self.S['round']} · {self.S['pending_picks']} pick(s) pending"
        if st:
            s += (f" · last update: forced log-prob {st['forced_logp_before']:.2f} → {st['forced_logp_after']:.2f}"
                  f" over {st['pairs']} picks, {st['steps']} step(s)")
        self.set_status(s)

    def render(self):
        S = self.S
        top = self.plist.yview()[0]
        self.plist.delete(0, "end")
        cur = S["draft"]["idx"] if S["draft"] else None
        for p in S["problems"]:
            self.plist.insert("end", p["id"] + (f"   [{p['picks']}]" if p["picks"] else ""))
            if p["picks"]:
                self.plist.itemconfig("end", foreground="#1f8a4c")
        if cur is not None:
            self.plist.selection_set(cur)
        self.plist.yview_moveto(top)
        self.render_status()
        D = S["draft"]
        if not D:
            return
        self.problem.config(state="normal")
        self.problem.delete("1.0", "end")
        self.insert_segs(self.problem, self.segments(D["prompt"]))
        self.problem.config(state="disabled")
        text = "".join(t["text"] for t in D["tokens"])
        ex = D.get("extra") or {}
        ref, got = ex.get("answer"), last_boxed(text)
        ok = ref is not None and got is not None and norm(ref) == norm(got)
        self.meta.config(text=f"{D['id']} · {ex.get('level', '')} {ex.get('type', '')} · reference: {ref} · model: {got} · "
                              f"{'MATCH' if ok else 'no match'} (rough) · {len(D['tokens'])} tokens, ended by {D['ended_by']}",
                         foreground="#1f8a4c" if ok else "#b3261e")
        picks = {p["pos"]: p for p in D["picks"]}
        y = self.answer.yview()[0]
        self.answer.config(state="normal")
        self.answer.delete("1.0", "end")
        # One batched insert and a handful of styled ranges: a Tk tag per token (thousands of
        # tags) makes the text widget burn CPU on every redraw, badly so on macOS.
        texts = [t["text"] or "\u200b" for t in D["tokens"]]
        self.starts = [0]
        for x in texts:
            self.starts.append(self.starts[-1] + len(x))
        self.segs = self.segments("".join(texts))
        self.seg_src = [g[0] for g in self.segs]
        self.seg_disp = [0]
        for g in self.segs:
            self.seg_disp.append(self.seg_disp[-1] + len(g[2]))
        self.answer.config(wrap="word" if self.latex.get() else "char")
        self.insert_segs(self.answer, self.segs)
        for pos, pk in picks.items():
            self.mark("reinf" if pk["token"] == pk["original"] else "pick", pos)
        if self.sel is not None:
            self.mark("cur", self.sel)
        self.answer.config(state="disabled")
        self.answer.yview_moveto(y)
        self.side()

    def side(self):
        self.cands.delete(*self.cands.get_children())
        D = self.S and self.S["draft"]
        if not D or self.sel is None or self.sel >= len(D["tokens"]):
            self.sideh.config(text="Click a token")
            self.unpick_btn.config(text="Unselect")
            return
        t = D["tokens"][self.sel]
        pk = next((p for p in D["picks"] if p["pos"] == self.sel), None)
        self.sideh.config(text=f"Position {self.sel}: sampler probabilities")
        for k, c in enumerate(t["top"]):
            tags = ("here",) if c["token"] == t["id"] else ("orig",) if pk and c["token"] == pk["original"] else ()
            pct = c["prob"] * 100
            self.cands.insert("", "end", iid=str(k), tags=tags, values=(
                k if k < 10 else "", vis(c["text"]), f"{pct:.2f}%" if pct < 1 else f"{pct:.1f}%", "█" * max(0, round(c["prob"] * 20))))
        self.unpick_btn.config(text=("Unselect (drop reinforcement)" if pk["token"] == pk["original"]
                                     else f"Unselect (revert to {vis(pk['original_text'])})") if pk else "Unselect")


    def on_problem(self, _):
        s = self.plist.curselection()
        cur = self.S["draft"]["idx"] if self.S and self.S["draft"] else None
        if self.busy:
            self.plist.selection_clear(0, "end")
            if cur is not None:
                self.plist.selection_set(cur)
            return
        if s and s[0] != cur:
            self.sel = None
            p = self.S["problems"][s[0]]
            self.waiting(f"Generating the model's answer to {p['id']}…\n\n"
                         "About 70 tokens/s: most answers take 5–15 s, one that runs to the 4096-token cap about a minute.")
            self.call("open", {"idx": s[0]})

    def waiting(self, msg):
        self.problem.config(state="normal")
        self.problem.delete("1.0", "end")
        self.problem.config(state="disabled")
        self.meta.config(text="", foreground="#555")
        self.answer.config(state="normal")
        self.answer.delete("1.0", "end")
        self.answer.insert("1.0", msg)
        self.answer.config(state="disabled")
        self.cands.delete(*self.cands.get_children())

    def segments(self, text):
        if self.latex.get():
            return Tex(text).out
        return [(0, len(text), text, (), True)]

    def insert_segs(self, widget, segs):
        args, run, tags = [], "", None
        base = ("prose",) if self.latex.get() else ()
        for _, _, text, t, _ in segs:
            t = base + t
            if t != tags and run:
                args += [run, tags]
                run = ""
            run, tags = run + text, t
        if run:
            args += [run, tags]
        if args:
            widget.insert("end", *args)

    def spans(self, i):
        """Display ranges showing token i (newlines trimmed: a styled newline paints its line)."""
        a, b = self.starts[i], self.starts[i + 1]
        out = []
        k = max(0, bisect.bisect_right(self.seg_src, a) - 1)
        while k < len(self.segs) and self.segs[k][0] < b:
            s, e, text, _, plain = self.segs[k]
            d = self.seg_disp[k]
            if plain and text:
                lo, hi = max(a, s), min(b, e)
                if lo < hi:
                    out.append((d + lo - s, d + hi - s))
            elif text and (a <= s < b or (s == e == a)):
                out.append((d, d + len(text)))
            k += 1
        if not out:
            k = bisect.bisect_left(self.seg_src, b)
            while k < len(self.segs) and not self.segs[k][2]:
                k += 1
            if k < len(self.segs):
                out.append((self.seg_disp[k], self.seg_disp[k] + 1))
        res = []
        for lo, hi in out:
            text = self.answer.get(f"1.0 + {lo} chars", f"1.0 + {hi} chars")
            l2, h2 = len(text) - len(text.lstrip("\n")), len(text) - len(text.rstrip("\n"))
            if l2 == len(text):
                l2, h2 = 0, len(text) - 1
            res.append((f"1.0 + {lo + l2} chars", f"1.0 + {hi - h2} chars"))
        return res

    def mark(self, tag, i):
        if 0 <= i < len(self.starts) - 1:
            for r in self.spans(i):
                self.answer.tag_add(tag, *r)

    def select(self, i):
        self.sel = i
        self.answer.tag_remove("cur", "1.0", "end")
        self.mark("cur", i)
        r = self.spans(i)
        if r:
            self.answer.see(r[0][0])
        self.side()

    def on_token(self, e):
        if not self.S or not self.S["draft"]:
            return
        n = self.answer.count("1.0", self.answer.index(f"@{e.x},{e.y}"), "chars")
        n = (n[0] if isinstance(n, tuple) else n) or 0
        k = min(max(0, bisect.bisect_right(self.seg_disp, n) - 1), len(self.segs) - 1)
        while k > 0 and not self.segs[k][2]:
            k -= 1
        if k < 0 or not self.segs:
            return "break"
        s, e, _, _, plain = self.segs[k]
        src = s + min(n - self.seg_disp[k], max(0, e - s - 1)) if plain else s
        i = bisect.bisect_right(self.starts, src) - 1
        if 0 <= i < len(self.starts) - 1:
            self.select(i)
        return "break"

    def on_cand(self, _):
        s = self.cands.selection()
        if s and self.sel is not None and not self.busy:
            c = self.S["draft"]["tokens"][self.sel]["top"][int(s[0])]
            if c["token"] != self.S["draft"]["tokens"][self.sel]["id"]:
                self.waiting_status()
            self.call("pick", {"pos": self.sel, "token": c["token"]})

    def waiting_status(self):
        self.meta.config(text="Forking: the model is rewriting the answer from the picked token…", foreground="#555")

    def on_arrow(self, e):
        D = self.S and self.S["draft"]
        if D and not self.busy:
            n = len(D["tokens"])
            cur = self.sel if self.sel is not None else -1
            self.select(max(0, min(n - 1, cur + (1 if e.keysym == "Right" else -1))))

    def on_key(self, e):
        D = self.S and self.S["draft"]
        if not D or self.busy or self.sel is None:
            return
        if e.char.isdigit():
            top = D["tokens"][self.sel]["top"]
            if int(e.char) < len(top):
                self.call("pick", {"pos": self.sel, "token": top[int(e.char)]["token"]})
        elif e.char == "u":
            self.do_unpick()

    def do_unpick(self):
        D = self.S and self.S["draft"]
        if D and self.sel is not None and any(p["pos"] == self.sel for p in D["picks"]):
            self.call("unpick", {"pos": self.sel})

    def do_regen(self):
        D = self.S and self.S["draft"]
        if not D:
            return
        if D["picks"] and not messagebox.askyesno("manual-rl", "Drop the picks on this problem and regenerate?"):
            return
        self.sel = None
        self.call("regenerate")

    def do_apply(self):
        n = self.S["pending_picks"] if self.S else 0
        if not n:
            return messagebox.showinfo("manual-rl", "Nothing picked yet.")
        if messagebox.askyesno("manual-rl", f"Apply the gradient on {n} pick(s)?\nEvery answer is regenerated from the new weights."):
            self.sel = None
            self.call("apply")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8765")
    a = ap.parse_args()
    root = tk.Tk()
    App(root, a.url)
    root.mainloop()


if __name__ == "__main__":
    main()
