"""CPU-only tests for tools/env_report.py (task T-002); directory and cgroup trees are labelled doubles."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from unittest import mock

import pytest

SCRIPT = Path(os.environ.get("ENV_REPORT_SCRIPT", Path(__file__).resolve().parents[1] / "env_report.py")).resolve()
spec = importlib.util.spec_from_file_location("env_report", SCRIPT)
env = importlib.util.module_from_spec(spec)
spec.loader.exec_module(env)


def test_cgroup_double_nested_and_ancestors(tmp_path):
    mount = tmp_path / "v2"
    child = mount / "parent" / "child"
    child.mkdir(parents=True)
    for directory, value in ((mount, "root"), (child.parent, "parent"), (child, "child")):
        for name in ("cpu.max", "memory.max"):
            (directory / name).write_text(value)
    member = tmp_path / "cgroup"
    member.write_text("0::/parent/child\n")
    mounts = tmp_path / "mountinfo"
    mounts.write_text(f"1 2 0:1 / {mount} rw - cgroup2 cgroup rw\n")
    result = env.cgroup_inventory(member, mounts)
    assert f"{child / 'cpu.max'}: child" in result
    assert f"{child.parent / 'memory.max'}: parent" in result
    assert f"{mount / 'memory.max'}: root" in result
    assert "cpu: unknown: membership absent" in result


def test_cgroup_v1_and_unresolved_double(tmp_path):
    mount = tmp_path / "v1"
    child = mount / "job"
    child.mkdir(parents=True)
    for directory in (mount, child):
        (directory / "cpu.cfs_quota_us").write_text("100")
        (directory / "cpu.cfs_period_us").write_text("200")
    member = tmp_path / "cgroup"
    member.write_text("2:cpu,cpuacct:/job\n3:memory:/unmounted\n")
    mounts = tmp_path / "mountinfo"
    mounts.write_text(f"1 2 0:1 / {mount} rw - cgroup cgroup rw,cpu,cpuacct\n")
    result = env.cgroup_inventory(member, mounts)
    assert f"{child / 'cpu.cfs_quota_us'}: 100" in result
    assert "memory: unknown: no matching cgroup mount" in result


def test_model_scan_error_double(tmp_path):
    root = tmp_path / "models"
    bad = root / "denied"
    bad.mkdir(parents=True)
    (root / "ok.gguf").write_text("fixture")
    real = os.scandir
    def scan(path):
        if Path(path) == bad:
            raise PermissionError("fixture denied")
        return real(path)
    with mock.patch.object(env.os, "scandir", side_effect=scan):
        result = env.model_inventory(root)
    assert "ok.gguf" in result and "incomplete, unknown" in result
    assert "denied" in result and "fixture denied" in result


def test_missing_model_root_is_unknown(tmp_path):
    root = tmp_path / "nonexistent_models"
    result = env.model_inventory(root)
    assert "incomplete, unknown" in result and str(root) in result


def test_models_root_required_and_override(tmp_path):
    args = [sys.executable, "-B", str(SCRIPT), "--data-root", str(tmp_path), "--scratch", str(tmp_path), "--timeout-seconds", "5"]
    missing = subprocess.run(args, capture_output=True, text=True, timeout=15)
    assert missing.returncode != 0 and "--models-root" in missing.stderr
    root = tmp_path / "custom-models"
    root.mkdir()
    (root / "fixture.gguf").write_text("fixture")
    result = subprocess.run(args + ["--models-root", str(root)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0 and "fixture.gguf" in result.stdout


def test_model_junctions_skipped(tmp_path):
    if sys.platform != "win32":
        pytest.skip("Windows junction creation is unavailable on this platform")
    import _winapi
    root = tmp_path / "models"
    root.mkdir()
    outside = tmp_path / "outside-models"
    outside.mkdir()
    (outside / "outside-fixture.gguf").write_text("fixture")
    junction = root / "junction"
    cycle = root / "cycle"
    root_link = tmp_path / "root-junction"
    created = []
    try:
        for target, link in ((outside, junction), (root, cycle), (outside, root_link)):
            try:
                _winapi.CreateJunction(str(target), str(link))
            except OSError as exc:
                pytest.skip(f"Windows junction creation unavailable: {exc}")
            created.append(link)
        result = env.model_inventory(root)
        assert "outside-fixture.gguf" not in result
        assert "skipped links or junctions" in result
        assert str(junction) in result and str(cycle) in result
        assert "skipped link or junction at model root" in env.model_inventory(root_link)
    finally:
        for link in reversed(created):
            link.rmdir()


def test_timeout_override_and_refusals():
    for value in (None, "0", "-1", "nan"):
        args = [sys.executable, "-B", str(SCRIPT), "--data-root", ".", "--scratch", ".", "--models-root", "."]
        if value is not None:
            args += ["--timeout-seconds", value]
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
        assert result.returncode != 0 and "timeout" in result.stderr
    with mock.patch.object(env.subprocess, "run") as run:
        run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        env.run(["fixture"], 3.5)
        assert run.call_args.kwargs["timeout"] == 3.5
