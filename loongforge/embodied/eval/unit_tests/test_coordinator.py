# Copyright 2026 The LoongForge Authors.
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the coordinator (pure helpers)."""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest
from typing import Any

from loongforge.embodied.eval.orchestrator.coordinator import _shard_config


def _shard(sid: str, overrides: dict, expected: int) -> dict:
    return {"id": sid, "overrides": overrides, "expected": expected}


def test_shard_config_applies_overrides_and_strips_coordinator_keys() -> None:
    """The shard patch is applied mechanically; coordinator-only keys are dropped."""
    base = {
        "benchmark": {"suites": ["a", "b"], "suite_overrides": {"a": {"max_steps": 1}}},
        "server": {"ckpt_path": "x"},
        "coordinator": {"n_workers": 4},
        "servers": [{"gpus": [0, 1]}],
        "pools": {"cpu_sim": {"n_sims_per_server": 2}},
    }
    patch = {"benchmark": {"suite": "b", "task_ids": [3], "episode_offset": 10, "episodes_per_task": 20}}
    shard = _shard_config(base, _shard("b/task=3", patch, 20), "ws://h:1", "/tmp/out")
    assert "coordinator" not in shard and "servers" not in shard and "pools" not in shard
    assert "suites" not in shard["benchmark"] and "suite_overrides" not in shard["benchmark"]
    assert shard["benchmark"]["suite"] == "b"
    assert shard["benchmark"]["task_ids"] == [3]
    assert shard["benchmark"]["episode_offset"] == 10
    assert shard["benchmark"]["episodes_per_task"] == 20
    assert shard["server"]["external_url"] == "ws://h:1"
    assert shard["run"]["timestamped_output"] is False
    assert "coordinator" in base  # base not mutated


def test_shard_config_defaults_max_retries_but_respects_explicit() -> None:
    """Coordinated units retry infra episode failures by default; an explicit value wins."""
    base = {"benchmark": {"suite": "a"}, "server": {"ckpt_path": "x"}}
    shard = _shard_config(base, _shard("a/ep=0", {}, 2), "ws://h:1", "/tmp/out")
    assert shard["run"]["max_retries"] == 2
    explicit = {**base, "run": {"max_retries": 0}}
    assert _shard_config(explicit, _shard("a/ep=0", {}, 2), "ws://h:1", "/tmp/out")["run"]["max_retries"] == 0


class _FakeProc:
    """Minimal stand-in for ``subprocess.Popen`` in the dispatch loop."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        self.pid = 12345
        self.terminated = False

    def poll(self) -> int:
        """Run poll."""
        return self.returncode

    def terminate(self) -> None:
        """Run terminate."""
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        """Run wait."""
        return self.returncode

    def kill(self) -> None:
        """Run kill."""
        self.terminated = True


def test_run_retries_a_failed_unit_once(monkeypatch: Any, tmp_path: Any) -> None:
    """A unit that exits non-zero is re-queued once; a later success is not a failure."""
    from loongforge.embodied.eval.orchestrator import coordinator as coord

    launches: dict[int, int] = {}

    def fake_launch(config: Any, shard: Any, index: int, url: str, output_dir: Any, tmp: Any) -> _FakeProc:
        launches[index] = launches.get(index, 0) + 1
        return _FakeProc(1 if (index == 0 and launches[index] == 1) else 0)

    monkeypatch.setattr(coord, "_launch_unit", fake_launch)
    monkeypatch.setattr(coord, "start_server", lambda args, env=None: _FakeProc(0))
    monkeypatch.setattr(coord, "wait_healthz", lambda *args, **kwargs: None)
    monkeypatch.setattr(coord, "_aggregate", lambda *args, **kwargs: {})
    monkeypatch.setattr(coord, "short_shards", lambda *args, **kwargs: [])

    config = {
        "benchmark": {"name": "libero", "suite": "libero_10", "task_ids": [0], "episodes_per_task": 2},
        "servers": [{"gpus": [0]}],
        "pools": {"cpu_sim": {"n_sims_per_server": 1}},
        "run": {"output_dir": str(tmp_path)},
    }
    result = coord.run(config, "")
    assert result["n_units"] == 2
    assert launches[0] == 2  # failed once, retried
    assert result["failed_units"] == []


def test_watchdog_restarts_a_dead_server_in_place(monkeypatch: Any) -> None:
    """A server whose process exited is restarted on the same port by the watchdog."""
    from loongforge.embodied.eval.orchestrator import coordinator as coord

    started = []

    def fake_start(args: Any, env: Any = None) -> _FakeProc:
        proc = _FakeProc(None)  # alive
        started.append(proc)
        return proc

    monkeypatch.setattr(coord, "start_server", fake_start)
    monkeypatch.setattr(coord, "wait_healthz", lambda *args, **kwargs: None)

    args = SimpleNamespace(host="127.0.0.1", port=14560, health_port=14561, server_start_timeout_sec=5)
    servers = [(_FakeProc(1), args)]  # the server process has exited
    watchdog = coord._ServerWatchdog(servers, [0], interval_sec=0.05)
    watchdog.start()
    time.sleep(0.4)
    watchdog.stop()
    watchdog.join(timeout=2)

    assert watchdog.restarts == 1
    assert servers[0][0] is started[0]  # replaced in place, so the URL stays valid
    assert servers[0][1] is args


def test_watchdog_leaves_a_live_server_alone(monkeypatch: Any) -> None:
    """Run test_watchdog_leaves_a_live_server_alone."""
    from loongforge.embodied.eval.orchestrator import coordinator as coord

    monkeypatch.setattr(coord, "start_server", lambda *a, **k: _FakeProc(None))
    monkeypatch.setattr(coord, "wait_healthz", lambda *args, **kwargs: None)

    args = SimpleNamespace(host="127.0.0.1", port=14560, health_port=14561, server_start_timeout_sec=5)
    live = _FakeProc(None)
    servers = [(live, args)]
    watchdog = coord._ServerWatchdog(servers, [0], interval_sec=0.05)
    watchdog.start()
    time.sleep(0.3)
    watchdog.stop()
    watchdog.join(timeout=2)

    assert watchdog.restarts == 0
    assert servers[0][0] is live


def test_unit_timeout_derives_from_the_episode_budget() -> None:
    """The per-unit budget is max episodes x per_episode_sec + connect + slack."""
    from loongforge.embodied.eval.orchestrator import coordinator as coord

    shards = [_shard("a/t0", {}, 4), _shard("a/t1", {}, 2)]
    config = {"timeouts": {"per_episode_sec": 100}, "server": {"connect_timeout_sec": 10}}
    assert coord._unit_timeout_sec(config, shards) == 4 * 100 + 10 + coord.UNIT_TIMEOUT_SLACK_SEC
    assert coord._unit_timeout_sec({**config, "run": {"unit_timeout_sec": 42}}, shards) == 42


def test_await_slot_returns_a_finished_unit() -> None:
    """Run test_await_slot_returns_a_finished_unit."""
    from loongforge.embodied.eval.orchestrator import coordinator as coord

    done = _FakeProc(0)
    proc, timed_out = coord._await_slot({done: (0, 3)}, {3: time.time()}, timeout_sec=10)
    assert proc is done
    assert timed_out is False


def test_await_slot_kills_a_unit_that_blows_its_budget() -> None:
    """A unit hung past its budget is killed so the slot frees (it then gets retried)."""
    from loongforge.embodied.eval.orchestrator import coordinator as coord

    stuck = _FakeProc(None)  # never finishes
    pool = {stuck: (0, 7)}
    started_at = {7: time.time() - 10}  # already 10s into its budget
    proc, timed_out = coord._await_slot(pool, started_at, timeout_sec=0.5)
    assert proc is stuck
    assert timed_out is True


def test_run_exits_nonzero_when_coverage_is_incomplete(monkeypatch: Any, tmp_path: Any) -> None:
    """A coordinated run whose coverage is still incomplete must not exit 0."""
    import sys as _sys

    from loongforge.embodied.eval.orchestrator import coordinator as coord
    from loongforge.embodied.eval.orchestrator import run as run_mod

    cfg = tmp_path / "c.yaml"
    cfg.write_text("coordinator: {}\n", encoding="utf-8")
    monkeypatch.setattr(_sys, "argv", ["run", "--config", str(cfg)])
    monkeypatch.setattr(run_mod, "load_config", lambda path: {"coordinator": {}})
    monkeypatch.setattr(coord, "run", lambda config, path: {"validation": {"ok": False}})
    with pytest.raises(SystemExit) as excinfo:
        run_mod.main()
    assert excinfo.value.code == 2


def test_run_exits_zero_when_coverage_is_complete(monkeypatch: Any, tmp_path: Any) -> None:
    """Run test_run_exits_zero_when_coverage_is_complete."""
    import sys as _sys

    from loongforge.embodied.eval.orchestrator import coordinator as coord
    from loongforge.embodied.eval.orchestrator import run as run_mod

    cfg = tmp_path / "c.yaml"
    cfg.write_text("coordinator: {}\n", encoding="utf-8")
    monkeypatch.setattr(_sys, "argv", ["run", "--config", str(cfg)])
    monkeypatch.setattr(run_mod, "load_config", lambda path: {"coordinator": {}})
    monkeypatch.setattr(coord, "run", lambda config, path: {"validation": {"ok": True}})
    run_mod.main()  # must not raise


def test_watchdog_does_not_leak_a_restart_started_during_shutdown(monkeypatch: Any) -> None:
    """A restart still in flight when the run shuts down must not leak a server process."""
    from loongforge.embodied.eval.orchestrator import coordinator as coord

    created = []

    def fake_start(args: Any, env: Any = None) -> _FakeProc:
        proc = _FakeProc(None)
        created.append(proc)
        return proc

    monkeypatch.setattr(coord, "start_server", fake_start)
    args = SimpleNamespace(host="127.0.0.1", port=14560, health_port=14561, server_start_timeout_sec=5)
    servers = [(_FakeProc(1), args)]
    watchdog = coord._ServerWatchdog(servers, [0], interval_sec=0.05)
    # shutdown lands while the restart is waiting for the server to get healthy
    monkeypatch.setattr(coord, "wait_healthz", lambda *a, **k: watchdog.stop())

    watchdog.start()
    time.sleep(0.4)
    watchdog.stop()
    watchdog.join(timeout=2)

    assert watchdog.restarts == 0  # never counted as a successful restart
    assert servers[0][0].returncode == 1  # the original entry is untouched
    assert created and created[0].terminated  # the process we started was cleaned up


def test_watchdog_restarts_a_hung_server(monkeypatch: Any) -> None:
    """A live process that stops answering pings is restarted after the failure limit."""
    from loongforge.embodied.eval.orchestrator import coordinator as coord

    monkeypatch.setattr(coord, "start_server", lambda args, env=None: _FakeProc(None))
    monkeypatch.setattr(coord, "wait_healthz", lambda *args, **kwargs: None)
    args = SimpleNamespace(host="127.0.0.1", port=14560, health_port=14561, server_start_timeout_sec=5)
    servers = [(_FakeProc(None), args)]  # process alive, but wedged
    watchdog = coord._ServerWatchdog(servers, [0], interval_sec=0.05, probe_interval_sec=0.0)
    monkeypatch.setattr(watchdog, "_alive", lambda a: False)

    watchdog.start()
    time.sleep(0.5)
    watchdog.stop()
    watchdog.join(timeout=2)

    assert watchdog.restarts >= 1  # it keeps failing the probe, so it may restart more than once


def test_watchdog_leaves_a_responding_server_alone(monkeypatch: Any) -> None:
    """Run test_watchdog_leaves_a_responding_server_alone."""
    from loongforge.embodied.eval.orchestrator import coordinator as coord

    args = SimpleNamespace(host="127.0.0.1", port=14560, health_port=14561, server_start_timeout_sec=5)
    live = _FakeProc(None)
    watchdog = coord._ServerWatchdog([(live, args)], [0], interval_sec=0.05, probe_interval_sec=0.0)
    monkeypatch.setattr(watchdog, "_alive", lambda a: True)

    watchdog.start()
    time.sleep(0.3)
    watchdog.stop()
    watchdog.join(timeout=2)

    assert watchdog.restarts == 0


def test_alive_probe_fails_fast_and_restores_proxy_env(monkeypatch: Any) -> None:
    """A dead port fails the probe, and the probe puts the proxy env back."""
    from loongforge.embodied.eval.orchestrator import coordinator as coord

    monkeypatch.setattr(coord, "PROBE_CONNECT_TIMEOUT_SEC", 0.1)
    monkeypatch.setenv("HTTP_PROXY", "http://example.invalid:1")
    watchdog = coord._ServerWatchdog([], [], interval_sec=99)
    assert watchdog._alive(SimpleNamespace(host="127.0.0.1", port=9, health_port=9)) is False
    assert os.environ.get("HTTP_PROXY") == "http://example.invalid:1"


def _mini_config(output_dir: str, **run_extra: Any) -> dict:
    return {
        "benchmark": {"name": "libero", "suite": "libero_10", "task_ids": [0], "episodes_per_task": 1},
        "servers": [{"gpus": [0]}],
        "pools": {"cpu_sim": {"n_sims_per_server": 1}},
        "run": {"output_dir": output_dir, **run_extra},
    }


def test_run_refuses_to_clobber_a_finished_run(tmp_path: Any) -> None:
    """An existing coordinator_result.json means "already run" -- refuse unless resuming."""
    from loongforge.embodied.eval.orchestrator import coordinator as coord

    out = tmp_path / "out"
    out.mkdir()
    (out / "coordinator_result.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        coord.run(_mini_config(str(out)), "")
    assert "already exists" in str(excinfo.value)


def test_run_allows_a_finished_dir_when_resuming(monkeypatch: Any, tmp_path: Any) -> None:
    """Run test_run_allows_a_finished_dir_when_resuming."""
    from loongforge.embodied.eval.orchestrator import coordinator as coord

    out = tmp_path / "out"
    out.mkdir()
    (out / "coordinator_result.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(coord, "start_server", lambda args, env=None: _FakeProc(0))
    monkeypatch.setattr(coord, "wait_healthz", lambda *args, **kwargs: None)
    monkeypatch.setattr(coord, "_launch_unit", lambda *args, **kwargs: _FakeProc(0))
    monkeypatch.setattr(coord, "short_shards", lambda *args, **kwargs: [])
    result = coord.run(_mini_config(str(out), resume=True), "")
    assert result["n_units"] == 1


def test_policy_timeout_warning_fires_only_when_below_the_floor(capsys: Any) -> None:
    """The floor is N x service_ms x safety; warn below it, stay quiet above (or unknown)."""
    from loongforge.embodied.eval.orchestrator import coordinator as coord

    coord._warn_policy_timeout({"timeouts": {"service_ms": 500, "policy_call_ms": 5000}}, 16)
    assert "below the queue-wait floor" in capsys.readouterr().err

    coord._warn_policy_timeout({"timeouts": {"service_ms": 500, "policy_call_ms": 60000}}, 16)
    assert capsys.readouterr().err == ""

    coord._warn_policy_timeout({"timeouts": {"policy_call_ms": 5000}}, 16)  # no service_ms -> skipped
    assert capsys.readouterr().err == ""


def test_watchdog_gives_up_after_repeated_failed_restarts(monkeypatch: Any) -> None:
    """A server that cannot come back is not restarted forever -- it is given up on."""
    from loongforge.embodied.eval.orchestrator import coordinator as coord

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("cannot start")

    monkeypatch.setattr(coord, "start_server", lambda args, env=None: _FakeProc(0))
    monkeypatch.setattr(coord, "wait_healthz", boom)
    args = SimpleNamespace(host="127.0.0.1", port=14560, health_port=14561, server_start_timeout_sec=5)
    watchdog = coord._ServerWatchdog([(_FakeProc(1), args)], [0], interval_sec=0.05, probe_interval_sec=999)

    watchdog.start()
    time.sleep(0.6)
    watchdog.stop()
    watchdog.join(timeout=2)

    assert 0 in watchdog.given_up
    assert watchdog.restarts == 0  # nothing ever came back


def test_run_exits_nonzero_when_a_server_was_given_up(monkeypatch: Any, tmp_path: Any) -> None:
    """Run test_run_exits_nonzero_when_a_server_was_given_up."""
    import sys as _sys

    from loongforge.embodied.eval.orchestrator import coordinator as coord
    from loongforge.embodied.eval.orchestrator import run as run_mod

    cfg = tmp_path / "c.yaml"
    cfg.write_text("coordinator: {}\n", encoding="utf-8")
    monkeypatch.setattr(_sys, "argv", ["run", "--config", str(cfg)])
    monkeypatch.setattr(run_mod, "load_config", lambda path: {"coordinator": {}})
    monkeypatch.setattr(coord, "run", lambda config, path: {"validation": {"ok": True}, "servers_given_up": [0]})
    with pytest.raises(SystemExit) as excinfo:
        run_mod.main()
    assert excinfo.value.code == 2


def test_watchdog_terminates_a_hung_server_before_restarting(monkeypatch: Any) -> None:
    """A hung server still holds its port, so it must be terminated before the rebind."""
    from loongforge.embodied.eval.orchestrator import coordinator as coord

    monkeypatch.setattr(coord, "start_server", lambda args, env=None: _FakeProc(None))
    monkeypatch.setattr(coord, "wait_healthz", lambda *args, **kwargs: None)
    args = SimpleNamespace(host="127.0.0.1", port=14560, health_port=14561, server_start_timeout_sec=5)
    hung = _FakeProc(None)  # process alive, but wedged
    watchdog = coord._ServerWatchdog([(hung, args)], [0], interval_sec=0.05, probe_interval_sec=0.0)
    monkeypatch.setattr(watchdog, "_alive", lambda a: False)

    watchdog.start()
    time.sleep(0.5)
    watchdog.stop()
    watchdog.join(timeout=2)

    assert hung.terminated  # otherwise the new server cannot bind the port
    assert watchdog.restarts >= 1
