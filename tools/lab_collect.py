import copy, json, os, re, sys, threading, traceback
sys.path.insert(0, os.path.dirname(__file__))
import lab_session, session_base
from session_base import Student


class Shared(lab_session.Lab):

    def __init__(self, a, srv, mine, shared_run):
        self.shared_srv, self.own, self.shared_run = srv, set(mine), shared_run
        a.fix_only = True
        super().__init__(a)
        self.counted = []

    def go_live(self, name):
        seat = self.seats[0]
        seat.name, seat.srv, seat.status = name, self.shared_srv, {}
        self.seat, self.original = seat, {}
        return f"{name} is the live student. Its answer to a pool task is written when you open the task."

    def listed(self, i):
        return i in self.own

    def idx_of(self, act):
        i = super().idx_of(act)
        if i not in self.own:
            raise ValueError(f"task {i} is not one of yours: {sorted(self.own)}")
        return i

    def prompt_vars(self):
        return {**super().prompt_vars(), "pool": len(self.own)}

    def kept(self):
        ids = {self.rows[i]["id"] for i in self.own}
        p = os.path.join(self.shared_run, "collected.jsonl")
        out = []
        for n, line in enumerate(open(p) if os.path.exists(p) else []):
            r = json.loads(line)
            if r["episode"]["prompt"]["id"] in ids:
                out.append({"key": f"c{n}", "round": r.get("round", 0), "id": r["episode"]["prompt"]["id"], "line": line,
                            "forced": len(r["episode"]["forks"]) + sum(m["w"] > 0 for m in r["episode"].get("marks", [])),
                            "down": len(r["episode"].get("down", []))})
        return out


def rows_of(config):
    return [json.loads(l) for l in open(next(l.split('"')[1] for l in open(config) if l.startswith("prompts = ")))]


def usable(a, rows):
    ok = [i for i in range(len(rows)) if rows[i]["tokens"] + 32 < a.max_train_tokens]
    if len(ok) < len(rows):
        print(f"{len(rows) - len(ok)} tasks are longer than a correction may be ({a.max_train_tokens:,} tokens): left out",
              file=sys.stderr, flush=True)
    return ok


def collect(a, model_dir, tasks, run):
    a = copy.copy(a)
    os.makedirs(run, exist_ok=True)
    if os.path.abspath(os.path.dirname(a.config)) != os.path.abspath(run):
        cfg = re.sub(r'(?m)^run_dir = .*$', f'run_dir = "{run}"', open(a.config).read())
        a.config = os.path.join(run, "ui.toml")
        open(a.config, "w").write(cfg)
    a.run = run
    rows = rows_of(a.config)
    gpus = a.job_gpus
    students = [Student(a, ",".join(gpus[k:k + a.server_gpus]), n)
                for n, k in enumerate(range(0, len(gpus) - a.server_gpus + 1, a.server_gpus))]
    srvs = [None] * len(students)

    def up(n):
        srvs[n] = students[n].start(model_dir, 0)
    starting = [threading.Thread(target=up, args=(n,)) for n in range(len(students))]
    for t in starting:
        t.start()
    for t in starting:
        t.join()
    if None in srvs:
        for st in students:
            st.stop()
        raise RuntimeError("a student server did not come up; see the run's server logs")
    groups = [tasks[k:k + a.tasks_per] for k in range(0, len(tasks), a.tasks_per)]
    answer = int(os.environ.get("MATH_EVAL_MAX_TOKENS", 16384))
    cap = [st.kv_tokens() for st in students]
    used, admit = [0] * len(srvs), threading.Condition()
    print(f"{len(srvs)} student servers ({cap[0]:,} KV tokens each) of {model_dir} for {len(tasks)} tasks; {len(groups)} "
          f"conversations of {a.tasks_per}, admitted while their tasks fit", file=sys.stderr, flush=True)
    results, threads = {}, []

    def one(k, mine):
        need = sum(rows[i]["tokens"] + answer for i in mine)
        with admit:
            while True:
                n = max(range(len(srvs)), key=lambda n: cap[n] - used[n])
                if used[n] == 0 or used[n] + need <= cap[n]:
                    used[n] += need
                    break
                admit.wait()
        ak = copy.copy(a)
        ak.run = os.path.join(run, f"w{k}")
        os.makedirs(ak.run, exist_ok=True)
        try:
            s = Shared(ak, srvs[n], mine, run)
            s.run()
            results[k] = json.load(open(os.path.join(ak.run, "result.json")))
        except Exception:
            results[k] = {"error": traceback.format_exc(limit=4)}
            traceback.print_exc()
        finally:
            with admit:
                used[n] -= need
                admit.notify_all()
    try:
        for k, mine in enumerate(groups):
            t = threading.Thread(target=one, args=(k, mine), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
    finally:
        for st in students:
            st.stop()
    fixes = [c for r in results.values() for c in (r.get("fixes") or {}).get("corrections", [])]
    summary = {"conversations": len(groups), "failed": sorted(k for k, r in results.items() if "error" in r),
               "corrections": len(fixes), "criteria": sum(c["n"] for c in fixes),
               "passed_before": sum(c["before"] for c in fixes), "passed_after": sum(c["after"] for c in fixes),
               "editor_usd": round(max([r.get("editor_usd", 0) for r in results.values()] or [0]), 2),
               "judge_usd": round(sum(r.get("judge_usd", 0) for r in results.values()), 2)}
    json.dump(summary, open(os.path.join(run, "summary.json"), "w"), indent=1)
    print("COLLECTED:", json.dumps(summary), file=sys.stderr, flush=True)
    return summary


def options(ap):
    ap.add_argument("--tasks-per", type=int, default=1, help="pool tasks each conversation works on")
    ap.add_argument("--server-gpus", type=int, default=1, help="GPUs of each student server (one server per that many GPUs)")
    ap.add_argument("--wall-min", type=float, default=0,
                    help="minutes a conversation may take; then it keeps what it has and ends (a collection lasts as long as its slowest conversation)")
    return ap


def main():
    ap = options(lab_session.parser())
    a = ap.parse_args()
    a.url, a.log, a.fix_only = f"http://127.0.0.1:{a.port}", a.run, True
    a.job_gpus = [g for g in os.environ.get("CUDA_VISIBLE_DEVICES", "").split(",") if g] or [str(g) for g in range(8)]
    a.gpus = [None]
    a.env = dict(os.environ, MRL_GPUS=str(a.server_gpus))
    base = next(l.split('"')[1] for l in open(a.config) if l.startswith("model = "))
    collect(a, base, usable(a, rows_of(a.config)), a.run)


if __name__ == "__main__":
    main()
