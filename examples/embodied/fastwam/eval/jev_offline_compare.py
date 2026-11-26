"""Fixed-sample, read-only trajectory replay against Jev (no policy/environment)."""
import copy
import json
import math
import os
import random
from pathlib import Path
from datetime import datetime
from urllib.error import HTTPError

from loongforge.embodied.eval.shadow import DEFAULT_QUESTIONS, ShadowConfig, ShadowDecisionModule

ROOT = Path("/mnt/cfs/sunyuehang/evaluate_results/loongforge_jev_task4_50_enriched_lf")


def distance(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def progress(current, available):
    """Summarize only recorded observations from the preceding 100 steps."""
    now = current["episode_step"]
    points = [p for p in available if now - 100 <= p["episode_step"] <= now]
    first = points[0]
    a = first["scene"]["entities"]
    b = current["scene"]["entities"]
    changes = {}
    for name in sorted(set(a) & set(b)):
        changes[name] = {
            "net_displacement_m": distance(a[name]["position_world_m"], b[name]["position_world_m"]),
        }
    goals = []
    for predicate in current["scene"].get("desired_goal_predicates", []):
        if len(predicate) != 3:
            continue
        _, subject, target = predicate
        if not all(n in a and n in b for n in (subject, target)):
            continue
        start = distance(a[subject]["position_world_m"], a[target]["position_world_m"])
        end = distance(b[subject]["position_world_m"], b[target]["position_world_m"])
        goals.append({"subject": subject, "target": target, "start_distance_m": start,
                      "current_distance_m": end, "distance_reduction_m": start - end})
    return {
        "observed_steps": [p["episode_step"] for p in points],
        "actual_span_steps": now - first["episode_step"],
        "history_is_sparse": True,
        "entity_changes": changes,
        "goal_distance_changes": goals,
        "caution": "Net displacement is not path length. Center distance is not task success. "
                   "Little movement may be normal during contact or release. No future data included.",
    }


def main():
    assert os.environ.get("OPENROUTER_API_KEY"), "API key required"
    rng = random.Random(42)
    rows = [json.loads(line) for line in (ROOT / "results.jsonl").read_text().splitlines() if line.strip()]
    # Five successful and five failed episodes, three temporal positions each.
    # Outcome is selection metadata ONLY, never included in request bodies.
    chosen = []
    for outcome in (0, 1):
        group = sorted([r for r in rows if r["success"] == outcome], key=lambda r: r["episode_idx"])
        chosen.extend(rng.sample(group, 5))
    chosen.sort(key=lambda r: r["episode_idx"])
    samples = []
    for index, row in enumerate(chosen):
        trace = json.loads(Path(row["trace_path"]).read_text())
        states = [t["shadow"]["request_state"] for t in trace
                  if t.get("shadow", {}).get("status") == "ok" and "request_state" in t["shadow"]]
        for fraction in (0.1, 0.5, 0.9):
            state = states[round((len(states) - 1) * fraction)]
            samples.append({
                "episode": row["episode_idx"], "outcome": row["success"],
                "split": "development" if index % 2 == 0 else "heldout",
                "fraction": fraction, "state": state, "progress": progress(state, states),
            })
    output = ROOT.parent / ("jev_offline_compare_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    output.mkdir(exist_ok=False)
    (output / "samples.json").write_text(json.dumps(samples, ensure_ascii=False, indent=2))
    print("OUTPUT", output, flush=True)
    client = ShadowDecisionModule(ShadowConfig(enabled=True, timeout_sec=30))
    jobs = [(sample, variant) for sample in samples for variant in ("original", "corrected", "history")]
    rng.shuffle(jobs)
    consecutive_errors = 0
    with (output / "responses.jsonl").open("x") as stream:
        for i, (sample, variant) in enumerate(jobs):
            state = copy.deepcopy(sample["state"])
            questions = copy.deepcopy(DEFAULT_QUESTIONS)
            if variant != "original":
                questions["next_operation"]["criteria"]["replan"] = (
                    "Execute the current action, discard the remaining cached actions, then regenerate "
                    "a chunk next step from the updated observation using the SAME policy, instruction "
                    "and fixed noise seed. This does not introduce recovery or a new strategy."
                )
                questions["next_operation"]["criteria"]["continue"] = (
                    "Execute the current action and remaining cached execution window; regenerate "
                    "normally after that window. Judge whether earlier feedback would help."
                )
            if variant == "history":
                state["progress_summary"] = sample["progress"]
            body = {"model": "~typesafe/jev-latest", "state": state, "questions": questions}
            record = {k: sample[k] for k in ("episode", "outcome", "split", "fraction")}
            record.update(variant=variant, step=state["episode_step"], request=body)
            fatal = False
            try:
                response = client._request(body)
                op = response.get("answers", {}).get("next_operation", {})
                assert op.get("choice") in ("continue", "replan"), "Invalid choice response"
                assert isinstance(op.get("probabilities"), dict), "Missing probabilities"
                record.update(status="ok", response=response)
                consecutive_errors = 0
            except Exception as exc:
                record.update(status="error", error_type=type(exc).__name__)
                if isinstance(exc, HTTPError):
                    record["http_status"] = exc.code
                    fatal = exc.code in (401, 402, 403, 429)
                consecutive_errors += 1
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            stream.flush()
            print(i + 1, "/", len(jobs), variant, record["status"], flush=True)
            if fatal or consecutive_errors >= 3:
                raise RuntimeError("Stopping on authentication/budget/rate-limit or consecutive failures")


if __name__ == "__main__":
    main()
