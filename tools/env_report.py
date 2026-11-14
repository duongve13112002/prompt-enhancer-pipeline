"""Read-only machine inventory; no network, model loading, or installation."""

import argparse
import ctypes
import importlib.metadata
import math
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys


def section(name, command, value):
    print(f"\n## {name}\nCommand: {command}\n```\n{value}\n```")


def run(command, timeout):
    try:
        p = subprocess.run(command, capture_output=True, text=True, timeout=timeout, errors="replace")
        return f"exit={p.returncode}\n{p.stdout}{p.stderr}".strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unknown: {exc}"


def nearest(path):
    path = path.resolve()
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def positive_seconds(value):
    try:
        number = float(value)
        if math.isfinite(number) and number > 0:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("timeout must be a positive finite number of seconds")


def is_junction(path):
    if hasattr(path, "is_junction"):
        return path.is_junction()
    try:
        return bool(getattr(path.lstat(), "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    except FileNotFoundError:
        return False


def model_inventory(root):
    files, errors, skipped = [], [], []
    try:
        if root.is_symlink() or is_junction(root):
            return f"incomplete, unknown: {root}: skipped link or junction at model root"
    except OSError as exc:
        return f"incomplete, unknown: {root}: {exc}"
    def walk(path):
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink() or is_junction(Path(entry.path)):
                            skipped.append(entry.path)
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            walk(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            files.append(entry.path)
                    except OSError as exc:
                        errors.append(f"{entry.path}: {exc}")
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    walk(root)
    result = "\n".join(sorted(files)) if files else "no files found"
    result += ("\nincomplete, unknown: " + "; ".join(errors) if errors else "\ncomplete")
    if skipped:
        result += "\nskipped links or junctions:\n" + "\n".join(sorted(skipped))
    return result


def cgroup_inventory(membership, mountinfo):
    # Assumption: proc mountinfo root and mountpoint use Linux mountinfo octal escapes.
    def unescape(value):
        import re
        return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), value)
    try:
        groups = [line.split(":", 2) for line in membership.read_text().splitlines()]
        mounts = [line.split(" ") for line in mountinfo.read_text().splitlines()]
        results = []
        for controller, names in (("unified", ("cpu.max", "memory.max")), ("cpu", ("cpu.cfs_quota_us", "cpu.cfs_period_us")), ("memory", ("memory.limit_in_bytes",))):
            group = next((g[2] for g in groups if len(g) == 3 and (g[1] == "" if controller == "unified" else controller in g[1].split(","))), None)
            if group is None:
                results.append(f"{controller}: unknown: membership absent")
                continue
            if ".." in Path(group).parts:
                results.append(f"{controller}: unknown: unsafe membership path {group}")
                continue
            candidates = []
            for fields in mounts:
                sep = fields.index("-")
                kind = fields[sep + 1]
                opts = fields[sep + 3].split(",")
                if (controller == "unified" and kind == "cgroup2") or (kind == "cgroup" and controller in opts):
                    root = Path(unescape(fields[3]))
                    try:
                        relative = Path(group).relative_to(root)
                    except ValueError:
                        continue
                    candidates.append((len(root.parts), root, Path(unescape(fields[4])), relative))
            if not candidates:
                results.append(f"{controller}: unknown: no matching cgroup mount for {group}")
                continue
            _, root, mount, relative = max(candidates, key=lambda x: x[0])
            current = mount / relative
            while True:
                for name in names:
                    path = current / name
                    try:
                        results.append(f"{path}: {path.read_text().strip()}")
                    except OSError as exc:
                        results.append(f"{path}: unknown: {exc}")
                if current == mount:
                    break
                current = current.parent
            if str(root) != "/":
                results.append(f"{controller}: unknown: ancestors above mounted root {root} are not visible")
        return "\n".join(results)
    except (OSError, ValueError, IndexError) as exc:
        return f"unknown: cannot resolve cgroup membership and mounts: {exc}"


def windows_memory():
    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
            (name, ctypes.c_ulonglong) for name in (
                "total_physical", "available_physical", "total_pagefile",
                "available_pagefile", "total_virtual", "available_virtual", "available_extended_virtual"
            )
        ]
    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return f"unknown: Windows error {ctypes.get_last_error()}"
    return f"total physical bytes: {status.total_physical}"


def windows_filesystem(path):
    filesystem = ctypes.create_unicode_buffer(256)
    root = path.anchor
    ok = ctypes.windll.kernel32.GetVolumeInformationW(root, None, 0, None, None, None, filesystem, len(filesystem))
    return filesystem.value if ok else f"unknown: Windows error {ctypes.get_last_error()}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--scratch", required=True)
    parser.add_argument("--models-root", required=True, help="model directory to inventory")
    parser.add_argument("--timeout-seconds", required=True, type=positive_seconds, help="positive finite timeout for each external command")
    parser.add_argument("--gpu", action="store_true", help="Run two read-only nvidia-smi queries if authorized")
    args = parser.parse_args()
    section("Query timeout", "--timeout-seconds", str(args.timeout_seconds))
    section("OS and Python", "platform.platform(); sys.version", f"{platform.platform()}\n{sys.version}")
    section("CPU logical count", "os.cpu_count()", str(os.cpu_count()))
    if sys.platform == "win32":
        section("RAM", "Windows GlobalMemoryStatusEx", windows_memory())
        ps = shutil.which("powershell") or shutil.which("pwsh")
        query = "Get-CimInstance Win32_ComputerSystem | Select-Object TotalPhysicalMemory; Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors"
        section("RAM and physical CPU", f"{ps} -NoProfile -Command {query}", run([ps, "-NoProfile", "-Command", query], args.timeout_seconds) if ps else "unknown: PowerShell absent")
    elif sys.platform.startswith("linux"):
        section("RAM", "read /proc/meminfo (MemTotal)", next((x for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemTotal:')), 'unknown'))
        section("Physical CPU", "lscpu", run(["lscpu"], args.timeout_seconds))
    else:
        section("RAM and physical CPU", "platform-specific", "unknown: unsupported platform")
    for label, raw in (("data", args.data_root), ("scratch", args.scratch)):
        path = nearest(Path(raw))
        try:
            usage = shutil.disk_usage(path)
            section(f"{label} disk", f"shutil.disk_usage({str(path)!r}); requested {raw!r}", f"existing ancestor: {path}\nfree bytes: {usage.free}")
        except OSError as exc:
            section(f"{label} disk", "shutil.disk_usage", f"unknown: {exc}")
        if sys.platform == "win32":
            section(f"{label} filesystem", f"Windows GetVolumeInformationW({path.anchor!r})", windows_filesystem(path))
        else:
            section(f"{label} filesystem", f"df -T {path}", run(["df", "-T", str(path)], args.timeout_seconds))
    for package in ("pip", "torch", "transformers"):
        try:
            version = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            version = "absent in this Python environment"
        section(package, f"importlib.metadata.version({package!r})", version)
    section("pip connectivity", "not_run", "unknown: online and wheelhouse installation were not tested; network and installs prohibited")
    binary = shutil.which("llama-server")
    section("llama-server", "shutil.which('llama-server'); llama-server --version", run([binary, "--version"], args.timeout_seconds) if binary else "absent on PATH; version unknown")
    models = Path(args.models_root)
    section("Model files", f"read-only recursive file listing under {models}; links skipped", model_inventory(models))
    if args.gpu:
        query = ["nvidia-smi", "--query-gpu=name,driver_version,memory.total,memory.used", "--format=csv"]
        section("GPU inventory", " ".join(query), run(query, args.timeout_seconds))
        section("GPU, driver and CUDA", "nvidia-smi", run(["nvidia-smi"], args.timeout_seconds))
    else:
        section("GPU, driver and CUDA", "not_run (use --gpu only with authorization)", "unknown: GPU query not requested")
    if sys.platform.startswith("linux"):
        section("Process cgroup limits", "resolve /proc/self/cgroup against /proc/self/mountinfo; read group and ancestor limit files", cgroup_inventory(Path('/proc/self/cgroup'), Path('/proc/self/mountinfo')))
        section("CPU affinity", "os.sched_getaffinity(0)", str(sorted(os.sched_getaffinity(0))) if hasattr(os, "sched_getaffinity") else "unknown")
    else:
        section("Process CPU and memory limits", "cgroup files", "unknown: cgroups are Linux-specific; Windows job-object limits not inspected")
    section("Benchmarks and model inference", "not_run", "not_run: outside scope and not authorized")


if __name__ == "__main__":
    main()
