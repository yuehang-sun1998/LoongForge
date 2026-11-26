"""Separate privileged-input LF experiment; does not touch existing evaluations."""
import os
from pathlib import Path
from loongforge.embodied.eval.orchestrator.config import load_config
from loongforge.embodied.eval.orchestrator.run import _run_libero_once
from loongforge.embodied.eval.shadow import ShadowDecisionModule, ShadowConfig


def main():
    root = Path(__file__).resolve().parents[4]
    path = root / "examples/embodied/fastwam/eval/configs/libero/libero_10_task4_jev_mid_lf.yaml"
    config = load_config(str(path))
    config["server"].update(port=12660, health_port=12661)
    config["run"]["output_dir"] = "/mnt/cfs/sunyuehang/evaluate_results/loongforge_jev_task4_50_enriched_lf"
    config["model"]["name"] = "fastwam-task4-jev-enriched-lf"
    config["shadow"].update(enriched_inputs=True, sample_offset=5)
    assert config["benchmark"]["episodes_per_task"] == 50
    assert os.environ.get("OPENROUTER_API_KEY"), "Missing API key; refusing GPU launch"
    assert not Path(config["run"]["output_dir"]).exists(), "Output exists; refusing mixed run"
    probe = ShadowDecisionModule(ShadowConfig.from_raw_config(config))
    response = probe.observe(canonical_obs={}, ctx={"episode_step": 5, "instruction": "API connectivity preflight"},
                             primary_response={}, primary_action=[0]*7, env_action=[0]*7)
    assert response.get("status") == "ok", f"Jev preflight failed: {response.get('error_type')}"
    assert response.get("answers", {}).get("next_operation", {}).get("choice") in ("continue", "replan")
    print("JEV_PREFLIGHT_PASS; starting enriched LF experiment", flush=True)
    _run_libero_once(config, str(path))


if __name__ == "__main__":
    main()
