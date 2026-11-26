"""Follow-up Jev comparison across all completed enriched LF episodes."""
import copy
import json
import math
import os
import random
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError

from loongforge.embodied.eval.shadow import DEFAULT_QUESTIONS, ShadowDecisionModule, ShadowConfig

ROOT = Path("/mnt/cfs/sunyuehang/evaluate_results/loongforge_jev_task4_50_enriched_lf")


def dist(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def progress(state, states):
    step = state["episode_step"]
    history = [x for x in states if step - 100 <= x["episode_step"] <= step]
    first = history[0]
    changes = {}
    for name in first["scene"]["entities"].keys() & state["scene"]["entities"].keys():
        changes[name] = {
            "net_displacement_m": dist(
                first["scene"]["entities"][name]["position_world_m"],
                state["scene"]["entities"][name]["position_world_m"],
            )
        }
    goals = []
    for predicate in state["scene"].get("desired_goal_predicates", []):
        if len(predicate) != 3:
            continue
        _, subject, target = predicate
        if subject not in first["scene"]["entities"] or target not in first["scene"]["entities"]:
            continue
        start = dist(first["scene"]["entities"][subject]["position_world_m"],
                     first["scene"]["entities"][target]["position_world_m"])
        end = dist(state["scene"]["entities"][subject]["position_world_m"],
                   state["scene"]["entities"][target]["position_world_m"])
        goals.append({"subject": subject, "target": target,
                      "distance_reduction_m": start - end})
    return {"observed_steps": [x["episode_step"] for x in history],
            "actual_span_steps": step - history[0]["episode_step"],
            "entity_changes": changes, "goal_distance_changes": goals,
            "history_is_sparse": True,
            "caution": "Geometric progress is not task success; no future information."}


def questions():
    q = copy.deepcopy(DEFAULT_QUESTIONS)
    q["next_operation"]["criteria"] = {
        "continue": (
            "Execute the current action and remaining cached execution window. "
            "After that window, regenerate normally."
        ),
        "replan": (
            "Execute the current action, discard only the remaining cached actions, "
            "and regenerate a new chunk on the next step from the updated observation "
            "with the same policy, instruction, and fixed noise seed. No recovery or new strategy."
        ),
    }
    return q


def validate_operation(response):
    answers = response.get("answers")
    if not isinstance(answers, dict):
        raise ValueError("response.answers is missing or not an object")
    operation = answers.get("next_operation")
    if not isinstance(operation, dict):
        raise ValueError("response.answers.next_operation is missing or not an object")
    choice = operation.get("choice")
    if choice not in ("continue", "replan"):
        raise ValueError(f"unsupported next_operation.choice: {choice!r}")
    probabilities = operation.get("probabilities")
    if not isinstance(probabilities, dict):
        raise ValueError("next_operation.probabilities is missing or not an object")
    values = {}
    for name in ("continue", "replan"):
        value = probabilities.get(name)
        if not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise ValueError(f"invalid {name} probability: {value!r}")
        values[name] = float(value)
    return choice, values, operation.get("confidence")


def main():
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is required")
    rows = [json.loads(x) for x in (ROOT / "results.jsonl").read_text().splitlines() if x.strip()]
    jobs = []
    for row in sorted(rows, key=lambda x: x["episode_idx"]):
        trace = json.loads(Path(row["trace_path"]).read_text())
        states = [x["shadow"]["request_state"] for x in trace
                  if x.get("shadow", {}).get("status") == "ok"
                  and "request_state" in x["shadow"]]
        if len(states) < 2:
            continue
        late_index = max(1, int((len(states) - 1) * 0.8))
        chosen = [states[0], states[late_index]]
        for state in chosen:
            for variant in ("corrected", "history"):
                jobs.append({"episode": row["episode_idx"], "outcome": int(row["success"]),
                             "step": state["episode_step"], "variant": variant,
                             "state": state, "progress": progress(state, states)})
    random.Random(42).shuffle(jobs)
    resume_dir = os.environ.get("JEV_FOLLOWUP_RESUME_DIR")
    if resume_dir:
        output = Path(resume_dir)
        response_path = output / "responses.jsonl"
        if not response_path.exists():
            raise RuntimeError(f"resume responses file does not exist: {response_path}")
        completed = response_path.read_text().splitlines()
        start_index = len([line for line in completed if line.strip()])
        if start_index > len(jobs):
            raise RuntimeError("resume file contains more records than generated jobs")
        fields = ("episode", "outcome", "step", "variant")
        manifest = [{k: job[k] for k in fields} for job in jobs]
        if json.loads((output / "jobs.json").read_text()) != manifest:
            raise RuntimeError("Resume job manifest mismatch")
        for index, line in enumerate(x for x in completed if x.strip()):
            record = json.loads(line)
            if {k: record[k] for k in fields} != manifest[index]:
                raise RuntimeError("Resume record order mismatch")
    else:
        output = Path("/mnt/cfs/sunyuehang/evaluate_results") / (
            "jev_followup_compare_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        )
        output.mkdir()
        (output / "jobs.json").write_text(
            json.dumps(
                [{k: job[k] for k in ("episode", "outcome", "step", "variant")} for job in jobs],
                ensure_ascii=False,
                indent=2,
            )
        )
        start_index = 0
    client = ShadowDecisionModule(
        ShadowConfig(
            enabled=True,
            timeout_sec=float(os.environ.get("JEV_TIMEOUT_SEC", "60")),
        )
    )
    q = questions()
    print("OUTPUT", output, "RESUME_INDEX", start_index, flush=True)
    consecutive_errors = 0
    with (output / "responses.jsonl").open("a" if resume_dir else "x") as stream:
        for i, job in enumerate(jobs[start_index:], start=start_index):
            state = copy.deepcopy(job["state"])
            if job["variant"] == "history":
                state["progress_summary"] = job["progress"]
            body = {"model": "~typesafe/jev-latest", "state": state, "questions": q}
            result = {
                k: job[k] for k in ("episode", "outcome", "step", "variant")
            }
            result["request"] = body
            fatal = False
            try:
                response = client._request(body)
                choice, probabilities, confidence = validate_operation(response)
                result.update(
                    status="ok",
                    choice=choice,
                    replan_probability=probabilities["replan"],
                    continue_probability=probabilities["continue"],
                    confidence=confidence,
                    response=response,
                )
                consecutive_errors = 0
            except Exception as exc:
                result.update(
                    status="error",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
                if isinstance(exc, HTTPError):
                    result["http_status"] = exc.code
                    fatal = exc.code in (401, 402, 403, 429)
                consecutive_errors += 1
            stream.write(json.dumps(result) + "\n")
            stream.flush()
            print(i + 1, "/", len(jobs), result["status"], flush=True)
            if fatal or consecutive_errors >= 3:
                raise RuntimeError(
                    "Stopping on authentication/budget/rate-limit or consecutive failures"
                )
    print("OUTPUT", output)


if __name__ == "__main__":
    main()
