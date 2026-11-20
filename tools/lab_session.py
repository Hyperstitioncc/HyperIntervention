import argparse, json, math, os, re, sys, threading
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0, os.path.dirname(__file__))
import judge_session, lab_eval, session_base
from judge_client import SPEND, answer_text, bits, vis

NAMES = {"gen": "generation units", "tr": "training units", "val": "validation looks", "chk": "problem checks"}


def unvis(text):
    return re.sub(r"⟦\d+\|[^⟧]*⟧", "", str(text)).replace("·", " ").replace("⏎", "\n")


def anchor(d, before, current=""):
    toks, before, current = d["tokens"], unvis(before), unvis(current)
    if not before:
        raise ValueError('give "pos", or "before": the text of the answer just before the point')
    starts, n = [], 0
    for t in toks:
        starts.append(n)
        n += len(t["text"])
    at = {s: i for i, s in enumerate(starts)}
    at[n] = len(toks)
    full = answer_text(d)
    found = set()
    core = before.rstrip()
    k = full.find(core)
    while k >= 0:
        e = k + len(core)
        while e < len(full) and full[e] in " \t" and e not in at:
            e += 1
        i = at.get(e, at.get(k + len(core)))
        if i is not None and (not current or (i < len(toks) and toks[i]["text"].strip() == current.strip())):
            found.add(i)
        k = full.find(core, k + 1)
    if not found:
        raise ValueError(f"no token boundary right after {before!r}" + (f" holding {current!r}" if current else "") +
                         " (quote the answer's text exactly, up to the point)")
    if len(found) > 1:
        raise ValueError(f"{before!r} occurs {len(found)} times in the answer; quote more of the text before the point")
    return found.pop()


def span_end(d, pos, remove):
    toks, remove = d["tokens"], unvis(remove)
    n = 0
    for j in range(pos, len(toks)):
        got = "".join(t["text"] for t in toks[pos:j + 1])
        if got.strip() == remove.strip() and got.strip():
            return j + 1
        if len(got.lstrip()) > len(remove.strip()) + 1:
            break
    raise ValueError(f"the answer at the point does not read {remove!r} up to a token boundary (it reads "
                     f"{''.join(t['text'] for t in toks[pos:pos + 12])!r}...)")


def resolve(d, a):
    toks = d["tokens"]
    if "pos" in a:
        pos = int(a["pos"])
        if not 0 <= pos < len(toks):
            raise ValueError(f"position {pos} is outside the answer (0..{len(toks) - 1})")
        top = toks[pos]["top"]
        if "choice" in a:
            k = int(a["choice"])
            if not 0 <= k < len(top):
                raise ValueError(f"choice {k} is not in the candidate list at {pos} (0..{len(top) - 1})")
            return pos, top[k]["token"], None
        new = unvis(a.get("text", a.get("new", "")))
        if not new:
            raise ValueError('give "choice" (a candidate\'s number), or "text" (what to put in at pos)')
        hit = [c for c in top if c["text"] == new]
        return (pos, hit[0]["token"], None) if hit else (pos, None, new)
    pos = anchor(d, a.get("before", ""), a.get("current", ""))
    return resolve(d, {"pos": pos, "text": a.get("text", a.get("new", ""))})


class Lab(judge_session.Direct):

    def __init__(self, a):
        if a.model.startswith("scripted:") or a.own_pool:
            raise SystemExit("LAB sessions take a model judge on the given pool")
        a.task, a.no_tries = "lab", True
        if a.fix_only:
            a.min_corrections = 0
        super().__init__(a)
        self.total = {"gen": a.gen_budget, "tr": a.train_units, "val": a.val_looks, "chk": a.problem_checks}
        self.left = dict(self.total)
        self.template = lab_eval.rubric_prompt(a.lab)
        self.judge = lab_eval.Grader(a.judge, a.judge_usd)
        self.judge_lock = threading.Lock()
        self.original = {}

    def charge(self, kind, n, what):
        with self.mu:
            self.left[kind] -= int(n)
        used = 1 - max(0, self.left[kind]) / max(1, self.total[kind])
        return f"[{int(n):,} {NAMES[kind]} for {what}; {max(0, self.left[kind]):,} left: {used:.1%} of this budget used so far]"

    def need(self, kind, n, what):
        have = self.left[kind] - self.reserved.get(kind, 0)
        if have < n:
            raise ValueError(f"{what} needs about {int(n):,} {NAMES[kind]} and {max(0, have):,} are left")

    def budget(self):
        return ("Left: " + "; ".join(f"{max(0, self.left[k]):,} of {self.total[k]:,} {NAMES[k]} "
                                     f"({1 - max(0, self.left[k]) / max(1, self.total[k]):.1%} used)" for k in NAMES)
                + f". Judge spend so far: ${SPEND['usd']:.2f}.")

    def prompt_vars(self):
        return {}

    def go_live(self, name):
        session_base.Session.go_live(self, name)
        self.original = {}
        out = f"{name} is the live student. Its answer to a pool task is written when you open the task."
        if name == "v0" and not self.versions["v0"].get("val"):
            out += "\n" + self.validate_live(charge=False)
        return out

    def screen(self, seat, n=None, ids=None, prev=None):
        seat.status = {}

    def catch_up(self, seat):
        pass

    def listed(self, i):
        return True

    def validate(self, name):
        self.versions[name]["val"] = {}
        return {}

    @staticmethod
    def outline(text):
        out = []
        for line in text.splitlines():
            t = line.strip()
            if re.match(r"(#+\s|\d+[.)]\s|[-*]\s+\*\*|\*\*\S)", t):
                words = re.sub(r"^[\d\s]+", "", re.sub(r"[^a-z0-9]+", " ", t.lower())).strip()
                if len(words) >= 12:
                    out.append((t[:160], words[:60]))
        return out

    def lost(self, i):
        if i not in self.original:
            return ""
        d = self.call("open", idx=i)["draft"]
        if d["ended_by"] == "paused":
            return ""
        now = re.sub(r"[^a-z0-9]+", " ", answer_text(d).lower())
        gone = [line for line, words in self.outline(self.original[i]) if words not in now]
        if not gone:
            return "\nEvery item of the student's original answer (headings, numbered and bold issue lines) is still in the answer."
        return (f"\nItems of the student's original answer no longer in the answer as it now stands ({len(gone)}; repair them with "
                "further forks, or decide they were wrong):\n" + "\n".join(f"  - {x}" for x in gone[:40]))

    def first_seen(self, act):
        i = self.idx_of(act)
        d = self.call("open", idx=i)["draft"]
        if not d["picks"]:
            self.note(i, d)

    def do_fork(self, act):
        self.first_seen(act)
        i = self.idx_of(act)
        d = self.call("open", idx=i)["draft"]
        pos, tok, text = resolve(d, act)
        write = act.get("write", "all")
        n = None if write == "all" else max(0, int(write))
        self.need("gen", self.a.call_cost + (n if n is not None else 200), "a fork")
        temp = float(act.get("temperature", self.a.gen_temperature))
        r = self.call("pick", idx=i, pos=pos, **({"token": tok} if tok is not None else {"text": text}), min_prob=self.a.min_prob,
                      up=int(act.get("up", 1)), down=int(act.get("down", 0)), temperature=temp, **({} if n is None else {"max_new": n}))
        old, nd = d["tokens"][pos], r["draft"]
        k = next((p["n"] for p in nd["picks"] if p["pos"] == pos), 1)
        put = nd["tokens"][pos:pos + k]
        prob = min(t["prob"] for t in put)
        note = ((f"The token you put in at {pos} ({pos / max(1, len(d['tokens'])):.0%} of the way through the answer) had probability {prob:.1%} there"
                 if k == 1 else
                 f"The {k} tokens you put in from {pos} ({pos / max(1, len(d['tokens'])):.0%} of the way through the answer) had probabilities "
                 + ", ".join(f"{vis(t['text'])!r} {t['prob']:.1%}" for t in put) + f" (the lowest {prob:.1%})")
                + f"; the student's own choice at {pos}, {vis(old['text'])!r}, had {old['prob']:.1%}."
                + (" A token the student gave so little takes many training steps to accept." if prob < 0.02 else ""))
        st = nd.get("stopped")
        if st:
            note += (f"\nYour text went in for {k} of its {st['asked']} tokens: at {st['pos']} the student gives {vis(st['token'])!r} "
                     f"probability {st['prob']:.2g}, under the floor of {st['floor']:g}. The answer pauses at {st['pos']}, on the student's own "
                     f"choice there. What the student allows at {st['pos']} (fork {st['pos']} with one of these choices, or with text that "
                     "starts with one of them; or continue): "
                     + ", ".join(f"{c['choice']}:{vis(c['text'])!r} {c['prob']:.1%}" for c in st["allowed"]))
        if r["generated"] and temp > 0:
            note += f" What follows the fork was sampled at temperature {temp:g}: forking the same token again gives another continuation."
        return (self.charge("gen", self.a.call_cost + r["generated"], f"the fork and {r['generated']} tokens written") + "\n" + note
                + f"\nTask {i}, from the fork at {pos} on:\n{self.show(r['draft'], max(0, pos - 12))}" + self.lost(i))

    def do_continue(self, act):
        i = self.idx_of(act)
        write = act.get("write", "all")
        n = None if write == "all" else max(1, int(write))
        before = len(self.call("open", idx=i)["draft"]["tokens"])
        self.need("gen", self.a.call_cost + (n or 200), "writing on")
        r = self.call("continue", idx=i, temperature=float(act.get("temperature", self.a.gen_temperature)), **({} if n is None else {"max_new": n}))
        return (self.charge("gen", self.a.call_cost + r["generated"], f"{r['generated']} tokens written")
                + f"\nTask {i}, from token {before} on:\n{self.show(r['draft'], max(0, before - 12))}" + self.lost(i))

    def do_options(self, act):
        i = self.idx_of(act)
        d = self.call("open", idx=i)["draft"]
        pos = int(act["pos"]) if "pos" in act else anchor(d, act.get("before", ""))
        text = unvis(act.get("text", ""))
        r = self.call("options", idx=i, pos=pos, text=text)
        f = self.a.min_prob
        allowed = lambda top: ", ".join(f"{vis(c['text'])!r} {c['prob']:.1%}" for c in top if c["prob"] >= f) or "nothing above the floor"
        out = [f"Task {i}, at {pos} (after {vis(''.join(t['text'] for t in d['tokens'][max(0, pos - 8):pos]))!r}):"]
        bad = next((k for k, t in enumerate(r["put"]) if t["prob"] < f), None)
        if r["put"]:
            out.append("your text, token by token (probability; ✗ = under the floor of " + f"{f:g}" + "): "
                       + " ".join(f"{vis(t['text'])!r} {t['prob']:.2g}{' ✗' if t['prob'] < f else ''}" for t in r["put"]))
        if bad is not None:
            out.append(f"It would stop at its token {bad + 1} ({vis(r['put'][bad]['text'])!r}). What the student allows there instead: {allowed(r['put'][bad]['top'])}")
        else:
            out.append(("All of it is above the floor. " if r["put"] else "") + f"What the student allows next: {allowed(r['after'])}")
        return "\n".join(out)

    def do_edit(self, act):
        self.first_seen(act)
        i = self.idx_of(act)
        d = self.call("open", idx=i)["draft"]
        if d["ended_by"] == "paused":
            raise ValueError("the answer is unfinished; continue it to the end before editing it")
        pos = int(act["pos"]) if "pos" in act else anchor(d, act.get("before", ""))
        end = span_end(d, pos, act["remove"]) if act.get("remove") else pos
        text = unvis(act.get("text", ""))
        if not text:
            raise ValueError('give "text": what to put in')
        self.need("gen", self.a.call_cost, "an edit")
        r = self.call("splice", idx=i, pos=pos, end=end, text=text, min_prob=self.a.min_prob, up=int(act.get("up", 1)), down=int(act.get("down", 0)))
        nd = r["draft"]
        k = next((p["n"] for p in nd["picks"] if p["pos"] == pos), 1)
        put = nd["tokens"][pos:pos + k]
        gone = "".join(t["text"] for t in d["tokens"][pos:end])
        note = (f"Put in at {pos} ({pos / max(1, len(d['tokens'])):.0%} of the way through the answer)" + (f", in place of {vis(gone)!r}" if gone else "")
                + f": {k} tokens, with probabilities " + ", ".join(f"{vis(t['text'])!r} {t['prob']:.1%}" for t in put)
                + f" (the lowest {min(t['prob'] for t in put):.1%}). The rest of the answer is as it was, read again after your text.")
        return (self.charge("gen", self.a.call_cost, "the edit") + "\n" + note
                + f"\nTask {i}, around the edit:\n{self.show(nd, max(0, pos - 12), pos + k + 40)}" + self.lost(i))

    def do_check(self, act):
        i = self.idx_of(act)
        if self.total["chk"] <= 0:
            raise ValueError("there are no answer checks in this session: judge the answer yourself against its rubric")
        self.need("chk", 1, "an answer check")
        d = self.call("open", idx=i)["draft"]
        if d["ended_by"] == "paused":
            raise ValueError("the answer is unfinished; continue it to the end before checking it")
        r = self.rows[i]
        vs = self.grade_task(r, answer_text(d), cut=False)
        by = {v["criterion"]: v for v in vs}
        ok = sum(v["verdict"] == "pass" for v in vs)
        lines = [f"[{c['id']}] {'PASS' if by.get(c['id'], {}).get('verdict') == 'pass' else 'FAIL'}: {c['title']}"
                 + ("" if by.get(c["id"], {}).get("verdict") == "pass" else f" ({by.get(c['id'], {}).get('reasoning', '')[:200]})")
                 for c in r["criteria"]]
        return (f"Task {i}, the answer as it now stands, judged: {ok} of {len(r['criteria'])} criteria passed.\n" + "\n".join(lines)
                + "\n" + self.charge("chk", 1, "this check"))

    def grade_task(self, row, answer, cut=False):
        out = []
        lab_eval.grade_answer(self.judge, self.template, row, answer, row["criteria"], 8, out.append, cut)
        return out

    def save0(self):
        json.dump({str(k): v for k, v in self.status0.items()}, open(os.path.join(self.a.run, "status0.json"), "w"))

    def note(self, i, d, seat=None):
        seat = seat or self.seat
        s = {"tokens": len(d["tokens"]), "ended": d["ended_by"]}
        seat.status[i] = s
        self.status[i] = s
        if seat is self.seat and not d["picks"]:
            self.original[i] = answer_text(d)
        if seat.name == "v0" and i not in self.status0 and not d["picks"]:
            self.status0[i] = s
            self.save0()
            lab_eval.append(os.path.join(self.a.run, "v0-pool-answers.jsonl"), [{"id": self.rows[i]["id"], "answer": answer_text(d)}])
        return s

    def do_list(self, act):
        area, off = act.get("area") or act.get("subject"), int(act.get("offset", 0))
        area = None if area == "all" else area
        made = {}
        for k in self.kept():
            made.setdefault(k["id"], []).append(f"kept@v{k['round']}")
        rows = []
        for i, r in enumerate(self.rows):
            if not self.listed(i) or (area and area not in r["area"]):
                continue
            s = self.status.get(i)
            seen = f"{s['tokens']} tok{' (cut off)' if s['ended'] != 'eos' else ''}" if s else "not opened"
            if not self.trainable(i):
                seen += " (too long to train)"
            rows.append(f"{i} | {r['area']} | {r.get('work_type') or '-'} | {r['title'][:90]} | {r['tokens']:,} | "
                        f"{len(r['criteria'])} | {seen} | {' '.join(made.get(r['id'], [])) or '-'}")
        page = rows[off:off + 100]
        by = {}
        for i, r in enumerate(self.rows):
            if self.listed(i):
                by[r["area"]] = by.get(r["area"], 0) + 1
        return (f"Pool: {sum(by.values())} tasks by area: " + "; ".join(f"{k} {n}" for k, n in sorted(by.items())) +
                f"\n{len(rows)} tasks match (showing {off}-{off + len(page)}):\n"
                "idx | area | work type | title | prompt tokens | criteria | live answer | corrections kept\n" + "\n".join(page))

    def documents(self, i):
        r = self.rows[i]
        parts = re.split(r"(?m)^=== documents/(.+?) ===\n", r["prompt"])
        return {parts[k]: parts[k + 1].rstrip("\n") for k in range(1, len(parts) - 1, 2)}

    def rubric(self, i):
        return "\n".join(f"[{c['id']}] {c['title']}\n    {c['match_criteria']}" for c in self.rows[i]["criteria"])

    def do_open(self, act):
        i = self.idx_of(act)
        r = self.rows[i]
        d = self.call("open", idx=i)["draft"]
        s = self.note(i, d)
        docs = "\n".join(f"  {p}  ({len(t):,} characters)" for p, t in self.documents(i).items())
        was = self.status0.get(i)
        then = "" if self.live == "v0" or not was else f" (the untrained student's answer: {was['tokens']} tokens)"
        return (f"Task {i} ({r['id']}; {r['area']}, {r.get('work_type') or '-'}): {r['title']}\n"
                f"INSTRUCTIONS (as the student got them, followed by the documents):\n{r['instructions'].strip()}\n"
                + ("" if self.trainable(i, s["tokens"]) else f"This task is too long to train on (over {self.a.max_train_tokens:,} tokens with its answer): read it, but keep corrections elsewhere.\n")
                + f"Deliverable: {r['deliverable']}. Documents ({r['tokens']:,} prompt tokens in all; read them with the read action):\n{docs}\n"
                f"RUBRIC ({len(r['criteria'])} criteria; the judge grades held-out answers against rubrics like this one, one criterion at a time):\n"
                f"{self.rubric(i)}\n"
                f"{self.live}'s answer as it now stands ({s['tokens']} tokens{then}):\n{self.show(d)}")

    def do_read(self, act):
        i = self.idx_of(act)
        docs = self.documents(i)
        path = str(act.get("path", ""))
        if path not in docs:
            hits = [p for p in docs if path and path in p]
            if len(hits) != 1:
                raise ValueError(f"path must be one of task {i}'s documents: {sorted(docs)}")
            path = hits[0]
        text = docs[path]
        lo = max(0, int(act.get("from", 0)))
        n = max(1000, min(40_000, int(act.get("chars", 8_000))))
        piece = text[lo:lo + n]
        more = f" Characters {lo + len(piece):,}-{len(text):,} follow: read on with \"from\": {lo + len(piece)}." if lo + len(piece) < len(text) else ""
        return f"Task {i}, documents/{path}, characters {lo:,}-{lo + len(piece):,} of {len(text):,}.{more}\n<document>\n{piece}\n</document>"

    def best(self):
        return "v0" if self.a.fix_only else super().best()

    def trainable(self, i, answer_tokens=0):
        return self.rows[i]["tokens"] + 32 + answer_tokens <= self.a.max_train_tokens

    def do_keep(self, act):
        i = self.idx_of(act)
        d = self.call("open", idx=i)["draft"]
        if not self.trainable(i, len(d["tokens"])):
            raise ValueError(f"task {i}'s documents and answer ({self.rows[i]['tokens'] + len(d['tokens']):,} tokens) are longer than the "
                             f"{self.a.max_train_tokens:,} tokens one correction may have: it cannot be trained. Work on another task")
        self.call("commit", idx=i, meta={"editor": self.a.model, "forks": len(d["picks"]), "marks": len(d.get("marks") or {})})
        n = max(1, len(d["tokens"]))
        forks = "; ".join(f"@{p['pos']} ({p['pos'] / n:.0%} in)" for p in d["picks"])
        self.call("drop", idx=i)
        self.status.pop(i, None)
        new = [k for k in self.kept() if k["key"] not in set(self.versions[self.live]["included"])]
        return (f"Task {i} kept as a correction ({len(d['picks'])} forks{': ' + forks if forks else ''}; {len(d.get('marks') or {})} weighted tokens). "
                f"{len(new)} kept corrections are not yet trained into {self.live}.")

    def acts(self):
        return {**{k: v for k, v in super().acts().items() if k not in ("pose", "wait")},
                "read": self.do_read, "check": self.do_check, "options": self.do_options, "edit": self.do_edit}

    def finish_check(self, version):
        if self.a.fix_only:
            return None
        return super().finish_check(version)

    def do_train(self, act):
        if self.a.fix_only:
            raise ValueError("this session only fixes answers: training is off. Keep your corrections and finish when you are done")
        return super().do_train(act)

    def do_validate(self, act):
        self.need("val", 1, "a validation look")
        return self.validate_live(charge=self.left["val"] > 0)

    def do_eval(self, act):
        if act.get("problems"):
            raise ValueError('pool answers are judged with "check" (when the session has checks), or by you against the rubric')
        self.need("val", 1, "a validation look")
        return self.validate_live(n=max(1, min(len(self.val_rows), int(act.get("n", len(self.val_rows))))))

    def effect(self, trained, seat=None):
        seat = seat or self.seat
        by_id = {r["id"]: n for n, r in enumerate(self.rows)}
        want = {}
        for k in self.kept():
            if by_id.get(k["id"]) in set(trained):
                want[by_id[k["id"]]] = json.loads(k["line"])["episode"]

        def one(i):
            d = self.ask(seat.srv, "open", idx=i)["draft"]
            self.note(i, d, seat)
            own, ep = [t["id"] for t in d["tokens"]], want[i]
            same = next((q for q, (x, y) in enumerate(zip(own, ep["completion"])) if x != y), min(len(own), len(ep["completion"])))
            first = min((f["pos"] for f in ep["forks"]), default=None)
            return i, len(own), d["ended_by"], same, len(ep["completion"]), first
        with ThreadPoolExecutor(16) as ex:
            rows = sorted(ex.map(one, sorted(want)))
        lines = [f"{i}: {n} tokens{' (cut off)' if e != 'eos' else ''}; writes your correction by itself for its first {same} of {m} tokens"
                 + (f" (first fork at {f}: {'past it' if f is not None and same > f else 'NOT past it'})" if f is not None else "")
                 + (f"; untrained answer {self.status0[i]['tokens']} tokens" if i in self.status0 else "")
                 for i, n, e, same, m, f in rows]
        held = sum(1 for _, _, _, same, _, f in rows if f is not None and same > f)
        return (f"What {seat.name} now writes by itself (its single most likely answer) on the {len(rows)} tasks it was trained on; "
                f"{held} of them follow your correction past its first fork:\n" + "\n".join(lines) +
                "\nOther pool tasks are not regraded: open one to read the live version's answer.")

    def held_out(self, rows, base, srv):
        ans_f, ver_f = base + "-answers.jsonl", base + "-verdicts.jsonl"
        have = {x["id"]: x["answer"] for x in lab_eval.load(ans_f)}
        cut = {x["id"] for x in lab_eval.load(ans_f) if x.get("finish") == "length"}
        graded = {}
        for v in lab_eval.load(ver_f):
            graded.setdefault(v["id"], []).append(v)

        def one(r):
            if r["id"] not in have:
                got = self.ask(srv, "sample", prompt=r["prompt"], k=1, temperature=0.0)["answers"][0]
                have[r["id"]] = got["text"]
                if got.get("finish") == "length":
                    cut.add(r["id"])
                lab_eval.append(ans_f, [{"id": r["id"], "answer": got["text"], "finish": got.get("finish")}], self.judge_lock)
            vs = graded.get(r["id"], [])
            if len(vs) < len(r["criteria"]):
                vs = self.grade_task(r, have[r["id"]], cut=r["id"] in cut)
                if len(vs) < len(r["criteria"]):
                    raise RuntimeError(f"the judge's cap (${self.a.judge_usd:g}) is reached; raise --judge-usd to go on")
                lab_eval.append(ver_f, vs, self.judge_lock)
            return r["id"], sum(v["verdict"] == "pass" for v in vs[-len(r["criteria"]):]) / len(r["criteria"])
        with ThreadPoolExecutor(64) as ex:
            return dict(ex.map(one, rows))

    def val_answers(self, seat=None):
        seat = seat or self.seat
        return list(self.held_out(self.val_rows, os.path.join(self.a.run, "versions", f"{seat.name}-val"), seat.srv).items())

    def val_record(self, name, out, charge=True):
        out = dict(out)
        with open(os.path.join(self.a.run, "versions", f"{name}-val.jsonl"), "w") as f:
            f.writelines(json.dumps({"id": i, "correct": c, "completion": ""}) + "\n" for i, c in out.items())
        self.versions[name]["val"] = out
        allp = sum(c == 1 for c in out.values())
        return (f"Validation, {name} (one greedy answer per task, every criterion judged): score {self.points(out):.2f} "
                f"of {len(self.counted)} (mean criterion pass rate {self.points(out) / len(self.counted):.1%}; "
                f"{allp} tasks with every criterion passed)"
                + (f"; {self.versus('v0', name)}" if name != "v0" and self.versions["v0"].get("val") else "") + "."
                + f" Judge spend so far ${self.judge.spent:.2f}."
                + ("\n" + self.charge("val", 1, "this validation score") if charge else ""))

    def validate_live(self, charge=True, n=None, k=None, first=0):
        if n is None or n >= len(self.val_rows):
            return self.val_record(self.live, self.val_answers(), charge)
        out = self.held_out(self.val_rows[:n], os.path.join(self.a.run, "versions", f"{self.live}-eval{n}"), self.seat.srv)
        return (f"{self.live} on the first {len(out)} validation tasks (one greedy answer each): mean criterion pass rate "
                f"{sum(out.values()) / len(out):.1%}." + ("\n" + self.charge("val", 1, "this validation look") if charge else ""))

    def test_scores(self, model_dir, name):
        seat = self.seats[0]
        seat.clear()
        seat.srv = seat.student.start(model_dir, 0)
        try:
            return self.held_out([json.loads(l) for l in open(self.a.test)], os.path.join(self.a.eval_out, f"{name}-test"), seat.srv)
        finally:
            seat.clear()

    def score_test(self, handed, result, ks):
        tag = f"{os.path.basename(os.path.normpath(self.a.run))}-{handed}"
        B = self.test_scores(self.base, f"base-{self.a.dtype}")
        T = self.test_scores(self.versions[handed]["dir"], tag)
        ids = sorted(B)
        d = [T[i] - B[i] for i in ids]
        n, mean = len(d), sum(d) / len(d)
        se = math.sqrt(sum((x - mean) ** 2 for x in d) / max(1, n - 1) / n)
        result["test"] = {"n": n, "base_pass_rate": sum(B.values()) / n, "pass_rate": sum(T.values()) / n,
                          "change": mean, "se": se, "base_all_pass": sum(B[i] == 1 for i in ids), "all_pass": sum(T[i] == 1 for i in ids),
                          "tasks_up": sum(x > 0 for x in d), "tasks_down": sum(x < 0 for x in d), "judge": self.a.judge}
        result["score"] = {"change_in_criterion_pass_rate": mean, "se": se, "of": n}
        result["judge_usd"] = round(self.judge.spent, 2)
        t = result["test"]
        print(f"TEST, {handed} against the untrained student on {n} tasks: mean criterion pass rate {t['base_pass_rate']:.1%} -> "
              f"{t['pass_rate']:.1%} ({mean:+.1%}, paired standard error {se:.1%}); all criteria passed {t['base_all_pass']} -> {t['all_pass']}",
              file=sys.stderr, flush=True)

    def score_fixes(self):
        before = {x["id"]: x["answer"] for x in lab_eval.load(os.path.join(self.a.run, "v0-pool-answers.jsonl"))}
        by_id = {r["id"]: r for r in self.rows}
        out = []
        for k in self.kept():
            ep = json.loads(k["line"])["episode"]
            r = by_id[k["id"]]
            if r["id"] not in before:
                continue
            text = self.episode_text(ep)
            b = {v["criterion"]: v["verdict"] == "pass" for v in self.grade_task(r, before[r["id"]])}
            a_ = {v["criterion"]: v["verdict"] == "pass" for v in self.grade_task(r, text)}
            titles = {c["id"]: c["title"] for c in r["criteria"]}
            out.append({"id": r["id"], "n": len(r["criteria"]), "before": sum(b.values()), "after": sum(a_.values()),
                        "newly_passed": [titles[c] for c in a_ if a_[c] and not b.get(c)],
                        "newly_failed": [titles[c] for c in a_ if b.get(c) and not a_[c]],
                        "forks": [f["pos"] for f in ep["forks"]], "answer_tokens": len(ep["completion"])})
            print(f"FIX {r['id']}: {out[-1]['before']}/{out[-1]['n']} -> {out[-1]['after']}/{out[-1]['n']} criteria passed "
                  f"(forks at {out[-1]['forks']}); newly passed {out[-1]['newly_passed']}; newly failed {out[-1]['newly_failed']}",
                  file=sys.stderr, flush=True)
        return out

    @staticmethod
    def episode_text(ep):
        return re.sub(r"<\|(im_end|endoftext)\|>", "", ep["text"]).strip()

    def finish(self, handed, turns):
        fixes = self.score_fixes() if self.a.fix_only else None
        if not self.a.fix_only:
            os.environ["FINAL_GREEDY"] = "0"
        super().finish(handed, turns)
        p = os.path.join(self.a.run, "result.json")
        res = json.load(open(p))
        res["judge_usd"] = round(self.judge.spent, 2)
        if not self.a.fix_only and handed != "v0":
            self.score_test(handed, res, None)
        if fixes is not None:
            res["fixes"] = {"corrections": fixes, "tasks": len(fixes), "criteria": sum(x["n"] for x in fixes),
                            "passed_before": sum(x["before"] for x in fixes), "passed_after": sum(x["after"] for x in fixes)}
            print(f"FIXES: {len(fixes)} corrections; criteria passed {res['fixes']['passed_before']} -> {res['fixes']['passed_after']} "
                  f"of {res['fixes']['criteria']}", file=sys.stderr, flush=True)
        json.dump(res, open(p, "w"), indent=1)


def parser():
    ap = session_base.parser()
    ap.set_defaults(no_tries=True)
    ap.add_argument("--own-pool", action="store_true", help="not for LAB: the pool starts empty and the judge writes its problems")
    ap.add_argument("--pose", action="store_true", help="not for LAB: the judge may add problems of its own to the given pool")
    ap.add_argument("--max-own", type=int, default=600, help="most problems the judge may add")
    ap.add_argument("--key", action="store_true", help="not for LAB: pool problems carry a reference final answer")
    ap.add_argument("--pool-k", type=int, default=0, help="not for LAB: every version answers each pool problem this many times, sampled")
    ap.add_argument("--pool-sample", type=int, default=0, help="not for LAB: pool problems answered again after a training run")
    ap.add_argument("--lab", required=True, help="the LAB checkout the tasks were built from (its judge prompt)")
    ap.add_argument("--test", required=True, help="the sealed test tasks (tools/lab_tasks.py split)")
    ap.add_argument("--judge", default=lab_eval.GRADER, help="the judge (OpenRouter model) of every held-out answer")
    ap.add_argument("--judge-usd", type=float, default=150.0, help="most the judge may spend in this session, test included")
    ap.add_argument("--fix-only", action="store_true",
                    help="no training: the judge fixes pool answers, and each kept correction is judged against the answer it was made from")
    ap.add_argument("--max-train-tokens", type=int, default=125_000, help="longest correction (documents and answer) the trainer takes")
    ap.add_argument("--min-prob", type=float, default=0.001,
                    help="every token put into an answer must be one the student itself gives at least this probability there")
    ap.add_argument("--mark-below", type=float, default=0.5, help="a token the student gave less than this is shown with its probability")
    ap.add_argument("--call-cost", type=int, default=50, help="generation units charged for every generation call, on top of its tokens")
    ap.add_argument("--min-corrections", type=int, default=0,
                    help="the untrained student cannot be handed in before this many corrections are kept or half the generation budget is used")
    ap.add_argument("--min-kept", type=int, default=0, help="no version can be handed in before this many corrections are kept (0: no such floor)")
    ap.add_argument("--grad-budget", type=int, default=0, help="the most trained tokens a version may have in it (0: no such limit)")
    ap.add_argument("--max-trainings", type=int, default=0, help="most versions a session makes (0: no such limit)")
    ap.add_argument("--auto-cap", type=int, default=40, help="most steps a train with steps \"auto\" takes")
    ap.add_argument("--candidates", type=int, default=-1, help="most training runs one train action may make at once")
    ap.add_argument("--tau", type=float, default=0.5, help="entropy above which a position counts as undecided")
    ap.add_argument("--branch", type=int, default=25, help="undecided positions shown with an answer")
    ap.add_argument("--show", type=int, default=8, help="candidates shown at an undecided position")
    ap.set_defaults(gen_budget=400_000, train_units=600_000_000, val_looks=12, eval_k=1, problem_checks=0, context_chars=80_000,
                    max_reply=64000, gen_temperature=0.0, default_optimizer="adam")
    return ap


def main():
    session_base.main(parser(), Lab)


if __name__ == "__main__":
    main()
