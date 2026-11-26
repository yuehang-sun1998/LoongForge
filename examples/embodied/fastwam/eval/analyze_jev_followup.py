"""Read-only paired summary of the completed follow-up."""
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean


def summarize(rows):
    return {
        "n": len(rows),
        "replan": sum(r["choice"] == "replan" for r in rows),
        "mean_replan_probability": mean(r["replan_probability"] for r in rows) if rows else None,
    }


def main():
    root = Path("/mnt/cfs/sunyuehang/evaluate_results/jev_followup_compare_20260923_114840")
    rows = [json.loads(line) for line in (root / "responses.jsonl").read_text().splitlines() if line.strip()]
    assert len(rows) == 200
    assert len({(r["episode"], r["step"], r["variant"]) for r in rows}) == 200
    ok = [r for r in rows if r["status"] == "ok"]
    pairs = defaultdict(dict)
    earliest = {}
    for row in rows:
        ep = row["episode"]
        earliest[ep] = min(earliest.get(ep, row["step"]), row["step"])
    for row in ok:
        pairs[row["episode"], row["step"]][row["variant"]] = row
    matched = [p for p in pairs.values() if len(p) == 2]
    print("total", len(rows), "episodes", len(earliest), "ok", len(ok))
    print("errors", Counter((r.get("error_type"), r.get("error_message")) for r in rows if r["status"] != "ok"))
    print("models", Counter(r["response"].get("model") for r in ok))
    for variant in ("corrected", "history"):
        group = [r for r in ok if r["variant"] == variant]
        print(variant, summarize(group))
    print("matched", len(matched))
    for variant in ("corrected", "history"):
        print("paired", variant, summarize([p[variant] for p in matched]))
    print("paired flips", Counter((p["corrected"]["choice"], p["history"]["choice"]) for p in matched))
    for phase in ("early", "late"):
        for outcome in (0, 1):
            subset = [p for p in matched if
                      ("early" if p["corrected"]["step"] == earliest[p["corrected"]["episode"]] else "late") == phase
                      and p["corrected"]["outcome"] == outcome]
            for variant in ("corrected", "history"):
                print("paired subgroup", phase, "outcome", outcome, variant,
                      summarize([p[variant] for p in subset]))
    print("history zero span", sum(r["request"]["state"]["progress_summary"]["actual_span_steps"] == 0
                                   for r in ok if r["variant"] == "history"))


if __name__ == "__main__":
    main()
