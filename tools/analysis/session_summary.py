import json, glob, time, os, collections, subprocess
now=time.time()
pass
for p in sorted(glob.glob("runs/*/state.json")):
    r=p.split("/")[1]; st=json.load(open(p)); V=st["versions"]
    L=[json.loads(l) for l in open(f"runs/{r}/agent.jsonl") if l.strip()]
    t0=float(open(f"runs/{r}/clock_start").read()); mins=(now-t0)/60
    def gain(n):
        m=V[n].get("marks")
        if not m or n=="v0": return None
        k=min(len(next(iter(m.values()))), len(next(iter(V["v0"]["marks"].values()))))
        return (sum(x[:k].count("1")/k for x in m.values())-sum(V["v0"]["marks"][i][:k].count("1")/k for i in m))/len(m)*100, k
    sc=[(gain(n)[0], n, gain(n)[1]) for n in V if gain(n)]
    best=max(sc) if sc else None; lg=gain(st["live"])
    kept=sum(1 for _ in open(f"runs/{r}/collected.jsonl")) if os.path.exists(f"runs/{r}/collected.jsonl") else 0
    acts=collections.Counter(a.get("action") for e in L for a in e["actions"] if isinstance(a, dict))
    job=json.loads(open(f"runs/{r}/config.jsonl").readline())["settings"]["SLURM_JOB_ID"]; f=glob.glob(f"logs/*-{job}.out")[0]
    cnt=lambda pat: int(subprocess.run(["grep","-ac",pat,f],capture_output=True,text=True).stdout or 0)
    notes=[]
    if cnt("content_filter"): notes.append("refused %d" % cnt("content_filter"))
    if cnt("HTTP 429"): notes.append("rate-limited %d" % cnt("HTTP 429"))
    if cnt("HARNESS FAILURE"): notes.append("HARNESS FAILURE %d" % cnt("HARNESS FAILURE"))
    if cnt("DUE TO PREEMPTION"): notes.append("EVICTED")
    if acts.get("pose"): notes.append("posed %d" % acts["pose"])
    quiet=(now-L[-1]["at"])/60 if L else 0
    if quiet>20: notes.append("quiet %d min" % quiet)
    done=os.path.exists(f"runs/{r}/result.json")
    if done: notes.append("FINISHED")
    print("%-15s %4.0fm %4d %5d %4d %-22s %-10s %5.2fM %6.2f %s" % (r, mins, kept, V[st["live"]].get("grads",0), len(V)-1, ("%+5.1f %s (k=%d)" % best) if best else "-", ("%+5.1f" % lg[0]) if lg else "no score", st["tok"]/1e6, st["usd"], "; ".join(notes)))
