import argparse, asyncio, json, math, os, tomllib, uuid

from aiohttp import web
from transformers import AutoTokenizer
from vllm import SamplingParams
from vllm.engine.arg_utils import AsyncEngineArgs
from vllm.v1.engine.async_llm import AsyncLLM

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
        self.max_new = ep.get("sampling", {}).get("max_new_tokens", 4096)
        self.tok = AutoTokenizer.from_pretrained(self.model)
        gen = json.load(open(os.path.join(self.model, "generation_config.json")))
        eos = gen.get("eos_token_id", [])
        self.eos = ep.get("eos") or (eos if isinstance(eos, list) else [eos])
        self.prompts = load_prompts(cfg["prompts"])
        os.makedirs(self.run_dir, exist_ok=True)
        self.engine = AsyncLLM.from_engine_args(AsyncEngineArgs(
            model=self.model, dtype="bfloat16", max_model_len=self.max_new + 2048, seed=0,
            gpu_memory_utilization=a.gpu_mem, enable_prefix_caching=True, max_logprobs=TOP))
        self.drafts, self.locks = {}, {}
        self.current, self.version, self.notes = None, 0, []
        self.commit_lock = asyncio.Lock()

    async def generate(self, prompt_tokens, prefix):
        if prefix and prefix[-1]["id"] in self.eos:
            return [], "eos"
        budget = self.max_new - len(prefix)
        if budget <= 0:
            return [], "max_tokens"
        sp = SamplingParams(temperature=0.0, max_tokens=budget, logprobs=TOP, stop_token_ids=self.eos,
                            detokenize=False, skip_special_tokens=False)
        ids = prompt_tokens + [t["id"] for t in prefix]
        final = None
        async for out in self.engine.generate({"prompt_token_ids": ids}, sp, request_id=uuid.uuid4().hex):
            final = out
        o = final.outputs[0]
        toks = []
        for tid, lps in zip(o.token_ids, o.logprobs):
            cands = sorted(((t, math.exp(lp.logprob)) for t, lp in lps.items()), key=lambda c: -c[1])
            top = [{"token": t, "prob": p} for t, p in cands[:TOP]]
            probs = [p for _, p in cands[:TOP]]
            rest = max(0.0, 1.0 - sum(probs))
            ent = -sum(p * math.log(p) for p in probs if p > 0) - (rest * math.log(rest) if rest > 0 else 0.0)
            prob = math.exp(lps[tid].logprob) if tid in lps else 0.0
            toks.append({"id": tid, "prob": prob, "entropy": ent, "top": top})
        if o.finish_reason == "length":
            return toks, "max_tokens"
        if toks and toks[-1]["id"] not in self.eos and isinstance(o.stop_reason, int):
            toks.append({"id": o.stop_reason, "prob": 1.0, "entropy": 0.0, "top": [{"token": o.stop_reason, "prob": 1.0}]})
        return toks, "eos"

    def encode(self, idx):
        text = self.template.replace("{prompt}", self.prompts[idx]["prompt"])
        return self.tok(text, add_special_tokens=False)["input_ids"]

    async def open(self, idx, fresh):
        if fresh or idx not in self.drafts:
            pt = self.encode(idx)
            toks, ended = await self.generate(pt, [])
            self.drafts[idx] = {"prompt_tokens": pt, "toks": toks, "picks": [], "ended_by": ended}

    def unpick(self, idx, pos):
        d = self.drafts[idx]
        i = next((k for k, p in enumerate(d["picks"]) if p["pos"] == pos), None)
        if i is None:
            raise ValueError("no pick at that position")
        pk = d["picks"][i]
        if pk["token"] == pk["original"]:
            d["picks"].pop(i)
            return
        d["toks"] = d["toks"][:pos] + pk["tail"]
        d["picks"] = d["picks"][:i] + pk["later_picks"]
        d["ended_by"] = pk["ended_by"]

    async def pick(self, idx, pos, token):
        d = self.drafts[idx]
        if pos >= len(d["toks"]):
            raise ValueError(f"position {pos} is past the end of the answer")
        if any(p["pos"] == pos for p in d["picks"]):
            self.unpick(idx, pos)
        here = d["toks"][pos]
        prob = next((c["prob"] for c in here["top"] if c["token"] == token), None)
        if prob is None:
            raise ValueError("pick one of the shown candidates")
        i = sum(1 for p in d["picks"] if p["pos"] < pos)
        pk = {"pos": pos, "token": token, "original": here["id"], "original_prob": here["prob"],
              "tail": [], "later_picks": [], "ended_by": d["ended_by"]}
        if token == here["id"]:
            d["picks"].insert(i, pk)
            return
        pk["tail"], d["toks"] = d["toks"][pos:], d["toks"][:pos]
        pk["later_picks"], d["picks"] = d["picks"][i:], d["picks"][:i]
        d["toks"].append({"id": token, "prob": prob, "entropy": here["entropy"], "top": here["top"]})
        d["picks"].append(pk)
        more, ended = await self.generate(d["prompt_tokens"], d["toks"])
        d["toks"].extend(more)
        d["ended_by"] = ended

    def episode(self, idx):
        d = self.drafts[idx]
        completion = [t["id"] for t in d["toks"]]
        forks = [{"pos": p["pos"], "forced": [p["token"]], "rejected": [p["original"]],
                  "rejected_prob": p["original_prob"], "forced_prob": d["toks"][p["pos"]]["prob"]} for p in d["picks"]]
        for f in forks:
            assert completion[f["pos"]] == f["forced"][0], "fork is not part of the final completion"
        return {"prompt": self.prompts[idx], "prompt_tokens": d["prompt_tokens"], "completion": completion,
                "text": self.tok.decode(completion, skip_special_tokens=False), "forks": forks,
                "outcome": "done", "ended_by": d["ended_by"], "revisions": 0, "forced_tokens": len(d["picks"])}

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
        picks = [{"pos": k["pos"], "token": k["token"], "original": k["original"], "original_text": dec(k["original"])}
                 for k in d["picks"]]
        return {"idx": idx, "id": p["id"], "prompt": p["prompt"], "extra": p.get("extra"), "tokens": tokens,
                "picks": picks, "ended_by": d["ended_by"], "gen": 0}

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
            page = os.environ.get("MANUAL_RL_PAGE", os.path.join(os.path.dirname(__file__), "../src/ui.html"))
            return web.Response(text=open(page).read(), content_type="text/html")
        if path in ("/api/progress", "/api/stop"):
            return web.json_response({"generating": False, "version": self.version})
        try:
            q = await req.json() if req.can_read_body else {}
        except Exception:
            q = {}
        idx = q.get("idx")
        try:
            if path == "/api/state":
                return web.json_response(self.view(self.current, True))
            if path == "/api/note":
                self.notes.append({"text": q["text"], "round": 0})
                self.notes = self.notes[-200:]
            elif path in ("/api/open", "/api/regenerate", "/api/pick", "/api/unpick", "/api/commit", "/api/drop"):
                if idx is None:
                    if path in ("/api/open", "/api/commit", "/api/drop") or self.current is None:
                        raise ValueError("idx")
                    idx = self.current
                lock = self.locks.setdefault(idx, asyncio.Lock())
                async with lock:
                    if path == "/api/open":
                        await self.open(idx, False)
                    elif path == "/api/regenerate":
                        await self.open(idx, True)
                    elif idx not in self.drafts:
                        if path != "/api/drop":
                            raise ValueError(f"problem {idx} has no answer (open it first)")
                    elif path == "/api/pick":
                        await self.pick(idx, q["pos"], q["token"])
                    elif path == "/api/unpick":
                        self.unpick(idx, q["pos"])
                    elif path == "/api/commit":
                        if not self.drafts[idx]["picks"]:
                            raise ValueError("no picks on that problem")
                        ep = self.episode(idx)
                        async with self.commit_lock:
                            path_c = os.path.join(self.run_dir, "collected.jsonl")
                            n = sum(1 for _ in open(path_c)) if os.path.exists(path_c) else 0
                            with open(path_c, "a") as f:
                                f.write(json.dumps({"round": 0, "index": n, "episode": ep}) + "\n")
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
                raise ValueError("collection-only server: train offline (batch_train.py / manual-rl train)")
            else:
                raise ValueError(f"unknown endpoint {path}")
            self.version += 1
            return web.json_response(self.view(idx, False))
        except (ValueError, KeyError) as e:
            return web.json_response({"error": str(e)}, status=400)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--gpu-mem", type=float, default=0.25)
    a = ap.parse_args()
    s = Steer(a)
    app = web.Application(client_max_size=1 << 26)
    app.router.add_route("*", "/{tail:.*}", s.handle)
    print(f"steer_vllm: http://{a.host}:{a.port}  ({len(s.prompts)} problems)", flush=True)
    web.run_app(app, host=a.host, port=a.port, print=None)


if __name__ == "__main__":
    main()
