#!/usr/bin/env python3
import json, os, random, sys


def main():
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    for spec in sys.argv[2:]:
        name, rest = spec.split("=", 1)
        run, what, *more = rest.split(":")
        lines = list(open(os.path.join(run, "collected.jsonl")))
        if what == "all":
            pick = list(range(len(lines)))
        elif what == "random":
            pick = sorted(random.Random(int(more[1])).sample(range(len(lines)), int(more[0])))
        else:
            inc = json.load(open(os.path.join(run, "state.json")))["versions"][what]["included"]
            pick = sorted(int(k[1:]) for k in inc if k.startswith("c"))
        if what != "random" and more:
            by_id = {json.loads(l)["id"]: n for n, l in enumerate(open(os.path.join(run, "prompts.jsonl")))}
            named = {int(x) for x in more[1].split(",")}
            idx = {n: by_id[json.loads(lines[n])["episode"]["prompt"]["id"]] for n in pick}
            assert named <= set(idx.values()), (spec, sorted(named - set(idx.values())))
            pick = [n for n in pick if (idx[n] in named) == (more[0] == "only")]
            what += f", {more[0]} {more[1]}"
        assert pick and max(pick) < len(lines), (spec, pick, len(lines))
        open(os.path.join(out, name + ".jsonl"), "w").writelines(lines[n] for n in pick)
        eps = [json.loads(lines[n])["episode"] for n in pick]
        frac = sorted(e["forks"][0]["pos"] / max(1, len(e["completion"])) for e in eps if e.get("forks"))
        prob = sorted(e["forks"][0].get("forced_prob", 0) for e in eps if e.get("forks"))
        print(f"{name}: {len(pick)} corrections from {os.path.basename(run.rstrip('/'))} ({what}); first fork at a median "
              f"{100 * frac[len(frac) // 2]:.0f}% of the way through the answer, median prior probability {100 * prob[len(prob) // 2]:.0f}%")


if __name__ == "__main__":
    main()
