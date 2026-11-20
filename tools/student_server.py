import argparse, asyncio, fcntl, json, math, os, re, tomllib, uuid

from aiohttp import web
from transformers import AutoTokenizer
from vllm import SamplingParams
from vllm.engine.arg_utils import AsyncEngineArgs
from vllm.v1.engine.async_llm import AsyncLLM

try:
    from vllm.sampling_params import RequestOutputKind
    FINAL_ONLY = RequestOutputKind.FINAL_ONLY
except Exception:
    FINAL_ONLY = None
TOP = 20


def load_prompts(path):
    out = []
    for i, line in enumerate(open(path)):
        if not line.strip():
            continue
        obj = json.loads(line)
        prompt = obj.pop("prompt")
        pid = obj.pop("id", str(i))
        out.append({"id": pid, "prompt": prompt, **({"extra": obj} if obj else {})})
    return out


class Steer:
    def __init__(self, a):
        cfg = tomllib.load(open(a.config, "rb"))
        ep = cfg.get("episode", {})
        self.model, self.run_dir, self.template = cfg["model"], cfg["run_dir"], cfg["template"]
        self.read = cfg.get("read", "")
        self.max_new = ep.get("sampling", {}).get("max_new_tokens", 4096)
        self.max_len = a.max_model_len or self.max_new + 2048
        self.round = getattr(a, "round", 0)
        self.tok = AutoTokenizer.from_pretrained(self.model)
        gen = json.load(open(os.path.join(self.model, "generation_config.json")))
        eos = gen.get("eos_token_id", [])
        self.eos = ep.get("eos") or (eos if isinstance(eos, list) else [eos])
        self.prompts = load_prompts(cfg["prompts"])
        os.makedirs(self.run_dir, exist_ok=True)
        self.engine = AsyncLLM.from_engine_args(AsyncEngineArgs(
            model=self.model, dtype=a.dtype, max_model_len=self.max_len, seed=0,
            gpu_memory_utilization=a.gpu_mem, enable_prefix_caching=True, max_logprobs=TOP,
            **({"max_num_seqs": a.max_num_seqs} if getattr(a, "max_num_seqs", 0) else {}),
            **({"moe_backend": os.environ["STUDENT_MOE_BACKEND"]} if os.environ.get("STUDENT_MOE_BACKEND") else {})))
        self.drafts, self.locks = {}, {}
        self.rolls, self.base = {}, {}
        self.current, self.version, self.notes = None, 0, []
        self.generated = 0
        self.commit_lock = asyncio.Lock()

    async def generate(self, prompt_tokens, prefix, limit=None, temperature=0.0, seed=None):
        if prefix and prefix[-1]["id"] in self.eos:
            return [], "eos"
        budget = self.max_new - len(prefix)
        if budget <= 0:
            return [], "max_tokens"
        paused = limit is not None and limit < budget
        if paused:
            budget = max(1, int(limit))
        t = float(temperature or 0.0)
        sp = SamplingParams(output_kind=FINAL_ONLY, temperature=t, max_tokens=budget, logprobs=TOP, stop_token_ids=self.eos,
                            detokenize=False, skip_special_tokens=False,
                            **({"top_p": 0.95, "seed": uuid.uuid4().int % (2 ** 31) if seed is None else int(seed)} if t > 0 else {}))
        ids = prompt_tokens + [t["id"] for t in prefix]
        final = None
        async for out in self.engine.generate({"prompt_token_ids": ids}, sp, request_id=uuid.uuid4().hex):
            final = out
        o = final.outputs[0]
        toks = [self.as_tok(tid, lps) for tid, lps in zip(o.token_ids, o.logprobs)]
        self.generated += len(toks)
        if o.finish_reason == "length":
            return toks, "paused" if paused else "max_tokens"
        if toks and toks[-1]["id"] not in self.eos and isinstance(o.stop_reason, int):
            toks.append({"id": o.stop_reason, "prob": 1.0, "entropy": 0.0, "top": [{"token": o.stop_reason, "prob": 1.0}]})
        return toks, "eos"

    @staticmethod
    def as_tok(tid, lps):
        cands = sorted(((t, math.exp(lp.logprob)) for t, lp in lps.items()), key=lambda c: -c[1])
        top = [{"token": t, "prob": p} for t, p in cands[:TOP]]
        probs = [p for _, p in cands[:TOP]]
        rest = max(0.0, 1.0 - sum(probs))
        ent = -sum(p * math.log(p) for p in probs if p > 0) - (rest * math.log(rest) if rest > 0 else 0.0)
        return {"id": tid, "prob": math.exp(lps[tid].logprob) if tid in lps else 0.0, "entropy": ent, "top": top}

    def encode(self, idx):
        text = self.template.replace("{prompt}", self.prompts[idx]["prompt"])
        return self.tok(text, add_special_tokens=False)["input_ids"]

    async def open(self, idx, fresh, rollout=None):
        if rollout is not None:
            rolls = self.rolls.pop(idx, None)
            if rolls is None or not 0 <= int(rollout) < len(rolls):
                raise ValueError("no such sampled answer: ask for the problem's rollouts first")
            self.base[idx] = {"prompt_tokens": self.encode(idx), **rolls[int(rollout)]}
            self.drafts.pop(idx, None)
        if fresh and self.read != "plain":
            self.base.pop(idx, None)
        if idx in self.base:
            if idx not in self.drafts:
                b = self.base[idx]
                self.drafts[idx] = {"prompt_tokens": b["prompt_tokens"], "toks": list(b["toks"]), "picks": [], "ended_by": b["ended_by"], "marks": {}}
            return
        if fresh or idx not in self.drafts:
            pt = self.encode(idx)
            toks, ended = await self.generate(pt, [])
            if self.read == "plain":
                self.base[idx] = {"prompt_tokens": pt, "toks": toks, "ended_by": ended}
            self.drafts[idx] = {"prompt_tokens": pt, "toks": toks, "picks": [], "ended_by": ended, "marks": {}}

    def unpick(self, idx, pos):
        d = self.drafts[idx]
        i = next((k for k, p in enumerate(d["picks"]) if p["pos"] == pos), None)
        if i is None:
            raise ValueError("no pick at that position")
        pk = d["picks"][i]
        d["marks"] = {q: w for q, w in d.get("marks", {}).items() if q < pos}
        if pk["token"] == pk["original"]:
            d["picks"].pop(i)
            return
        d["toks"] = d["toks"][:pos] + pk["tail"]
        d["picks"] = d["picks"][:i] + pk["later_picks"]
        d["ended_by"] = pk["ended_by"]

    async def more(self, idx, limit=None, temperature=0.0):
        d = self.drafts[idx]
        if d["ended_by"] != "paused":
            raise ValueError("the answer is complete; fork a token to change it")
        new, ended = await self.generate(d["prompt_tokens"], d["toks"], limit, temperature)
        d["toks"].extend(new)
        d["ended_by"] = ended

    async def read_out(self, pt, text):
        from math_eval import FINISH_TEXT, needs_finish
        if not needs_finish(text):
            return text, 0
        ids = pt + self.tok(text.rstrip() + FINISH_TEXT, add_special_tokens=False)["input_ids"]
        sp = SamplingParams(output_kind=FINAL_ONLY, temperature=0.0, max_tokens=64, stop=["\n"], stop_token_ids=self.eos, skip_special_tokens=True)
        fin = None
        async for out in self.engine.generate({"prompt_token_ids": ids[-(self.max_len - 80):]}, sp, request_id=uuid.uuid4().hex):
            fin = out
        return text.rstrip() + FINISH_TEXT + fin.outputs[0].text, len(fin.outputs[0].token_ids)

    async def rollouts(self, idx, k, temperature, seed):
        from math_eval import read
        pt = self.encode(idx)
        outs = await asyncio.gather(*(self.generate(pt, [], temperature=temperature, seed=seed + j) for j in range(k)))
        answers = []
        for toks, ended in outs:
            own = read(self.tok.decode([t["id"] for t in toks], skip_special_tokens=True))
            text, extra = await self.read_out(pt, own)
            self.generated += extra
            answers.append({"text": text, "length": len(toks), "tokens": len(toks) + extra, "boxed": "\\boxed" in own, "ended_by": ended})
        self.rolls[idx] = [{"toks": toks, "ended_by": ended} for toks, ended in outs]
        return {"answers": answers}

    async def sample(self, q):
        from math_eval import read
        head = ""
        if q.get("draft") is not None:
            d = self.drafts[int(q["draft"])]
            keep = [t["id"] for t in d["toks"][:int(q["upto"]) + 1]]
            base = d["prompt_tokens"]
            pt, head = base + keep, self.tok.decode(keep, skip_special_tokens=True)
        elif q.get("idx") is not None:
            pt = base = self.encode(int(q["idx"]))
        else:
            pt = base = self.tok(self.template.replace("{prompt}", q["prompt"]), add_special_tokens=False)["input_ids"]
        k, temp = max(1, min(16, int(q.get("k", 1)))), float(q.get("temperature", 0.7))
        base_seed = int(q.get("seed", 0))

        async def one(j):
            sp = SamplingParams(output_kind=FINAL_ONLY, temperature=temp, max_tokens=int(q.get("max_new") or self.max_new), stop_token_ids=self.eos,
                                skip_special_tokens=True, **({"top_p": 0.95, "seed": base_seed + j} if temp > 0 else {}))
            final = None
            async for out in self.engine.generate({"prompt_token_ids": pt}, sp, request_id=uuid.uuid4().hex):
                final = out
            o = final.outputs[0]
            text, extra = await self.read_out(base, read(head + o.text))
            return {"text": text, "tokens": len(o.token_ids) + extra, "finish": o.finish_reason}
        answers = list(await asyncio.gather(*(one(j) for j in range(1 if temp <= 0 else k))))
        if temp <= 0:
            answers = answers * k
        self.generated += sum(a["tokens"] for a in answers[:1 if temp <= 0 else k])
        return {"answers": answers}

    @staticmethod
    def trim(d, pos):
        for p in d["picks"]:
            toks = p.get("tokens") or [p["token"]]
            if p["pos"] < pos < p["pos"] + len(toks):
                p["tokens"] = toks[:pos - p["pos"]]

    async def score(self, ids, start):
        sp = SamplingParams(output_kind=FINAL_ONLY, temperature=0.0, max_tokens=1, prompt_logprobs=TOP, prompt_logprob_start=start - 1,
                            skip_reading_prefix_cache=False, detokenize=False)
        final = None
        async for out in self.engine.generate({"prompt_token_ids": ids}, sp, request_id=uuid.uuid4().hex):
            final = out
        return [self.as_tok(ids[k], final.prompt_logprobs[k]) for k in range(start, len(ids))]

    async def options(self, idx, pos, tokens):
        d = self.drafts[idx]
        if not 0 <= pos <= len(d["toks"]):
            raise ValueError(f"position {pos} is outside the answer (0..{len(d['toks'])})")
        ids = d["prompt_tokens"] + [t["id"] for t in d["toks"][:pos]] + list(tokens)
        sp = SamplingParams(output_kind=FINAL_ONLY, temperature=0.0, max_tokens=1, logprobs=TOP, detokenize=False,
                            skip_reading_prefix_cache=False,
                            **({"prompt_logprobs": TOP, "prompt_logprob_start": len(ids) - len(tokens) - 1} if tokens else {}))
        final = None
        async for out in self.engine.generate({"prompt_token_ids": ids}, sp, request_id=uuid.uuid4().hex):
            final = out
        put = [self.as_tok(ids[k], final.prompt_logprobs[k]) for k in range(len(ids) - len(tokens), len(ids))] if tokens else []
        o = final.outputs[0]
        after = self.as_tok(o.token_ids[0], o.logprobs[0])["top"]
        dec = lambda t: self.tok.decode([t], skip_special_tokens=False)
        return {"put": [{"text": dec(t["id"]), "prob": t["prob"], "top": [{"text": dec(c["token"]), "prob": c["prob"]} for c in t["top"]]} for t in put],
                "after": [{"text": dec(c["token"]), "prob": c["prob"]} for c in after]}

    async def splice(self, idx, pos, end, tokens, up=1, down=0, min_prob=0.0):
        d = self.drafts[idx]
        if not 0 <= pos <= end <= len(d["toks"]):
            raise ValueError(f"the span {pos}-{end} is not inside the answer (0..{len(d['toks'])})")
        if not tokens:
            raise ValueError("nothing to put in")
        if any(p["pos"] == pos for p in d["picks"]):
            self.unpick(idx, pos)
        head = d["prompt_tokens"] + [t["id"] for t in d["toks"][:pos]]
        rest = [t["id"] for t in d["toks"][end:]]
        scored = await self.score(head + list(tokens) + rest, len(head))
        for j, t in enumerate(scored[:len(tokens)]):
            if t["prob"] < min_prob:
                allowed = ", ".join(f"{k}:{self.tok.decode([c['token']])!r} {c['prob']:.1%}" for k, c in enumerate(t["top"]) if c["prob"] >= min_prob)
                raise ValueError(f"nothing was changed: the student gives {self.tok.decode([tokens[j]])!r} probability {t['prob']:.2g} at {pos + j}"
                                 + (f" (after {self.tok.decode(list(tokens[:j]))!r})" if j else "") + f", under the floor of {min_prob:g}. "
                                 f"What the student allows there: {allowed or 'nothing above the floor'}")
        d["marks"] = {q: w for q, w in d.get("marks", {}).items() if q < pos}
        self.trim(d, pos)
        i = sum(1 for p in d["picks"] if p["pos"] < pos)
        here = d["toks"][pos] if pos < len(d["toks"]) else None
        pk = {"pos": pos, "token": tokens[0], "tokens": list(tokens), "original": here["id"] if here else tokens[0],
              "original_prob": here["prob"] if here else 0.0, "tail": d["toks"][pos:], "later_picks": d["picks"][i:],
              "ended_by": d["ended_by"], "up": up, "down": down if end > pos else 0}
        d["toks"] = d["toks"][:pos] + scored
        d["picks"] = d["picks"][:i] + [pk]

    async def pick(self, idx, pos, tokens, up=1, down=0, max_new=None, temperature=0.0, min_prob=0.0):
        d = self.drafts[idx]
        d["stopped"] = None
        if pos >= len(d["toks"]):
            raise ValueError(f"position {pos} is past the end of the answer")
        if not (isinstance(up, int) and isinstance(down, int)) or up < 0 or down < 0:
            raise ValueError("up and down are whole numbers, 0 or more")
        if not tokens:
            raise ValueError("nothing to put in")
        if min_prob and (up > 2 or down > 2):
            raise ValueError("up must be 1 or 2, down 0, 1 or 2")
        if any(p["pos"] == pos for p in d["picks"]):
            self.unpick(idx, pos)
        d["marks"] = {q: w for q, w in d.get("marks", {}).items() if q < pos}
        here = d["toks"][pos]
        head = d["prompt_tokens"] + [t["id"] for t in d["toks"][:pos]]
        if tokens == [here["id"]]:
            scored = [here]
        elif len(tokens) == 1 and not min_prob:
            prob = next((c["prob"] for c in here["top"] if c["token"] == tokens[0]), None)
            if prob is None:
                raise ValueError("pick one of the shown candidates")
            scored = [{"id": tokens[0], "prob": prob, "entropy": here["entropy"], "top": here["top"]}]
        else:
            scored = await self.score(head + list(tokens), len(head))
        j = next((k for k, t in enumerate(scored) if t["prob"] < min_prob), len(tokens))
        stop = None
        if j < len(tokens):
            t = scored[j]
            stop_tok = {"id": t["top"][0]["token"], "prob": t["top"][0]["prob"], "entropy": t["entropy"], "top": t["top"]}
            stop = {"pos": pos + j, "token": self.tok.decode([tokens[j]]), "prob": t["prob"], "floor": min_prob, "asked": len(tokens),
                    "allowed": [{"choice": k, "token": c["token"], "text": self.tok.decode([c["token"]]), "prob": c["prob"]}
                                for k, c in enumerate(t["top"]) if c["prob"] >= min_prob]}
            if not j:
                raise ValueError(f"the student gives {stop['token']!r} probability {t['prob']:.2g} at {pos}, under the floor of {min_prob:g}: "
                                 "nothing was put in. What the student allows there (fork with one of these choices, or text starting with it): "
                                 + ", ".join(f"{c['choice']}:{c['text']!r} {c['prob']:.1%}" for c in stop["allowed"]))
            tokens, scored = list(tokens[:j]), scored[:j]
        self.trim(d, pos)
        i = sum(1 for p in d["picks"] if p["pos"] < pos)
        pk = {"pos": pos, "token": tokens[0], "tokens": list(tokens), "original": here["id"], "original_prob": here["prob"],
              "tail": [], "later_picks": [], "ended_by": d["ended_by"], "up": up, "down": down}
        if list(tokens) == [here["id"]]:
            d["picks"].insert(i, pk)
            return
        pk["tail"], d["toks"] = d["toks"][pos:], d["toks"][:pos]
        pk["later_picks"], d["picks"] = d["picks"][i:], d["picks"][:i]
        d["toks"].extend(scored)
        d["picks"].append(pk)
        if stop:
            d["toks"].append(stop_tok)
            d["ended_by"], d["stopped"] = "paused", stop
            return
        if max_new is not None and int(max_new) <= 0:
            d["ended_by"] = "eos" if tokens[-1] in self.eos else "paused"
            return
        more, ended = await self.generate(d["prompt_tokens"], d["toks"], max_new, temperature)
        d["toks"].extend(more)
        d["ended_by"] = ended

    def episode(self, idx):
        d = self.drafts[idx]
        completion = [t["id"] for t in d["toks"]]
        forks = [{"pos": p["pos"], "forced": p.get("tokens") or [p["token"]], "rejected": [p["original"]],
                  "rejected_prob": p["original_prob"], "forced_prob": d["toks"][p["pos"]]["prob"],
                  "up": p.get("up", 1), "down": p.get("down", 0)} for p in d["picks"]]
        for f in forks:
            assert completion[f["pos"]:f["pos"] + len(f["forced"])] == f["forced"], "fork is not part of the final completion"
        ep = {"prompt": self.prompts[idx], "prompt_tokens": d["prompt_tokens"], "completion": completion,
              "text": self.tok.decode(completion, skip_special_tokens=False), "forks": forks,
              "outcome": "done", "ended_by": d["ended_by"], "revisions": 0, "forced_tokens": len(d["picks"])}
        marks = sorted((q, w) for q, w in d.get("marks", {}).items() if q < len(completion) and w)
        if marks:
            ep["marks"] = [{"pos": q, "token": completion[q], "w": w} for q, w in marks]
        return ep

    def piece(self, ids, i):
        lo = max(0, i - 6)
        a = self.tok.decode(ids[lo:i], skip_special_tokens=False)
        b = self.tok.decode(ids[lo:i + 1], skip_special_tokens=False)
        return b[len(a):] if b.startswith(a) else ""

    def draft_view(self, idx):
        d = self.drafts.get(idx)
        if d is None:
            return None
        p = self.prompts[idx]
        ids = [t["id"] for t in d["toks"]]
        dec = lambda t: self.tok.decode([t], skip_special_tokens=False)
        tokens = [{"id": t["id"], "text": self.piece(ids, i), "prob": t["prob"], "entropy": t["entropy"],
                   "top": [{"token": c["token"], "prob": c["prob"], "text": dec(c["token"])} for c in t["top"]]}
                  for i, t in enumerate(d["toks"])]
        picks = [{"pos": k["pos"], "token": k["token"], "n": len(k.get("tokens") or [k["token"]]), "original": k["original"],
                  "original_text": dec(k["original"]), "up": k.get("up", 1), "down": k.get("down", 0)}
                 for k in d["picks"]]
        return {"idx": idx, "id": p["id"], "prompt": p["prompt"], "extra": p.get("extra"), "tokens": tokens,
                "picks": picks, "marks": {str(q): w for q, w in sorted(d.get("marks", {}).items())}, "stopped": d.get("stopped"),
                "down": sorted(q for q, w in d.get("marks", {}).items() if w < 0), "ended_by": d["ended_by"], "gen": 0}

    def view(self, idx, full):
        v = {"pending_picks": sum(len(d["picks"]) for d in self.drafts.values()), "round": 0, "last_stats": None,
             "version": self.version, "notes": self.notes, "draft": self.draft_view(idx) if idx is not None else None}
        if full:
            v["problems"] = [{"idx": i, "id": p["id"], "picks": len(self.drafts[i]["picks"]) if i in self.drafts else 0}
                             for i, p in enumerate(self.prompts)]
        return v

    async def handle(self, req):
        path = req.path
        if path == "/":
            return web.Response(status=404, text="not found")
        if path in ("/api/progress", "/api/stop"):
            return web.json_response({"generating": False, "version": self.version})
        try:
            q = await req.json() if req.can_read_body else {}
        except Exception:
            q = {}
        idx = q.get("idx")
        gen0, more = self.generated, {}
        try:
            if path == "/api/state":
                return web.json_response(self.view(self.current, True))
            if path == "/api/add":
                known = next((i for i, x in enumerate(self.prompts) if x["id"] == q["id"]), None)
                if known is None:
                    self.prompts.append({"id": q["id"], "prompt": q["prompt"], **({"extra": q["extra"]} if q.get("extra") else {})})
                return web.json_response({"idx": len(self.prompts) - 1 if known is None else known, "n": len(self.prompts)})
            if path == "/api/sample":
                out = await self.sample(q)
                return web.json_response({**out, "generated": sum(a["tokens"] for a in out["answers"])})
            if path == "/api/options":
                if q.get("idx") is None or int(q["idx"]) not in self.drafts:
                    raise ValueError(f"task {q.get('idx')} has no answer (open it first)")
                toks = self.tok(str(q.get("text", "")), add_special_tokens=False)["input_ids"] if q.get("text") else []
                return web.json_response(await self.options(int(q["idx"]), int(q["pos"]), toks))
            if path == "/api/note":
                self.notes.append({"text": q["text"], "round": 0})
                self.notes = self.notes[-200:]
            elif path in ("/api/open", "/api/regenerate", "/api/rollouts", "/api/pick", "/api/splice", "/api/continue", "/api/unpick", "/api/down", "/api/weight", "/api/commit", "/api/drop"):
                if idx is None:
                    if path in ("/api/open", "/api/rollouts", "/api/commit", "/api/drop") or self.current is None:
                        raise ValueError("idx")
                    idx = self.current
                lock = self.locks.setdefault(idx, asyncio.Lock())
                async with lock:
                    if path == "/api/open":
                        await self.open(idx, False, q.get("rollout"))
                    elif path == "/api/regenerate":
                        await self.open(idx, True)
                    elif path == "/api/rollouts":
                        more = await self.rollouts(idx, max(1, min(16, int(q.get("k", 4)))), float(q.get("temperature", 0.7)), int(q.get("seed", 0)))
                    elif idx not in self.drafts:
                        if path != "/api/drop":
                            raise ValueError(f"problem {idx} has no answer (open it first)")
                    elif path == "/api/pick":
                        toks = [int(q["token"])] if q.get("token") is not None else \
                            self.tok(str(q.get("text", "")), add_special_tokens=False)["input_ids"]
                        await self.pick(idx, q["pos"], toks, int(q.get("up", 1)), int(q.get("down", 0)), q.get("max_new"),
                                        q.get("temperature", 0.0), float(q.get("min_prob", 0.0)))
                    elif path == "/api/splice":
                        toks = self.tok(str(q.get("text", "")), add_special_tokens=False)["input_ids"]
                        await self.splice(idx, int(q["pos"]), int(q.get("end", q["pos"])), toks, int(q.get("up", 1)), int(q.get("down", 0)),
                                          float(q.get("min_prob", 0.0)))
                    elif path == "/api/continue":
                        await self.more(idx, q.get("max_new"), q.get("temperature", 0.0))
                    elif path == "/api/unpick":
                        self.unpick(idx, q["pos"])
                    elif path in ("/api/down", "/api/weight"):
                        d = self.drafts[idx]
                        if not 0 <= q["pos"] < len(d["toks"]):
                            raise ValueError(f"position {q['pos']} is outside the answer")
                        marks = d.setdefault("marks", {})
                        w = int(q["w"]) if path == "/api/weight" else (0 if marks.get(q["pos"], 0) < 0 else -1)
                        if any(p["pos"] == q["pos"] for p in d["picks"]):
                            raise ValueError("that position is a swap; set its weights on the edit itself (up, down)")
                        if w:
                            marks[q["pos"]] = w
                        else:
                            marks.pop(q["pos"], None)
                    elif path == "/api/commit":
                        if not self.drafts[idx]["picks"] and not self.drafts[idx].get("marks"):
                            raise ValueError("no picks or marks on that problem")
                        if self.drafts[idx]["ended_by"] == "paused":
                            raise ValueError("the answer is unfinished; continue it to the end before keeping it")
                        ep = self.episode(idx)
                        async with self.commit_lock:
                            path_c = os.path.join(self.run_dir, "collected.jsonl")
                            n = sum(1 for _ in open(path_c)) if os.path.exists(path_c) else 0
                            with open(path_c, "a") as f:
                                rec = {"round": self.round, "index": n, "episode": ep}
                                if q.get("meta") is not None:
                                    rec["meta"] = q["meta"]
                                fcntl.flock(f, fcntl.LOCK_EX)
                                f.write(json.dumps(rec) + "\n")
                                f.flush()
                    elif path == "/api/drop":
                        self.drafts.pop(idx, None)
                if path == "/api/drop":
                    self.locks.pop(idx, None)
                    if self.current == idx:
                        self.current = None
                    idx = None
                else:
                    self.current = idx
            elif path == "/api/apply":
                raise ValueError("collection-only server: train offline (tools/train_hf.py)")
            else:
                raise ValueError(f"unknown endpoint {path}")
            self.version += 1
            return web.json_response({**self.view(idx, False), **more, "generated": self.generated - gen0})
        except (ValueError, KeyError) as e:
            return web.json_response({"error": str(e)}, status=400)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--gpu-mem", type=float, default=0.25)
    ap.add_argument("--max-num-seqs", type=int, default=0, help="answers written at once at most (0: vLLM's own default)")
    ap.add_argument("--round", type=int, default=0, help="written on every episode: which student version this server holds")
    ap.add_argument("--dtype", default="float32", help="the student's precision (bfloat16: ordinary 16-bit inference)")
    ap.add_argument("--max-model-len", type=int, default=0,
                    help="engine context (default max_new_tokens + 2048)")
    a = ap.parse_args()
    s = Steer(a)
    app = web.Application(client_max_size=1 << 26)
    app.router.add_route("*", "/{tail:.*}", s.handle)
    import socket, time
    for _ in range(150):
        with socket.socket() as sk:
            sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sk.bind((a.host, a.port))
                break
            except OSError:
                print(f"student_server: port {a.port} is in use; waiting", flush=True)
                time.sleep(2)
    print(f"student_server: http://{a.host}:{a.port}  ({len(s.prompts)} problems)", flush=True)
    web.run_app(app, host=a.host, port=a.port, print=None)


if __name__ == "__main__":
    main()
