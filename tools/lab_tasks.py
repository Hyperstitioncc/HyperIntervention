import argparse, collections, hashlib, importlib.util, json, multiprocessing, os, random, subprocess, sys

ANSWER_LINE = ("Write the deliverable ({name}) as your answer, in Markdown: your answer is its full text. "
               "The documents of the matter follow, each under its path.")


def lab_parser(lab):
    spec = importlib.util.spec_from_file_location("lab_parse_doc", os.path.join(lab, "lab_core", "sandbox", "parsers", "parse_doc.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.PARSERS


def versions(lab):
    def ver(mod):
        try:
            return __import__(mod).__version__
        except Exception:
            return None
    try:
        pandoc = subprocess.run(["pandoc", "--version"], capture_output=True, text=True).stdout.split("\n")[0]
    except FileNotFoundError:
        pandoc = ""
    commit = subprocess.run(["git", "-C", lab, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    return {"commit": commit, "pandoc": pandoc, "markitdown": ver("markitdown"), "pandas": ver("pandas")}


def read_doc(parsers, path, rel):
    ext = os.path.splitext(path)[1].lower()[1:]
    if ext in parsers:
        try:
            return parsers[ext](path), False
        except Exception as e:
            tail = f"{type(e).__name__}: {e}".strip().splitlines()[-1]
            return f"Error: failed to parse /workspace/documents/{rel} ({ext}): {tail}", True
    return open(path, "rb").read().decode("utf-8", errors="replace"), False


_W = {}


def _init(lab, tokenizer):
    _W["parsers"] = lab_parser(lab)
    if tokenizer:
        from transformers import AutoTokenizer
        _W["tok"] = AutoTokenizer.from_pretrained(tokenizer)


def _read_one(doc):
    path, rel = doc
    text, failed = read_doc(_W["parsers"], path, rel)
    if failed and "TimeoutExpired" in text:
        text, failed = read_doc(_W["parsers"], path, rel)
    return hashlib.sha1(open(path, "rb").read()).hexdigest(), text, failed


def _count(prompt):
    return len(_W["tok"](prompt, add_special_tokens=False)["input_ids"])


def eligible(task, area):
    return area not in ("contracts", "firm-knowledge") and len(task.get("deliverables") or {}) == 1


def build(a):
    ver = versions(a.lab)
    if not ver["pandoc"].startswith("pandoc 3.11"):
        sys.exit(f"LAB converts .docx with pandoc 3.11 ({ver['pandoc'] or 'no pandoc found'}): install it with "
                 f"{a.lab}/lab_core/sandbox/install_pandoc.sh PREFIX")
    root = os.path.join(a.lab, "tasks")
    tasks = []
    for d, _, files in sorted(os.walk(root)):
        if "task.json" not in files:
            continue
        tid = os.path.relpath(d, root)
        t = json.load(open(os.path.join(d, "task.json")))
        if not eligible(t, tid.split(os.sep)[0]):
            continue
        ddir = os.path.join(d, "documents")
        tasks.append((tid, t, [(os.path.join(r2, f), os.path.relpath(os.path.join(r2, f), ddir))
                               for r2, _, fs in sorted(os.walk(ddir)) for f in sorted(fs)]))
    jobs = [doc for _, _, docs in tasks for doc in docs]
    print(f"{len(tasks)} eligible tasks, {len(jobs):,} documents, {a.workers} workers", file=sys.stderr, flush=True)
    with multiprocessing.Pool(a.workers, initializer=_init, initargs=(a.lab, a.tokenizer)) as pool:
        read = iter(pool.imap(_read_one, jobs, chunksize=4))
        rows, digests = [], collections.defaultdict(set)
        for tid, t, paths in tasks:
            docs, parts = [], []
            for _, rel in paths:
                digest, text, failed = next(read)
                digests[digest].add(tid)
                docs.append({"path": rel, "chars": len(text), **({"parse_error": True} if failed else {})})
                if failed:
                    print(f"{tid}: {rel}: {text}", file=sys.stderr)
                parts.append(f"=== documents/{rel} ===\n{text.rstrip()}\n")
            name = next(iter(t["deliverables"]))
            prompt = t["instructions"].strip() + "\n\n" + ANSWER_LINE.format(name=name) + "\n\n" + "\n".join(parts)
            del parts
            n_chars = len(prompt)
            if n_chars > a.max_chars:
                prompt = None
            rows.append({"id": tid, "area": tid.split(os.sep)[0], "work_type": t.get("work_type"), "title": t["title"],
                         "instructions": t["instructions"], "deliverable": name, "documents": docs, "prompt": prompt,
                         "criteria": t["criteria"], "lab": ver, "prompt_chars": n_chars})
            print(f"{len(rows)} {tid}: {len(docs)} documents, {n_chars:,} chars" + ("" if prompt else " (too long: no prompt kept)"),
                  file=sys.stderr, flush=True)
        if a.tokenizer:
            for r, n in zip(rows, pool.imap(_count, (r["prompt"] or "" for r in rows))):
                r["tokens"] = n if r["prompt"] else None
    for r, g in zip(rows, groups(rows, digests)):
        r["group"] = g
    with open(a.out + ".tmp", "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in rows)
    os.replace(a.out + ".tmp", a.out)
    n_groups = len({r["group"] for r in rows})
    bad = [(r["id"], d["path"]) for r in rows for d in r["documents"] if d.get("parse_error")]
    if bad:
        print(f"{len(bad)} documents in {len({i for i, _ in bad})} tasks failed LAB's parser; their text is the error line, "
              f"as LAB's agent sees it: {bad[:10]}", file=sys.stderr)
    print(f"{len(rows)} tasks in {n_groups} groups -> {a.out}  (LAB {ver['commit'][:10]}, {ver['pandoc']}, "
          f"markitdown {ver['markitdown']}, pandas {ver['pandas']})", file=sys.stderr)


def groups(rows, digests):
    parent = {r["id"]: r["id"] for r in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def join(ids):
        ids = sorted(ids)
        for x in ids[1:]:
            parent[find(x)] = find(ids[0])
    template = collections.defaultdict(list)
    for r in rows:
        template[os.sep.join(r["id"].split(os.sep)[:2])].append(r["id"])
    for ids in list(template.values()) + [sorted(ts) for ts in digests.values()]:
        join([i for i in ids if i in parent])
    comp = collections.defaultdict(list)
    for r in rows:
        comp[find(r["id"])].append(r["id"])
    name = {root: min(ids) for root, ids in comp.items()}
    return [name[find(r["id"])] for r in rows]


def split(a):
    rows = [json.loads(l) for l in open(a.rows)]
    if a.max_tokens:
        if any("tokens" not in r for r in rows):
            sys.exit("--max-tokens needs rows built with --tokenizer")
        rows = [r for r in rows if r["tokens"] is not None and r["tokens"] <= a.max_tokens]
    by = collections.defaultdict(list)
    for r in rows:
        by[r["group"]].append(r)
    keys = sorted(by)
    random.Random(a.seed).shuffle(keys)
    want = {"test": a.test * len(rows), "val": a.val * len(rows)}
    out = {"test": [], "val": [], "pool": []}
    for k in keys:
        part = next((p for p in ("test", "val") if len(out[p]) < want[p]), "pool")
        out[part] += by[k]
    os.makedirs(a.out, exist_ok=True)
    for p, rs in out.items():
        with open(os.path.join(a.out, f"{p}.jsonl"), "w") as f:
            f.writelines(json.dumps(r) + "\n" for r in rs)
    seen = {p: {r["group"] for r in rs} for p, rs in out.items()}
    assert not (seen["test"] & seen["val"] or seen["test"] & seen["pool"] or seen["val"] & seen["pool"]), "a group in two parts"
    for p, rs in out.items():
        print(f"{p}: {len(rs)} tasks, {len(seen[p])} groups, {sum(len(r['criteria']) for r in rs):,} criteria, "
              f"areas {dict(collections.Counter(r['area'] for r in rs).most_common(5))} ...")


def prompt_tokens(tok, prompt):
    template = tok.apply_chat_template([{"role": "user", "content": "{prompt}"}], add_generation_prompt=True, tokenize=False)
    return tok(template.replace("{prompt}", prompt), add_special_tokens=False)["input_ids"]


def digest(ids):
    return hashlib.sha1(json.dumps(ids).encode()).hexdigest()


def episodes(a):
    from transformers import AutoTokenizer
    import gzip
    tok = AutoTokenizer.from_pretrained(a.model)
    rows = {r["id"]: r for f in a.rows for r in map(json.loads, open(f))}
    n = 0
    with open(a.out + ".tmp", "w") as out:
        for line in gzip.open(a.corrections, "rt"):
            c = json.loads(line)
            e = dict(c["correction"])
            r = rows.get(e["prompt"]["id"])
            if r is None or r["prompt"] is None:
                sys.exit(f"{e['prompt']['id']}: not among the built tasks")
            ids = prompt_tokens(tok, r["prompt"])
            if len(ids) != e.pop("prompt_token_count") or digest(ids) != e.pop("prompt_token_sha1"):
                sys.exit(f"{e['prompt']['id']}: the rebuilt prompt is not the one the correction was made on "
                         f"(LAB's documents read differently: check the pinned commit and pandoc 3.11)")
            e["prompt"] = {"id": r["id"], "prompt": r["prompt"]}
            e["prompt_tokens"] = ids
            out.write(json.dumps({"round": c.get("round", 0), "index": c.get("index", n), "episode": e}) + "\n")
            n += 1
    os.replace(a.out + ".tmp", a.out)
    print(f"{n} corrections, every prompt rebuilt exactly -> {a.out}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    b = sp.add_parser("build")
    b.add_argument("--lab", required=True, help="a checkout of github.com/harveyai/harvey-labs at the pinned commit")
    b.add_argument("--max-chars", type=int, default=2_000_000,
                   help="a longer prompt (over 400k tokens at any document type's ratio) is not kept or counted")
    b.add_argument("--workers", type=int, default=min(64, os.cpu_count()))
    b.add_argument("--out", required=True); b.add_argument("--tokenizer", help="the student's model directory, to count prompt tokens")
    s = sp.add_parser("split")
    s.add_argument("--rows", required=True); s.add_argument("--out", required=True)
    s.add_argument("--max-tokens", type=int, default=0, help="keep only tasks whose prompt has at most this many tokens")
    s.add_argument("--val", type=float, default=0.15); s.add_argument("--test", type=float, default=0.3)
    s.add_argument("--seed", type=int, default=0)
    e = sp.add_parser("episodes")
    e.add_argument("--corrections", required=True, help="published corrections (.jsonl.gz)")
    e.add_argument("--rows", required=True, nargs="+", help="the built tasks they were made on")
    e.add_argument("--model", required=True, help="the student's model directory (its tokenizer and chat template)")
    e.add_argument("--out", required=True)
    a = ap.parse_args()
    {"build": build, "split": split, "episodes": episodes}[a.cmd](a)


if __name__ == "__main__":
    main()
