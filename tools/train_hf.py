import argparse, json, math, os, shutil, sys, time

DEFAULTS = dict(lr=1e-6, stages=[5], weight_decay=0.0, optimizer="adamw", betas=(0.9, 0.999), eps=1e-8,
                momentum=0.9, max_grad_norm=1.0, minibatch=0, forced_weight=1.0, down_weight=0.0, forward="f32",
                keep_weight=0.0, keep="rest", freeze="", pull=0.0, until_taken=False, taken_p=0.5,
                grad_checkpoint=True, seed=0, device="cuda", match_sign=0.0, measure=False, devices=1, plain=0.0, fsdp=False)


def load_episodes(path):
    out = []
    for line in open(path):
        try:
            e = json.loads(line)["episode"]
        except (json.JSONDecodeError, KeyError):
            continue
        if e.get("outcome") != "done":
            continue
        pl, ids = len(e["prompt_tokens"]), e["prompt_tokens"] + e["completion"]
        up, dn = {}, {}
        for f in e["forks"]:
            if f.get("up", 1) > 0:
                for j in range(len(f["forced"])):
                    up[pl + f["pos"] + j] = float(f.get("up", 1))
            if f["rejected"][0] != f["forced"][0] and f.get("down", 1) > 0:
                dn[(pl + f["pos"], f["rejected"][0])] = float(f.get("down", 1))
        for m in e.get("down", []):
            dn[(pl + m["pos"], m["token"])] = 1.0
        for m in e.get("marks", []):
            if m["w"] > 0:
                up[pl + m["pos"]] = float(m["w"])
            elif m["w"] < 0:
                dn[(pl + m["pos"], m["token"])] = float(-m["w"])
        forced, down = sorted(up), sorted(dn)
        out.append({"id": e["prompt"]["id"], "ids": ids, "pl": pl, "forced": forced, "down": down,
                    "forced_w": [up[q] for q in forced], "down_w": [dn[q] for q in down]})
    return out


FORWARD = "f32"
SPARSE = False


class Dist:
    def __init__(self):
        self.world, self.rank, self.local = (int(os.environ.get(k, d)) for k, d in (("WORLD_SIZE", 1), ("RANK", 0), ("LOCAL_RANK", 0)))

    def start(self):
        import torch, torch.distributed as dist
        if self.world > 1 and not dist.is_initialized():
            torch.cuda.set_device(self.local)
            dist.init_process_group("nccl")

    def sum(self, *xs):
        if self.world == 1:
            return list(xs)
        import torch, torch.distributed as dist
        t = torch.tensor(xs, dtype=torch.float64, device=f"cuda:{self.local}")
        dist.all_reduce(t)
        return t.tolist()

    def barrier(self):
        if self.world > 1:
            import torch.distributed as dist
            dist.barrier()

    def share(self, items):
        items = sorted(items, key=lambda e: -len(e["ids"]))
        mine = items[self.rank::self.world]
        return mine + [None] * (-(-len(items) // self.world) - len(mine))


def stand_in(ep):
    ids = ep["ids"][:16]
    return {"ids": ids, "pl": 0, "forced": [len(ids) - 1], "forced_w": [0.0], "down": [], "down_w": [], "keep": []}


def touch_experts(model):
    import torch

    def hook(mod, args, out):
        zero = sum(p.sum() for p in mod.experts.parameters() if p.requires_grad) * 0.0
        return (out[0] + zero, *out[1:]) if isinstance(out, tuple) else out + zero
    for m in model.modules():
        if isinstance(getattr(m, "experts", None), torch.nn.Module):
            m.register_forward_hook(hook)


def split_experts():
    import torch
    import transformers.integrations.moe as moe
    gm = torch.nn.functional.grouped_mm
    bf, f32 = torch.bfloat16, torch.float32

    class Split(torch.autograd.Function):
        @staticmethod
        def forward(ctx, x, w, offs):
            xb, w1 = x.to(bf), w.to(bf)
            r = w - w1.to(f32)
            w2 = r.to(bf)
            w3 = (r - w2.to(f32)).to(bf)
            y = gm(xb, w1, offs=offs).to(f32) + gm(xb, w2, offs=offs).to(f32) + gm(xb, w3, offs=offs).to(f32)
            ctx.save_for_backward(xb, w1, offs)
            ctx.x_dtype, ctx.w_stride = x.dtype, w.stride()
            return y

        @staticmethod
        def backward(ctx, gy):
            xb, w1, offs = ctx.saved_tensors
            gb = gy.to(bf)
            gx = gm(gb, w1.transpose(-2, -1), offs=offs).to(ctx.x_dtype) if ctx.needs_input_grad[0] else None
            gw = None
            if ctx.needs_input_grad[1]:
                g = gm(xb.transpose(0, 1), gb, offs=offs)
                gw = torch.empty_strided(g.shape, ctx.w_stride, dtype=f32, device=g.device).copy_(g)
            return gx, gw, None

    plain = moe._grouped_mm

    def grouped(input, weight, offs):
        if weight.dtype == f32 and weight.is_cuda:
            return Split.apply(input, weight, offs)
        return plain(input, weight, offs)
    moe._grouped_mm = grouped


def sparse_logps(model, ep, grad, keep=False):
    import torch
    dev = next(model.parameters()).device
    ids = torch.tensor([ep["ids"]], device=dev)
    down = ep.get("down") or []
    need = sorted({q - 1 for q in list(ep["forced"]) + list(ep.get("keep") or []) + [q for q, _ in down]}) or [len(ep["ids"]) - 1]
    row = {q: i for i, q in enumerate(need)}
    with torch.set_grad_enabled(grad), torch.autocast(dev.type if dev.type in ("cuda", "cpu") else "cpu", dtype=torch.bfloat16, enabled=FORWARD == "bf16"):
        logits = model(input_ids=ids, use_cache=False, logits_to_keep=torch.tensor(need, device=dev)).logits[0]

        def at(pos):
            if not pos:
                return logits[:0, 0].float()
            return torch.log_softmax(logits[torch.tensor([row[q - 1] for q in pos], device=dev)].float(), -1).gather(
                1, ids[0, torch.tensor(pos, device=dev), None])[:, 0]
        if down:
            t = torch.tensor([[tok] for _, tok in down], device=dev)
            dn = torch.log_softmax(logits[torch.tensor([row[q - 1] for q, _ in down], device=dev)].float(), -1).gather(1, t)[:, 0]
        else:
            dn = logits[:0, 0].float()
        if keep:
            return at(ep["forced"]), dn, at(ep.get("keep") or [])
        return at(ep["forced"]), dn


END_TOKENS = (151645, 151643)


def keep_positions(ep, which):
    if ep.get("pl") is None:
        return []
    pushed = {q for q, tok in (ep.get("down") or []) if q < len(ep["ids"]) and ep["ids"][q] == tok}
    if which == "end":
        last = len(ep["ids"]) - 1
        return [last] if ep["ids"][-1] in END_TOKENS and last not in pushed else []
    forced = set(ep["forced"])
    return [q for q in range(ep["pl"], len(ep["ids"])) if q not in forced and q not in pushed]


def target_logps(model, ep, grad, keep=False):
    if SPARSE:
        return sparse_logps(model, ep, grad, keep)
    import torch
    dev = next(model.parameters()).device
    ids = torch.tensor([ep["ids"]], device=dev)
    with torch.set_grad_enabled(grad), torch.autocast(dev.type if dev.type in ("cuda", "cpu") else "cpu", dtype=torch.bfloat16, enabled=FORWARD == "bf16"):
        logits = model(input_ids=ids, use_cache=False).logits[0]
        dev = logits.device
        ids = ids.to(dev)

        def at(pos):
            if not pos:
                return logits.new_zeros(0, dtype=torch.float32)
            p = torch.tensor(pos, device=dev)
            return torch.log_softmax(logits[p - 1].float(), -1).gather(1, ids[0, p, None])[:, 0]
        down = ep.get("down") or []
        if down:
            p = torch.tensor([q for q, _ in down], device=dev)
            t = torch.tensor([[tok] for _, tok in down], device=dev)
            dn = torch.log_softmax(logits[p - 1].float(), -1).gather(1, t)[:, 0]
        else:
            dn = logits.new_zeros(0, dtype=torch.float32)
        if keep:
            return at(ep["forced"]), dn, at(ep.get("keep") or [])
        return at(ep["forced"]), dn


def episode_loss(c, e, a, dn, kp, nf, nd, nk):
    import torch
    loss = 0.0
    if c["keep_weight"] > 0 and nk and len(kp):
        loss = loss - c["keep_weight"] * kp.sum() / nk
    if c["forced_weight"] > 0 and nf and len(a):
        loss = loss - c["forced_weight"] * (a * a.new_tensor(e["forced_w"])).sum() / nf
    if c["down_weight"] > 0 and nd and len(dn):
        loss = loss - c["down_weight"] * (torch.log1p(-dn.exp().clamp(max=1 - 1e-6)) * dn.new_tensor(e["down_w"])).sum() / nd
    return loss


def measure(model, eps, D=None):
    f = d = 0.0
    if D is not None:
        for e in D.share(eps):
            a, c = target_logps(model, e or stand_in(eps[0]), False)
            if e is not None:
                f, d = f + float(a.sum()), d + float(c.sum())
        f, d = D.sum(f, d)
    else:
        for e in eps:
            a, c = target_logps(model, e, False)
            f, d = f + float(a.sum()), d + float(c.sum())
    nf, nd = (sum(len(e[k]) for e in eps) for k in ("forced", "down"))
    return (f / nf if nf else 0.0), (d / nd if nd else 0.0)


def save(model, base_dir, ck):
    os.makedirs(ck + ".tmp", exist_ok=True)
    model.save_pretrained(ck + ".tmp", safe_serialization=True, max_shard_size="1000GB")
    for f in os.listdir(base_dir):
        if (f.endswith((".json", ".txt", ".model")) and not f.endswith(".safetensors.index.json")
                and not os.path.exists(os.path.join(ck + ".tmp", f))):
            shutil.copy(os.path.join(base_dir, f), ck + ".tmp")
    os.replace(ck + ".tmp", ck)


def save_sharded(model, base_dir, ck, D):
    from torch.distributed.checkpoint.state_dict import StateDictOptions, get_model_state_dict
    sd = get_model_state_dict(model, options=StateDictOptions(full_state_dict=True, cpu_offload=True))
    if D.rank == 0:
        os.makedirs(ck + ".tmp", exist_ok=True)
        model.save_pretrained(ck + ".tmp", state_dict=sd, safe_serialization=True, max_shard_size="10GB")
        cfg_p = os.path.join(ck + ".tmp", "config.json")
        cfg = json.load(open(cfg_p))
        cfg["architectures"] = json.load(open(os.path.join(base_dir, "config.json"))).get("architectures", cfg.get("architectures"))
        json.dump(cfg, open(cfg_p, "w"), indent=2)
        for f in os.listdir(base_dir):
            if (f.endswith((".json", ".txt", ".model")) and not f.endswith(".safetensors.index.json")
                    and not os.path.exists(os.path.join(ck + ".tmp", f))):
                shutil.copy(os.path.join(base_dir, f), ck + ".tmp")
        os.replace(ck + ".tmp", ck)
    del sd
    D.barrier()


def train(model_dir, episodes, out, model=None, log=sys.stderr, **cfg):
    import torch
    from transformers import AutoModelForCausalLM
    global FORWARD, SPARSE
    c = {**DEFAULTS, **cfg}
    unknown = set(cfg) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"unknown settings {sorted(unknown)}")
    if c["forward"] not in ("f32", "bf16"):
        raise ValueError("forward must be f32 or bf16")
    FORWARD = c["forward"]
    D = None
    if c["fsdp"]:
        if c["match_sign"] > 0 or c["plain"] > 0 or c["measure"] or c["devices"] > 1:
            raise ValueError("fsdp trains with the plain optimizer step; match_sign, plain, measure and devices are not available with it")
        D = Dist()
        D.start()
        log = log if D.rank == 0 else open(os.devnull, "w")
        SPARSE = True
        if FORWARD == "bf16":
            split_experts()
    torch.manual_seed(c["seed"])
    eps = load_episodes(episodes) if isinstance(episodes, str) else episodes
    use_f, use_d = c["forced_weight"] > 0, c["down_weight"] > 0
    eps = [dict(e, down=e.get("down") or [], forced_w=e.get("forced_w") or [1.0] * len(e["forced"]),
                down_w=e.get("down_w") or [1.0] * len(e.get("down") or [])) for e in eps]
    use_k = c["keep_weight"] > 0
    eps = [e for e in eps if (use_f and e["forced"]) or (use_d and e["down"]) or use_k]
    if c["keep"] not in ("rest", "end"):
        raise ValueError("keep must be rest or end")
    eps = [dict(e, keep=keep_positions(e, c["keep"]) if use_k else []) for e in eps]
    if not eps:
        raise ValueError("no episode has a target under these loss weights")
    if model is None:
        if c["fsdp"]:
            model = AutoModelForCausalLM.from_pretrained(model_dir, torch_dtype=torch.float32, attn_implementation="sdpa",
                                                         device_map={"": f"cuda:{D.local}"})
        elif c["devices"] > 1:
            model = AutoModelForCausalLM.from_pretrained(model_dir, torch_dtype=torch.float32, attn_implementation="sdpa",
                                                         device_map="balanced", max_memory={i: "1000GiB" for i in range(c["devices"])})
        else:
            model = AutoModelForCausalLM.from_pretrained(model_dir, torch_dtype=torch.float32, attn_implementation="sdpa").to(c["device"])
    if c["grad_checkpoint"]:
        if c["fsdp"]:
            model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
        else:
            model.gradient_checkpointing_enable()
    model.train()
    frozen = [s for s in c["freeze"].split(",") if s] if isinstance(c["freeze"], str) else list(c["freeze"])
    for n, p in model.named_parameters():
        p.requires_grad_(not any(s in n for s in frozen))
    if c["fsdp"]:
        from torch.distributed.fsdp import fully_shard
        touch_experts(model)
        for layer in model.model.layers:
            fully_shard(layer)
        fully_shard(model)
    params = [p for p in model.parameters() if p.requires_grad]
    if c["optimizer"] == "adamw":
        opt = torch.optim.AdamW(params, lr=c["lr"], betas=tuple(c["betas"]), eps=c["eps"], weight_decay=c["weight_decay"])
    elif c["optimizer"] == "sgdm":
        opt = torch.optim.SGD(params, lr=c["lr"], momentum=c["momentum"], weight_decay=c["weight_decay"])
    else:
        raise ValueError(f"unknown optimizer {c['optimizer']!r} (adamw or sgdm)")
    start = [p.detach().to("cpu", copy=True) for p in params] if c["pull"] > 0 or c["measure"] else None
    if c["match_sign"] > 0 and c["optimizer"] != "sgdm":
        raise ValueError("match_sign is a step rule for sgdm")
    if c["plain"] > 0 and c["match_sign"] <= 0:
        raise ValueError("plain needs match_sign (it sets the first step)")
    plain_lr = None
    n_params, l1s, lens = sum(p.numel() for p in params), [], []
    stages = sorted(set(int(s) for s in c["stages"]))
    mbs = c["minibatch"] or len(eps)
    one_step = mbs >= len(eps)
    nf_all, nd_all, nk_all = (sum(len(e[k]) for e in eps) for k in ("forced", "down", "keep"))
    base = {"episodes": len(eps), "forced_tokens": nf_all, "down_tokens": nd_all, "keep_tokens": nk_all,
            "settings": {k: c[k] for k in DEFAULTS if k != "device"}}
    if not one_step:
        base["forced_logp_before"], base["down_logp_before"] = measure(model, eps, D)
    if c["until_taken"] and (len(stages) != 1 or not one_step or not nf_all):
        raise ValueError("until_taken needs one stage (the most steps allowed), the whole batch per step, and forced tokens")
    results, norms, steps, taken_at = {}, [], 0, None
    for epoch in range(1, stages[-1] + 1):
        t0, fs, ds, ks, taken = time.time(), 0.0, 0.0, 0.0, 0
        for k in range(0, len(eps), mbs):
            mb = eps[k:k + mbs]
            nk = sum(len(e["keep"]) for e in mb)
            nf, nd = sum(sum(e["forced_w"]) for e in mb), sum(sum(e["down_w"]) for e in mb)
            opt.zero_grad(set_to_none=True)
            if D is not None:
                lf = ld = lk = lt = 0.0
                for e in D.share(mb):
                    real = e is not None
                    e = e or stand_in(mb[0])
                    a, dn, kp = target_logps(model, e, True, keep=True)
                    loss = episode_loss(c, e, a, dn, kp, nf, nd, nk)
                    loss = (loss if torch.is_tensor(loss) else 0.0) + a.sum() * 0.0
                    (loss * D.world).backward()
                    if real:
                        lf, ld = lf + float(a.detach().sum()), ld + float(dn.detach().sum())
                        lt += int((a.detach() > math.log(c["taken_p"])).sum())
                        if use_k and nk and len(kp):
                            lk += float(kp.detach().sum())
                lf, ld, lk, lt = D.sum(lf, ld, lk, lt)
                fs, ds, ks, taken = fs + lf, ds + ld, ks + lk, taken + int(round(lt))
            for e in (mb if D is None else []):
                a, dn, kp = target_logps(model, e, True, keep=True)
                loss = episode_loss(c, e, a, dn, kp, nf, nd, nk)
                if use_k and nk and len(kp):
                    ks += float(kp.detach().sum())
                if torch.is_tensor(loss):
                    loss.backward()
                fs, ds = fs + float(a.detach().sum()), ds + float(dn.detach().sum())
                taken += int((a.detach() > math.log(c["taken_p"])).sum())
            if c["until_taken"] and taken == nf_all and steps >= 1:
                taken_at = steps
                break
            if c["measure"] or c["match_sign"] > 0:
                l1s.append(sum(float(p.grad.abs().sum(dtype=torch.float64)) for p in params if p.grad is not None))
            norm = float(torch.nn.utils.clip_grad_norm_(params, c["max_grad_norm"] if c["max_grad_norm"] > 0 else float("inf")))
            norms.append(norm)
            if c["match_sign"] > 0:
                now = min(norm, c["max_grad_norm"]) if c["max_grad_norm"] > 0 else norm
                for p in params:
                    if p.grad is not None:
                        p.grad.mul_(1.0 / max(now, 1e-30))
                lens.append(c["match_sign"] * l1s[-1] / max(norm, 1e-30))
                if c["plain"] > 0:
                    if plain_lr is None:
                        plain_lr = c["plain"] * lens[-1] / max(norm, 1e-30)
                    lens[-1] = plain_lr * norm
                for g in opt.param_groups:
                    g["lr"] = lens[-1] * (1.0 - c["momentum"])
            opt.step()
            if start is not None and c["pull"] > 0:
                with torch.no_grad():
                    for p, s0 in zip(params, start):
                        p.add_(s0.to(p.device) - p, alpha=c["pull"])
            steps += 1
        if epoch == 1 and one_step:
            base["forced_logp_before"] = fs / nf_all if nf_all else 0.0
            base["down_logp_before"] = ds / nd_all if nd_all else 0.0
            base["keep_logp_before"] = ks / nk_all if nk_all else 0.0
        print(f"epoch {epoch}: forced logp {fs / nf_all if nf_all else 0.0:.4f}, "
              f"pushed-down logp {ds / nd_all if nd_all else 0.0:.4f}, kept logp {ks / nk_all if nk_all else 0.0:.4f} "
              f"(at the weights before each step), {taken}/{nf_all} forced tokens taken, grad norm {norms[-1] if norms else 0:.3g}, "
              f"{time.time() - t0:.0f}s", file=log, flush=True)
        if taken_at is not None:
            epoch = stages[-1]
        if epoch in stages:
            opt.zero_grad(set_to_none=True)
            fa, da = measure(model, eps, D)
            st = {**base, "epochs": epoch, "steps": steps, "lr": c["lr"], "forced_logp_after": fa,
                  "down_logp_after": da, "keep_logp_last_step": ks / nk_all if nk_all else 0.0,
                  "taken_after_steps": taken_at, "grad_norm": sum(norms) / len(norms) if norms else 0.0}
            if c["measure"]:
                d2 = d1 = dmax = 0.0
                with torch.no_grad():
                    for p, s0 in zip(params, start):
                        d = p.detach() - s0.to(p.device)
                        d2, d1, dmax = d2 + float(d.pow(2).sum(dtype=torch.float64)), d1 + float(d.abs().sum(dtype=torch.float64)), max(dmax, float(d.abs().max()))
                st.update(n_params=n_params, moved_l2=math.sqrt(d2), moved_mean_abs=d1 / n_params, moved_max=dmax,
                          grad_l2=list(norms), grad_l1=list(l1s), step_l2=list(lens))
            if out and D is not None:
                ck = os.path.join(out, f"ckpt-{epoch:05d}")
                save_sharded(model, model_dir, ck, D)
                if D.rank == 0:
                    json.dump(st, open(os.path.join(ck, "stats.json"), "w"), indent=1)
            elif out:
                ck = os.path.join(out, f"ckpt-{epoch:05d}")
                save(model, model_dir, ck)
                json.dump(st, open(os.path.join(ck, "stats.json"), "w"), indent=1)
            results[epoch] = st
        if taken_at is not None:
            break
    return results


KINDS = (("embed", ("embed_tokens",)), ("unembed", ("lm_head",)), ("attention", ("q_proj", "k_proj", "v_proj", "o_proj")),
         ("mlp", ("gate_proj", "up_proj", "down_proj")), ("norm", ("norm",)))


def geometry(a, cfg):
    import torch
    from transformers import AutoModelForCausalLM
    c = {**DEFAULTS, **cfg}
    eps = load_episodes(a.episodes)
    eps = [dict(e, keep=keep_positions(e, c["keep"]) if c["keep_weight"] > 0 else []) for e in eps]
    eps = [e for e in eps if e["forced"] or e["down"] or e["keep"]]
    model = AutoModelForCausalLM.from_pretrained(a.model, torch_dtype=torch.float32, attn_implementation="sdpa").to(a.device)
    model.gradient_checkpointing_enable()
    model.train()
    named = [(n, p) for n, p in model.named_parameters() if p.requires_grad]
    params = [p for _, p in named]
    n = sum(p.numel() for p in params)
    kind = [next((k for k, parts in KINDS if any(s in nm for s in parts)), "other") for nm, _ in named]

    def grad(batch):
        model.zero_grad(set_to_none=True)
        nk = sum(len(e["keep"]) for e in batch)
        nf, nd = sum(sum(e["forced_w"]) for e in batch), sum(sum(e["down_w"]) for e in batch)
        for e in batch:
            x, dn, kp = target_logps(model, e, True, keep=True)
            loss = episode_loss(c, e, x, dn, kp, nf, nd, nk)
            if torch.is_tensor(loss):
                loss.backward()

    def sizes():
        l1 = [float(p.grad.abs().sum(dtype=torch.float64)) for p in params]
        sq = [float(p.grad.pow(2).sum(dtype=torch.float64)) for p in params]
        L1, L2 = sum(l1), math.sqrt(sum(sq))
        clip = min(1.0, c["max_grad_norm"] / L2) if c["max_grad_norm"] > 0 else 1.0
        below = sum(int((p.grad.abs() * clip < c["eps"]).sum()) for p in params)
        by = {k: {"weights": sum(p.numel() for p, kk in zip(params, kind) if kk == k) / n,
                  "l1": sum(v for v, kk in zip(l1, kind) if kk == k) / L1, "l2sq": sum(v for v, kk in zip(sq, kind) if kk == k) / L2 ** 2}
              for k in sorted(set(kind))}
        return {"l2": L2, "l1": L1, "spread": L1 / (math.sqrt(n) * L2), "below_eps": below / n, "by_kind": by}

    out = {"n_params": n, "episodes": len(eps), "settings": {k: c[k] for k in ("forced_weight", "keep_weight", "keep", "down_weight", "max_grad_norm", "eps")}}
    grad(eps)
    out["batch"] = sizes()
    print(json.dumps({"batch": out["batch"]}), file=sys.stderr, flush=True)
    total = [torch.zeros_like(p) for p in params]
    for e in eps:
        grad([e])
        for t, p in zip(total, params):
            t.add_(p.grad)
    out["single"] = []
    for e in eps[:a.geometry]:
        grad([e])
        r = dict(sizes(), id=e["id"], forced=len(e["forced"]), tokens=len(e["ids"]))
        dot = sdot = agree = osq = 0.0
        for t, p in zip(total, params):
            o = t - p.grad
            dot, sdot = dot + float((p.grad * o).sum(dtype=torch.float64)), sdot + float((p.grad.sign() * o).sum(dtype=torch.float64))
            agree, osq = agree + float(((p.grad > 0) == (o > 0)).sum()), osq + float(o.pow(2).sum(dtype=torch.float64))
        r["to_others"] = {"cos_grad": dot / (r["l2"] * math.sqrt(osq)), "cos_sign": sdot / (math.sqrt(n) * math.sqrt(osq)), "sign_agree": agree / n,
                          "per_own_grad": dot / r["l2"] ** 2, "per_own_sign": sdot / r["l1"]}
        out["single"].append(r)
        print(json.dumps({k: r[k] for k in ("id", "l2", "l1", "spread", "below_eps", "to_others")}), file=sys.stderr, flush=True)
    json.dump(out, open(a.out, "w"), indent=1)
    print(json.dumps({"batch": {k: out["batch"][k] for k in ("l2", "l1", "spread", "below_eps")}, "singles": len(out["single"])}))


def precision_check(a):
    global FORWARD
    import torch
    from transformers import AutoModelForCausalLM
    eps = []
    for line in open(a.episodes):
        e = json.loads(line)["episode"]
        pl, ids = len(e["prompt_tokens"]), (e["prompt_tokens"] + e["completion"])[:3000]
        eps.append({"ids": ids, "forced": list(range(pl, len(ids))), "down": []})
        if len(eps) >= a.n:
            break
    for attn in ("sdpa", "eager"):
        model = AutoModelForCausalLM.from_pretrained(a.model, torch_dtype=torch.float32, attn_implementation=attn).to(a.device)
        model.eval()
        diffs = []
        for e in eps:
            FORWARD = "f32"
            x = target_logps(model, e, False)[0]
            FORWARD = "bf16"
            y = target_logps(model, e, False)[0]
            diffs.append((x - y).abs())
        d = torch.cat(diffs)
        flips = 0
        print(f"{attn} attention, bf16 forward against f32, {len(d)} tokens of {len(eps)} answers: mean |difference in log-prob| "
              f"{float(d.mean()):.4f}, 99th percentile {float(d.quantile(0.99)):.3f}, max {float(d.max()):.2f}", flush=True)
        del model
        torch.cuda.empty_cache()
    FORWARD = "f32"


def selftest(a):
    import random, tempfile, torch
    from transformers import AutoModelForCausalLM
    rnd = random.Random(0)
    V = 5000

    def ep(i, untargeted):
        prompt = [rnd.randrange(1000, V) for _ in range(20)]
        comp = [rnd.randrange(1000, V) for _ in range(12)]
        forks = [] if untargeted else [{"pos": 3 + i % 4, "forced": [comp[3 + i % 4]], "rejected": [7]}]
        e = {"prompt": {"id": f"t{i}"}, "prompt_tokens": prompt, "completion": comp, "forks": forks, "outcome": "done"}
        if i == 0:
            e["down"] = [{"pos": 9, "token": comp[9]}]
        return e
    tmp = tempfile.mkdtemp(prefix="train-hf-selftest-")
    path = os.path.join(tmp, "eps.jsonl")
    with open(path, "w") as f:
        for i in range(6):
            f.write(json.dumps({"round": 0, "index": i, "episode": ep(i, i >= 4)}) + "\n")
        f.write('{"round": 0, "index": 6, "episode": {"prompt"\n')
    eps = load_episodes(path)
    assert [len(e["forced"]) for e in eps] == [1, 1, 1, 1, 0, 0]

    def fresh():
        return AutoModelForCausalLM.from_pretrained(a.model, torch_dtype=torch.float32).to(a.device)
    m = fresh()
    res = train(a.model, path, os.path.join(tmp, "run"), model=m, lr=1e-5, stages=[2, 4], device=a.device)
    s2, s4 = res[2], res[4]
    print(json.dumps(s4, indent=1))
    assert s4["forced_logp_after"] > s2["forced_logp_after"] > s4["forced_logp_before"], "forced tokens did not go up"
    assert (s4["episodes"], s4["forced_tokens"], s4["steps"]) == (4, 4, 4)
    del m
    back = AutoModelForCausalLM.from_pretrained(os.path.join(tmp, "run", "ckpt-00004"), torch_dtype=torch.float32).to(a.device)
    fa, _ = measure(back, eps)
    print(f"reloaded checkpoint: forced {fa:.6f} (trained {s4['forced_logp_after']:.6f})")
    assert abs(fa - s4["forced_logp_after"]) < 1e-4, "export differs from the trained model"
    files = os.listdir(os.path.join(tmp, "run", "ckpt-00004"))
    assert "model.safetensors" in files and not any(f.endswith(".index.json") for f in files), files
    del back
    r1 = train(a.model, path, None, model=fresh(), lr=1e-5, stages=[2], device=a.device)[2]
    r2 = train(a.model, eps[:4], None, model=fresh(), lr=1e-5, stages=[2], device=a.device)[2]
    assert r1["episodes"] == 4 and abs(r1["forced_logp_after"] - r2["forced_logp_after"]) < 1e-5
    r3 = train(a.model, path, None, model=fresh(), lr=1e-3, stages=[2], minibatch=2, optimizer="sgdm", device=a.device)[2]
    assert r3["steps"] == 4 and r3["forced_logp_after"] > r3["forced_logp_before"], r3
    r4 = train(a.model, path, None, model=fresh(), lr=1e-5, stages=[3], forced_weight=0.0,
               down_weight=1.0, device=a.device)[3]
    assert r4["episodes"] == 4 and r4["down_tokens"] == 5 and r4["down_logp_after"] < r4["down_logp_before"], r4
    r5 = train(a.model, path, os.path.join(tmp, "run16"), model=fresh(), lr=1e-4, stages=[4], forward="bf16", device=a.device)[4]
    assert r5["forced_logp_after"] > r5["forced_logp_before"], r5
    back = AutoModelForCausalLM.from_pretrained(os.path.join(tmp, "run16", "ckpt-00004"), torch_dtype=torch.bfloat16).to(a.device)
    FORWARD_was = FORWARD
    globals()["FORWARD"] = "f32"
    fb, _ = measure(back, eps)
    globals()["FORWARD"] = FORWARD_was
    print(f"bf16 forward: trained forced {r5['forced_logp_after']:.4f}; the export loaded as a bf16 model reads {fb:.4f}")
    assert abs(fb - r5["forced_logp_after"]) < 0.05, "bf16-trained numbers do not match the bf16-loaded export"
    r6 = train(a.model, path, None, model=fresh(), lr=1e-5, stages=[3], keep_weight=1.0, device=a.device)[3]
    assert r6["keep_tokens"] == 4 * 11 + 2 * 12 - 1 and r6["keep_logp_last_step"] > r6["keep_logp_before"], r6
    assert r1["keep_tokens"] == 0
    assert train(a.model, path, None, model=fresh(), lr=1e-5, stages=[1], keep_weight=1.0, keep="end",
                 device=a.device)[1]["keep_tokens"] == 0
    m7 = fresh()
    before = {n: p.detach().clone() for n, p in m7.named_parameters()}
    train(a.model, path, None, model=m7, lr=1e-4, stages=[2], freeze="embed_tokens,lm_head", device=a.device)
    moved = {n: bool((p.detach() != before[n]).any()) for n, p in m7.named_parameters()}
    assert not any(v for n, v in moved.items() if "embed_tokens" in n or "lm_head" in n) and any(moved.values()), moved
    r8 = train(a.model, path, None, model=fresh(), lr=1e-5, stages=[3], pull=1.0, device=a.device)[3]
    assert abs(r8["forced_logp_after"] - r8["forced_logp_before"]) < 1e-5, r8
    r9 = train(a.model, path, None, model=fresh(), lr=1e-5, stages=[2], pull=0.5, device=a.device)[2]
    assert r9["forced_logp_before"] < r9["forced_logp_after"] < r1["forced_logp_after"], (r9, r1)
    r10 = train(a.model, eps[:4], os.path.join(tmp, "ut"), model=fresh(), lr=1e-5, stages=[40], until_taken=True, device=a.device)
    s10 = r10[40]
    assert s10["taken_after_steps"] is not None and s10["steps"] == s10["taken_after_steps"] < 40, s10
    assert s10["forced_logp_after"] > -0.6931 and os.path.exists(os.path.join(tmp, "ut", "ckpt-00040", "model.safetensors")), s10
    r11 = train(a.model, eps[:4], None, model=fresh(), lr=1e-7, stages=[1], measure=True, device=a.device)[1]
    r12 = train(a.model, eps[:4], None, model=fresh(), lr=1.0, stages=[1], optimizer="sgdm", momentum=0.0, max_grad_norm=0.0,
                match_sign=1e-7, measure=True, device=a.device)[1]
    up11, up12 = (r["forced_logp_after"] - r["forced_logp_before"] for r in (r11, r12))
    print(f"one step: adam moved {r11['moved_l2']:.4g} (largest {r11['moved_max']:.3g}, mean {r11['moved_mean_abs']:.3g}), targets {up11:+.4f}; "
          f"matched gradient step moved {r12['moved_l2']:.4g} (largest {r12['moved_max']:.3g}), targets {up12:+.4f}")
    assert 0.5e-7 < r11["moved_mean_abs"] < 1.5e-7 and r11["moved_max"] < 5e-6 and r11["moved_l2"] <= 1e-7 * math.sqrt(r11["n_params"]) * 1.5, r11
    assert abs(r12["moved_l2"] - r12["step_l2"][0]) < 0.05 * r12["step_l2"][0] and r12["moved_l2"] < r11["moved_l2"], (r11, r12)
    assert up11 > 0 and up12 > 0 and 0.3 < up12 / up11 < 3, (up11, up12)
    r13 = train(a.model, eps[:4], None, model=fresh(), lr=1.0, stages=[3], optimizer="sgdm", momentum=0.0, max_grad_norm=0.0,
                match_sign=1e-5, plain=2.0, measure=True, device=a.device)[3]
    r14 = train(a.model, eps[:4], None, model=fresh(), lr=1.0, stages=[1], optimizer="sgdm", momentum=0.0, max_grad_norm=0.0,
                match_sign=1e-5, measure=True, device=a.device)[1]
    assert abs(r13["step_l2"][0] - 2 * r14["step_l2"][0]) < 0.02 * r13["step_l2"][0], (r13["step_l2"], r14["step_l2"])
    for k in (1, 2):
        want = r13["step_l2"][0] * r13["grad_l2"][k] / r13["grad_l2"][0]
        assert abs(r13["step_l2"][k] - want) < 0.02 * want, (r13["step_l2"], r13["grad_l2"])
    print(f"plain steps: {[round(x, 6) for x in r13['step_l2']]} at gradients {[round(x, 3) for x in r13['grad_l2']]}")
    import types
    gp = os.path.join(tmp, "geometry.json")
    geometry(types.SimpleNamespace(model=a.model, episodes=path, out=gp, device=a.device, geometry=2), {})
    g = json.load(open(gp))
    assert len(g["single"]) == 2 and 0 < g["batch"]["spread"] <= 1 and all(-1 <= r["to_others"]["cos_grad"] <= 1 for r in g["single"]), g
    shutil.rmtree(tmp)
    print("train_hf selftest: all checks passed")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="the base model directory (transformers format)")
    ap.add_argument("--episodes"); ap.add_argument("--out")
    ap.add_argument("--lr", type=float, default=DEFAULTS["lr"])
    ap.add_argument("--stages", default="5", help="cumulative epochs at which to write a checkpoint")
    ap.add_argument("--weight-decay", type=float, default=0.0, help="the optimizer's own decay, toward ZERO; leave at 0 (see --pull)")
    ap.add_argument("--pull", type=float, default=0.0,
                    help="after every step, move each weight this fraction of the way back to its starting value (0 = off)")
    ap.add_argument("--optimizer", choices=["adamw", "sgdm"], default="adamw")
    ap.add_argument("--betas", default="0.9,0.999"); ap.add_argument("--eps", type=float, default=1e-8)
    ap.add_argument("--momentum", type=float, default=0.9)
    ap.add_argument("--max-grad-norm", type=float, default=1.0, help="global gradient-norm clip; 0 disables")
    ap.add_argument("--minibatch", type=int, default=0, help="episodes per optimizer step (0 = all: one step per epoch)")
    ap.add_argument("--forced-weight", type=float, default=1.0)
    ap.add_argument("--down-weight", type=float, default=0.0, help="push-down on rejected and marked tokens (0 = off)")
    ap.add_argument("--keep-weight", type=float, default=0.0,
                    help="also train the student to go on writing the rest of each corrected answer as it did (0 = off)")
    ap.add_argument("--keep", choices=["rest", "end"], default="rest",
                    help="rest: every answer token that is not forced; end: only the answer's end token")
    ap.add_argument("--until-taken", action="store_true",
                    help="stop once every forced token has more than half the probability; --stages is then the one cap on steps")
    ap.add_argument("--taken-p", type=float, default=0.5, help="--until-taken: the probability every forced token has to pass")
    ap.add_argument("--freeze", default="", help="comma-separated name parts of weights left at base, e.g. embed_tokens,lm_head")
    ap.add_argument("--forward", choices=["f32", "bf16"], default="f32",
                    help="precision of the training forward pass: bf16 trains the model a bf16 server holds")
    ap.add_argument("--precision-check", action="store_true", help="compare the bf16 forward with f32 on --episodes and exit")
    ap.add_argument("--n", type=int, default=40, help="--precision-check: episodes to read")
    ap.add_argument("--no-grad-checkpoint", action="store_true")
    ap.add_argument("--match-sign", type=float, default=0.0,
                    help="sgdm: every step is the gradient direction, as long as lowers the loss (first order) like a step of this size on every weight")
    ap.add_argument("--plain", type=float, default=0.0,
                    help="with --match-sign: a plain gradient step whose first step is this many matched steps long; later steps shorten with the gradient")
    ap.add_argument("--devices", type=int, default=1, help="lay the model over this many of the job's GPUs (a student whose weights and gradient do not fit one)")
    ap.add_argument("--fsdp", action="store_true", help="under torchrun: shard the model over the job's GPUs, each rank training its share of the corrections (a mixture-of-experts student over several GPUs)")
    ap.add_argument("--measure", action="store_true", help="also record how far the weights moved, and every step's gradient sizes")
    ap.add_argument("--geometry", type=int, default=0, help="no training: the gradients' sizes and agreement, for the batch and the first N corrections alone (--out FILE.json)")
    ap.add_argument("--seed", type=int, default=0); ap.add_argument("--device", default="cuda")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest(a)
    if a.precision_check:
        return precision_check(a)
    if not a.episodes or not a.out:
        ap.error("--episodes and --out are required")
    if a.geometry:
        return geometry(a, dict(forced_weight=a.forced_weight, down_weight=a.down_weight, keep_weight=a.keep_weight, keep=a.keep,
                                max_grad_norm=a.max_grad_norm, eps=a.eps))
    res = train(a.model, a.episodes, a.out, lr=a.lr, stages=[int(s) for s in a.stages.split(",")],
                weight_decay=a.weight_decay, optimizer=a.optimizer, betas=tuple(float(b) for b in a.betas.split(",")),
                eps=a.eps, momentum=a.momentum, max_grad_norm=a.max_grad_norm, minibatch=a.minibatch,
                forced_weight=a.forced_weight, down_weight=a.down_weight, forward=a.forward, grad_checkpoint=not a.no_grad_checkpoint,
                keep_weight=a.keep_weight, keep=a.keep, freeze=a.freeze, pull=a.pull, until_taken=a.until_taken, taken_p=a.taken_p,
                seed=a.seed, device=a.device, match_sign=a.match_sign, measure=a.measure, devices=a.devices, plain=a.plain, fsdp=a.fsdp)
    if int(os.environ.get("RANK", 0)) == 0:
        print(json.dumps(res[max(res)], indent=1))


if __name__ == "__main__":
    main()
