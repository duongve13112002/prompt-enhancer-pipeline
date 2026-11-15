"""Check and repair how the agent tooling is bound to this repository and this machine.

Usage:
  python -B tools/agent-workflow/setup_project.py check [--json] [--no-cli]
  python -B tools/agent-workflow/setup_project.py apply [--force-settings]

`check` is read-only. It verifies the binding file `.agents/project.json`, the interpreter and `.venv`, the shared
instruction files (AGENTS.md, CLAUDE.md importing it), the skills (`.agents/skills` canonical, `.claude/skills` synced
copy), the enabled agent CLIs, the rendered Claude hook settings, the tool configs that copy a binding value, the files
the hooks read, the Windows agy runner binding (this project's task, its -ProjectRoot, the job folder, the work folder,
the Allow rule on `.tmp/agy-work`; one WARN per missing item, never FAIL), the Git-ignore entries, tokens of a previous project, and dead relative links in live Markdown.
Exit 0: no FAIL. Exit 1: at least one FAIL. WARN lines are machine-specific or optional (a missing CLI, the Windows
agy runner scripts). Nothing is sent over the network; `.secrets/` is never read.

`apply` makes the idempotent repairs `check` can name: create `.tmp/pm-state`, copy `.agents/skills` into
`.claude/skills`, write the integration branch into `memory_hooks.json`, and render `.claude/settings.local.json` from
`settings.example.json` for this checkout (an existing file is replaced only with `--force-settings`). It never edits
AGENTS.md, policy files, `.secrets/` or anything outside the project root. Run `check` afterwards.
"""

from __future__ import annotations

import argparse
import filecmp
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import project_config  # noqa: E402
import settings_drift  # noqa: E402

ROOT = HERE.parents[1]
SKILLS_SRC = ".agents/skills"
SKILLS_CLAUDE = ".claude/skills"
SETTINGS_LOCAL = ".claude/settings.local.json"
SETTINGS_TEMPLATE = "tools/agent-workflow/settings.example.json"
MEMORY_HOOKS = "tools/agent-workflow/memory_hooks.json"
# Files the hooks and tools read by fixed relative path (protocol-bound: moving one needs a code change).
TOOL_READ_FILES = (
    "AGENTS.md",
    "tasks/STATUS.md",
    "tasks/DECISIONS_INDEX.md",
    "tasks/LESSONS_LEARNED.md",
    "tasks/REQUIREMENTS.md",
    ".agents/ORCHESTRATION.md",
    ".agents/policy/AGENT_BRIEF_FOOTER.md",
    ".agents/policy/USER_STANDING_RULES.md",
    ".agents/policy/AGENT_PEER_RULES.md",
    "docs/note/README.md",
)
# `.agents/registry.json` of the earlier rules is gone (decision 48): worker state lives under `.tmp/pm-state/`.
GITIGNORE_ENTRIES = (".tmp/", ".venv/", ".secrets/*", ".agents/log.md", SETTINGS_LOCAL)
MARKERS = ("<!-- ORCHESTRATION:START -->", "<!-- ORCHESTRATION:END -->", "<!-- AGENT-WORKER:START -->",
           "<!-- AGENT-WORKER:END -->")
TEXT_SUFFIXES = {".md", ".py", ".json", ".toml", ".yaml", ".yml", ".ps1", ".txt", ".cfg", ".ini"}
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
PLACEHOLDER_RE = re.compile(r"<(PROJECT_ROOT|PERMISSION_ROOT|VENV_PYTHON)>")
CLI_TIMEOUT_SECONDS = 20  # protocol-bound: a `--version` call that takes longer is reported, not waited on


@dataclass
class Result:
    level: str  # OK, WARN, FAIL
    area: str
    message: str
    fix: str = ""


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def venv_python(root: Path, venv_dir: str) -> Path:
    base = root / venv_dir
    return base / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def permission_root(root: Path) -> str:
    """Absolute path in Claude Code permission-rule form: `//d/x` on Windows, `//home/x` on POSIX."""
    text = root.as_posix()
    match = re.match(r"^([A-Za-z]):/(.*)$", text)
    if match:
        return f"//{match.group(1).lower()}/{match.group(2)}"
    return "/" + text


# ---- checks -------------------------------------------------------------------------------------------------------


def check_binding(root: Path) -> tuple[list[Result], project_config.ProjectConfig | None]:
    try:
        cfg = project_config.load(root)
    except project_config.ProjectConfigError as exc:
        return [Result("FAIL", "binding", str(exc), "fix .agents/project.json (schema: project_config.py)")], None
    return [Result("OK", "binding", f"{cfg.name}; agents {', '.join(cfg.enabled_agents) or 'none'}")], cfg


def check_python(root: Path, cfg: project_config.ProjectConfig) -> list[Result]:
    out: list[Result] = []
    have = sys.version_info[:2]
    level = "OK" if have >= cfg.min_python else "FAIL"
    out.append(Result(level, "python", f"interpreter {have[0]}.{have[1]} (min {cfg.min_python[0]}.{cfg.min_python[1]})",
                      "" if level == "OK" else "use a newer Python for .venv"))
    py = venv_python(root, cfg.venv_dir)
    if py.exists():
        out.append(Result("OK", "python", f"venv interpreter {_rel(py, root)}"))
    else:
        out.append(Result("WARN", "python", f"no venv interpreter at {_rel(py, root)}",
                          f"python -m venv {cfg.venv_dir} && {cfg.venv_dir}/... -m pip install pytest hypothesis"))
    if cfg.package:
        pkg = root / cfg.src_dir / cfg.package.replace(".", "/")
        out.append(Result("OK" if pkg.is_dir() else "FAIL", "python", f"package {cfg.package} at {_rel(pkg, root)}",
                          "" if pkg.is_dir() else "create the package or fix python.package"))
    return out


def check_instructions(root: Path) -> list[Result]:
    out: list[Result] = []
    agents = root / "AGENTS.md"
    if not agents.is_file():
        return [Result("FAIL", "instructions", "AGENTS.md missing", "restore AGENTS.md (single rule source)")]
    text = agents.read_text(encoding="utf-8")
    missing = [m for m in MARKERS if text.count(m) != 1]
    out.append(Result("FAIL" if missing else "OK", "instructions",
                      f"AGENTS.md marker blocks {'broken: ' + ', '.join(missing) if missing else 'present once each'}",
                      "restore the ORCHESTRATION and AGENT-WORKER blocks" if missing else ""))
    claude = root / "CLAUDE.md"
    first = ""
    if claude.is_file():
        first = next((ln.strip() for ln in claude.read_text(encoding="utf-8").splitlines() if ln.strip()), "")
    ok = first == "@AGENTS.md"
    out.append(Result("OK" if ok else "FAIL", "instructions", "CLAUDE.md imports AGENTS.md" if ok else
                      "CLAUDE.md does not start with '@AGENTS.md'", "" if ok else "make '@AGENTS.md' its first line"))
    if (root / "GEMINI.md").exists():
        out.append(Result("WARN", "instructions", "GEMINI.md exists; agy would read it besides AGENTS.md",
                          "move its rules into AGENTS.md and delete it"))
    return out


def _skill_names(root: Path) -> list[str]:
    base = root / SKILLS_SRC
    return sorted(p.name for p in base.iterdir() if (p / "SKILL.md").is_file()) if base.is_dir() else []


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    fields: dict[str, str] = {}
    for line in text[4:end].splitlines() if end > 0 else []:
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


def _tree_differs(src: Path, dst: Path) -> list[str]:
    if not dst.is_dir():
        return ["missing"]
    cmp = filecmp.dircmp(src, dst)
    diffs: list[str] = []
    stack = [(cmp, "")]
    while stack:
        node, prefix = stack.pop()
        diffs += [f"only canonical: {prefix}{n}" for n in node.left_only]
        diffs += [f"only copy: {prefix}{n}" for n in node.right_only]
        _, mismatch, errors = filecmp.cmpfiles(node.left, node.right, node.common_files, shallow=False)
        diffs += [f"differs: {prefix}{n}" for n in mismatch + errors]
        stack += [(sub, f"{prefix}{name}/") for name, sub in node.subdirs.items()]
    return diffs


def check_skills(root: Path, cfg: project_config.ProjectConfig) -> list[Result]:
    names = _skill_names(root)
    if not names:
        return [Result("WARN", "skills", f"no skills under {SKILLS_SRC}")]
    out: list[Result] = []
    for name in names:
        meta = _frontmatter((root / SKILLS_SRC / name / "SKILL.md").read_text(encoding="utf-8"))
        if meta.get("name") != name or not meta.get("description"):
            out.append(Result("FAIL", "skills", f"{name}: frontmatter needs name: {name} and a description"))
    if cfg.agents["claude"].enabled:
        stale = {n: _tree_differs(root / SKILLS_SRC / n, root / SKILLS_CLAUDE / n) for n in names}
        stale = {n: d for n, d in stale.items() if d}
        extra = sorted(p.name for p in (root / SKILLS_CLAUDE).iterdir() if p.is_dir() and p.name not in names) \
            if (root / SKILLS_CLAUDE).is_dir() else []
        if stale:
            detail = "; ".join(f"{n} ({', '.join(d[:2])})" for n, d in stale.items())
            out.append(Result("FAIL", "skills", f"{SKILLS_CLAUDE} out of sync: {detail}", "setup_project.py apply"))
        if extra:
            out.append(Result("WARN", "skills", f"{SKILLS_CLAUDE} has skills with no canonical copy: {extra}",
                              f"move them to {SKILLS_SRC} or delete them"))
    if not out:
        out.append(Result("OK", "skills", f"{len(names)} skills: {', '.join(names)}"))
    return out


def _cli_version(cli: str) -> tuple[str, str]:
    path = shutil.which(cli)
    if path is None:
        return "missing", ""
    try:
        proc = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=CLI_TIMEOUT_SECONDS,
                              check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "error", type(exc).__name__
    first = (proc.stdout or proc.stderr).strip().splitlines()
    return ("ok" if proc.returncode == 0 else "error"), (first[0][:80] if first else f"exit {proc.returncode}")


def check_clis(root: Path, cfg: project_config.ProjectConfig, run_cli: bool) -> list[Result]:
    out: list[Result] = []
    for agent in cfg.agents.values():
        if not agent.enabled:
            continue
        if not run_cli:
            found = shutil.which(agent.cli)
            out.append(Result("OK" if found else "WARN", "cli", f"{agent.name}: {agent.cli} "
                              f"{'on PATH' if found else 'not on PATH'} (version not checked)"))
            continue
        state, detail = _cli_version(agent.cli)
        level = "OK" if state == "ok" else "WARN"
        out.append(Result(level, "cli", f"{agent.name}: {agent.cli} {state} {detail}".rstrip(),
                          "" if level == "OK" else f"install {agent.cli} on this machine or disable it in project.json"))
    if cfg.agents["codex"].enabled:
        profiles = json.loads((HERE / "codex_profiles.json").read_text(encoding="utf-8"))
        holes = [r for r, v in profiles.get("roles", {}).items() if str(v.get("model", "")).startswith("<")]
        if holes:
            out.append(Result("WARN", "cli", f"codex_profiles.json roles without a model: {holes}",
                              "set models per .agents/policy/PM_POLICY.md section 2"))
        local = sorted((root / ".tmp" / "pm-state").glob("codex.local*.json"))
        bound = []
        for path in local:
            try:
                command = json.loads(path.read_text(encoding="utf-8"))["runtime"]["codex_command"]
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if isinstance(command, str) and not command.startswith("<") and Path(command).is_file():
                bound.append(path.name)
        if bound:
            out.append(Result("OK", "cli", f"local Codex config with a real codex_command: {bound[0]}"))
        else:
            out.append(Result("WARN", "cli", "no .tmp/pm-state/codex.local*.json with an existing codex_command",
                              "copy codex_profiles.json there and set runtime.codex_command (CODEX_JOBS.md)"))
    shell = shutil.which("pwsh") or shutil.which("powershell")
    out.append(Result("OK" if shell else "WARN", "cli",
                      f"PowerShell {'found: ' + Path(shell).name if shell else 'not on PATH'} (pm-supervisor.ps1, gemini-*.ps1)",
                      "" if shell else "install PowerShell (pwsh) to run the PM supervisor heartbeat"))
    agy = cfg.agents["antigravity"]
    if agy.enabled and agy.runner_scripts_dir:
        lib = root / agy.runner_scripts_dir / "agy-lib.ps1"
        out.append(Result("OK" if lib.is_file() else "WARN", "cli",
                          f"agy runner scripts {'present' if lib.is_file() else 'missing'} at {agy.runner_scripts_dir}",
                          "" if lib.is_file() else "copy the Windows runner scripts there (needed by gemini-*.ps1)"))
    return out


# ---- agy runner binding (S42) ---------------------------------------------------------------------------------------
# Single source (fix round 1, F2): the task name, the action parse, the path comparison and the rule decision live in
# scripts/agy-runner/agy-lib.ps1. This row asks them through one read-only PowerShell call and only presents the result.
AGY_LIB = ROOT / "scripts" / "agy-runner" / "agy-lib.ps1"
# Read-only. Every read stops on failure ($ErrorActionPreference Stop and the lib's fail-closed helpers), so a failed
# read becomes a WARN with its error and never looks like "no task" or "no folder". AGY_CHECK_MODE=name: the name only.
RUNNER_READ_PS = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding $false
. $env:AGY_CHECK_LIB
$root = $env:AGY_CHECK_ROOT
$out = [ordered]@{ task_name = (Get-AgyRunnerTaskName -ProjectRoot $root) }
if ($env:AGY_CHECK_MODE -ne 'name') {
  $all = @(Get-AgyScheduledTaskAll)
  $own = Get-AgyNamedTask -TaskName $out.task_name -Tasks $all
  $tasks = @(Get-AgyRunnerTask -Tasks $all)
  if ($own -and -not @($tasks | Where-Object { $_.TaskName -eq $own.TaskName -and $_.TaskPath -eq $own.TaskPath })) { $tasks += $own }
  $out.tasks = @(foreach ($t in $tasks) {
    $info = Get-ScheduledTaskInfo -TaskName $t.TaskName -TaskPath $t.TaskPath
    $last = 'never'
    if (($info.LastRunTime -is [datetime]) -and $info.LastRunTime.Year -ge 2000) { $last = $info.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss') }
    $bound = Get-AgyTaskBoundRoot $t
    $here = $null
    $err = ''
    if ($bound) { try { $here = Test-AgySamePath $bound $root } catch { $err = $_.Exception.Message } }
    [ordered]@{ name = $t.TaskName; own = [bool]($own -and $t.TaskName -eq $own.TaskName -and $t.TaskPath -eq $own.TaskPath)
      state = [string]$t.State; user = [string]$t.Principal.UserId; last_run = $last; project_root = $bound
      bound_here = $here; bound_error = $err }
  })
  $out.jobs_exists = Test-AgyPathExists -Path (Join-Path $root '.tmp\agy-jobs') -PathType Container
  $work = Join-Path $root '.tmp\agy-work'
  $out.work_exists = Test-AgyPathExists -Path $work -PathType Container
  # The account comes from the tasks themselves (setup.ps1 -UserName), never from a literal here.
  $users = @(@(if ($own) { $own } else { $tasks }) | ForEach-Object { [string]$_.Principal.UserId } | Where-Object { $_ } | Sort-Object -Unique)
  $out.work_rules = @()
  $out.acl_error = ''
  if ($out.work_exists -and $users.Count -gt 0) {
    try {
      $rules = @((Get-Acl -LiteralPath $work -ErrorAction Stop).Access)
      $out.work_rules = @(foreach ($u in $users) {
        [ordered]@{ account = $u; decision = (Get-AgyAceDecision -Rules $rules -UserName $u -Mode grant -Rights Modify -Inherit $true) } })
    } catch { $out.acl_error = $_.Exception.Message }
  }
}
ConvertTo-Json -InputObject $out -Depth 5 -Compress
"""


def _powershell_json(root: Path | str, mode: str) -> dict[str, object]:
    """One read-only PowerShell child that dot-sources agy-lib.ps1; raises OSError, ValueError or SubprocessError."""
    shell = shutil.which("powershell") or shutil.which("pwsh")
    if shell is None:
        raise OSError("PowerShell not on PATH")
    # Paths travel in environment variables, so a non-ASCII root needs no quoting; the reply is UTF-8 JSON.
    env = {**os.environ, "AGY_CHECK_LIB": str(AGY_LIB), "AGY_CHECK_ROOT": str(root), "AGY_CHECK_MODE": mode}
    proc = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", RUNNER_READ_PS],
                          capture_output=True, encoding="utf-8", errors="replace", timeout=CLI_TIMEOUT_SECONDS,
                          check=False, stdin=subprocess.DEVNULL, env=env,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if proc.returncode != 0:
        lines = [line.strip() for line in (proc.stderr or proc.stdout or "").splitlines() if line.strip()]
        raise OSError(lines[0] if lines else f"PowerShell exit {proc.returncode}")
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    if not isinstance(data, dict):
        raise ValueError("the runner state is not a JSON object")
    return data


def runner_task_name(root: Path | str) -> str:
    """This project's runner task name, from Get-AgyRunnerTaskName in agy-lib.ps1 (the single source)."""
    name = _powershell_json(root, "name").get("task_name")
    if not isinstance(name, str) or not name:
        raise ValueError("agy-lib.ps1 returned no task name")
    return name


def _read_runner_state(root: Path) -> dict[str, object]:
    return _powershell_json(root, "full")


def check_runner(root: Path, cfg: project_config.ProjectConfig,
                 read: Callable[[Path], dict[str, object]] | None = None,
                 is_windows: bool = os.name == "nt") -> list[Result]:
    """Read-only runner rows, one per required item, so each absent item gets its own WARN (fix round 1, F5).

    WARN, never FAIL: read-only Gemini reviews do not need the runner.
    """
    read = read or _read_runner_state  # looked up at call time so tests can replace the reader
    if not cfg.agents["antigravity"].enabled:
        return []
    if not is_windows:
        return [Result("OK", "runner", "agy runner: not applicable (Windows only)")]
    fix = ("the user runs scripts/agy-runner/setup.ps1 as administrator for this project (scripts/agy-runner/README.md, "
           "'Set up the runner for this project'); read-only reviews need no runner")
    try:
        state = read(root)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return [Result("WARN", "runner", f"agy runner: could not read the runner state ({exc})", "")]
    name = state.get("task_name")
    if not isinstance(name, str) or not name:
        return [Result("WARN", "runner", "agy runner: could not read the runner state (agy-lib.ps1 gave no task name)", "")]
    tasks = [t for t in state.get("tasks") or [] if isinstance(t, dict)]
    own = next((t for t in tasks if t.get("own") is True), None)

    def bound(task: dict[str, object]) -> str:
        return str(task.get("project_root") or "<not the action layout setup.ps1 writes>")

    out: list[Result] = []
    # 1. This project's task and its binding.
    if own is None:
        elsewhere = "; ".join(f"{t.get('name')} is bound to {bound(t)}" for t in tasks) or "no other runner task"
        out.append(Result("WARN", "runner", f"agy runner: task {name} for this project is missing ({elsewhere})", fix))
    elif not own.get("project_root"):
        out.append(Result("WARN", "runner", f"agy runner: task {name} does not have the action layout setup.ps1 "
                          "writes, so its project is unknown", fix))
    elif own.get("bound_error"):
        out.append(Result("WARN", "runner", f"agy runner: task {name} is bound to {bound(own)}, which could not be "
                          f"resolved ({own['bound_error']})", fix))
    elif own.get("bound_here") is not True:
        out.append(Result("WARN", "runner", f"agy runner: task {name} is bound to {bound(own)}, not to this root", fix))
    else:
        out.append(Result("OK", "runner", f"agy runner: task {name} bound to this root, state {own.get('state')}, "
                          f"last run {own.get('last_run')}"))
    # 2. The job folder.
    if state.get("jobs_exists") is True:
        out.append(Result("OK", "runner", "agy runner: .tmp/agy-jobs exists"))
    else:
        out.append(Result("WARN", "runner", "agy runner: .tmp/agy-jobs missing", fix))
    # 3. The work folder, then the Allow rule on it.
    rules = [r for r in state.get("work_rules") or [] if isinstance(r, dict)]
    if state.get("work_exists") is not True:
        out.append(Result("WARN", "runner", "agy runner: .tmp/agy-work missing (the runner's only writable folder)", fix))
    elif state.get("acl_error"):
        out.append(Result("WARN", "runner", f"agy runner: ACL of .tmp/agy-work unreadable: {state['acl_error']}", ""))
    elif not rules:
        out.append(Result("WARN", "runner", "agy runner: Allow Modify on .tmp/agy-work not checked: no runner task "
                          "names the account", fix))
    for rule in rules if state.get("work_exists") is True and not state.get("acl_error") else []:
        who, decision = rule.get("account"), str(rule.get("decision", ""))
        if decision == "present":
            out.append(Result("OK", "runner", f"agy runner: explicit Allow Modify for {who} on .tmp/agy-work, applying "
                              "to the folder and its children"))
        elif decision.startswith("blocked:"):
            out.append(Result("WARN", "runner", f"agy runner: .tmp/agy-work carries an {decision[len('blocked: '):]}; "
                              "the runner needs Allow Modify there",
                              "the user decides how to clear it; setup.ps1 stops on it and never removes a rule"))
        else:
            out.append(Result("WARN", "runner", f"agy runner: no explicit Allow Modify for {who} on .tmp/agy-work that "
                              "applies to the folder and its children", fix))
    return out


def check_claude_settings(root: Path, cfg: project_config.ProjectConfig) -> list[Result]:
    if not cfg.agents["claude"].enabled:
        return []
    path = root / SETTINGS_LOCAL
    if not path.is_file():
        return [Result("WARN", "settings", f"{SETTINGS_LOCAL} missing: hooks are not registered", "setup_project.py apply")]
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, ValueError) as exc:
        return [Result("FAIL", "settings", f"{SETTINGS_LOCAL} unreadable: {exc}", "setup_project.py apply --force-settings")]
    out: list[Result] = []
    if PLACEHOLDER_RE.search(text):
        out.append(Result("FAIL", "settings", f"{SETTINGS_LOCAL} still has placeholders", "setup_project.py apply --force-settings"))
    scripts = re.findall(r'"([^"]+?/tools/agent-workflow/[^"]+?\.py)"', text)
    want = (root / "tools" / "agent-workflow").as_posix()
    foreign = sorted({s for s in scripts if not s.startswith(want)})
    if foreign:
        out.append(Result("FAIL", "settings", f"hooks point at another checkout: {foreign[0]}",
                          "setup_project.py apply --force-settings"))
    missing = sorted({s for s in scripts if s.startswith(want) and not Path(s).is_file()})
    if missing:
        out.append(Result("FAIL", "settings", f"hook scripts missing: {missing}", "restore them or re-render"))
    example = json.loads((root / SETTINGS_TEMPLATE).read_text(encoding="utf-8"))
    drift = settings_drift.compare_hooks(example, data)
    if drift:
        out.append(Result("FAIL", "settings", f"hooks differ from the template: {drift[0]}",
                          "setup_project.py apply --force-settings"))
    if not out:
        out.append(Result("OK", "settings", f"{SETTINGS_LOCAL} bound to this checkout, hooks match the template"))
    return out


def check_tool_configs(root: Path, cfg: project_config.ProjectConfig) -> list[Result]:
    out: list[Result] = []
    memory = json.loads((root / MEMORY_HOOKS).read_text(encoding="utf-8"))
    branch_ok = memory.get("batch_branch") == cfg.integration_branch
    out.append(Result("OK" if branch_ok else "FAIL", "configs",
                      f"memory_hooks.json batch_branch {memory.get('batch_branch')!r} vs git.integration_branch "
                      f"{cfg.integration_branch!r}", "" if branch_ok else "setup_project.py apply"))
    budgets = json.loads((HERE / "doc_budgets.json").read_text(encoding="utf-8"))["project"]
    missing = [p for p in budgets if not (root / p).is_file()]
    over = [f"{p} {len((root / p).read_text(encoding='utf-8'))}>{n}" for p, n in budgets.items()
            if (root / p).is_file() and len((root / p).read_text(encoding="utf-8")) > n]
    out.append(Result("FAIL" if missing else "OK", "configs",
                      f"doc_budgets.json: {'missing ' + ', '.join(missing) if missing else 'every budgeted file exists'}",
                      "fix the paths in doc_budgets.json" if missing else ""))
    if over:
        out.append(Result("WARN", "configs", f"over character budget: {', '.join(over)}", "condense or archive verbatim"))
    guard = json.loads((HERE / "guard_config.json").read_text(encoding="utf-8"))["question_guard"]
    if not (root / guard["rules_file"]).is_file():
        out.append(Result("FAIL", "configs", f"guard_config.json rules_file {guard['rules_file']} missing"))
    absent = [p for p in TOOL_READ_FILES if not (root / p).is_file()]
    out.append(Result("FAIL" if absent else "OK", "configs",
                      f"files read by hooks and tools: {'missing ' + ', '.join(absent) if absent else 'all present'}"))
    return out


def check_state(root: Path) -> list[Result]:
    out: list[Result] = []
    state = root / ".tmp" / "pm-state"
    out.append(Result("OK" if state.is_dir() else "WARN", "state", f".tmp/pm-state {'exists' if state.is_dir() else 'missing'}",
                      "" if state.is_dir() else "setup_project.py apply"))
    ignore = (root / ".gitignore").read_text(encoding="utf-8").splitlines() if (root / ".gitignore").is_file() else []
    lacking = [e for e in GITIGNORE_ENTRIES if e not in ignore]
    out.append(Result("FAIL" if lacking else "OK", "state",
                      f".gitignore {'lacks ' + ', '.join(lacking) if lacking else 'covers local state and secrets'}",
                      "add the entries to .gitignore" if lacking else ""))
    return out


def tracked_text_files(root: Path) -> list[Path]:
    """Tracked files when Git is available, else every file outside ignored local trees; text suffixes only."""
    files: list[Path] = []
    try:
        proc = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                              capture_output=True, timeout=30, check=False)
        if proc.returncode == 0:
            files = [root / p for p in proc.stdout.decode("utf-8").split("\0") if p]
    except (OSError, subprocess.TimeoutExpired):
        files = []
    if not files:
        skip = {".git", ".venv", ".tmp", ".cache", ".secrets", "models", "runs", "node_modules", "__pycache__"}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".venv")]
            files += [Path(dirpath) / f for f in filenames]
    return [f for f in files if f.suffix.lower() in TEXT_SUFFIXES and f.is_file()
            and ".secrets" not in f.relative_to(root).parts]


def check_foreign(root: Path, cfg: project_config.ProjectConfig, files: list[Path]) -> list[Result]:
    if not cfg.foreign_bindings:
        return [Result("OK", "foreign", "no previous-project tokens configured")]
    hits: list[str] = []
    # This checkout's own absolute path is a binding of this project, not of the previous one, even when a foreign
    # token is a prefix of it (rendered settings name the root; a checkout under the old project's parent folder).
    # Only a whole path component counts: `<root>-old` or `<root>2` is a sibling folder, so it stays visible to the
    # token check (review of the S42 fix, retest 6 F1).
    forms = sorted({root.as_posix(), str(root)}, key=len, reverse=True)
    own = re.compile(r"(?<![\w.-])(?:" + "|".join(re.escape(f) for f in forms) + r")(?![\w.-])")
    for path in files:
        rel = _rel(path, root)
        if any(rel == ex or (ex.endswith("/") and rel.startswith(ex)) for ex in cfg.foreign_scan_exclude):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, raw in enumerate(text.splitlines(), 1):
            line = own.sub("", raw)
            for token in cfg.foreign_bindings:
                if token in line:
                    hits.append(f"{rel}:{number} {token}")
    if hits:
        return [Result("FAIL", "foreign", f"{len(hits)} previous-project token(s); first: {'; '.join(hits[:5])}",
                       "rebind each occurrence (or exclude a verbatim archive in foreign_scan_exclude)")]
    return [Result("OK", "foreign", f"none of {len(cfg.foreign_bindings)} previous-project tokens in live files")]


def check_links(root: Path, files: list[Path]) -> list[Result]:
    dead: list[str] = []
    for path in files:
        rel = _rel(path, root)
        if path.suffix != ".md" or rel.startswith("tasks/archive/"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        text = re.sub(r"```.*?```", "", text, flags=re.S)
        for target in LINK_RE.findall(text):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("#"):
                continue
            target = target.split("#", 1)[0]
            if target and not (path.parent / target).exists():
                dead.append(f"{rel} -> {target}")
    if dead:
        return [Result("FAIL", "links", f"{len(dead)} dead relative link(s); first: {'; '.join(dead[:5])}",
                       "fix or remove the links")]
    return [Result("OK", "links", "no dead relative links in live Markdown")]


def run_checks(root: Path, run_cli: bool = True) -> list[Result]:
    results, cfg = check_binding(root)
    if cfg is None:
        return results
    files = tracked_text_files(root)
    for step in (
        lambda: check_python(root, cfg),
        lambda: check_instructions(root),
        lambda: check_skills(root, cfg),
        lambda: check_clis(root, cfg, run_cli),
        lambda: check_runner(root, cfg),
        lambda: check_claude_settings(root, cfg),
        lambda: check_tool_configs(root, cfg),
        lambda: check_state(root),
        lambda: check_foreign(root, cfg, files),
        lambda: check_links(root, files),
    ):
        results += step()
    return results


# ---- apply ----------------------------------------------------------------------------------------------------------


def sync_skills(root: Path) -> list[str]:
    done: list[str] = []
    for name in _skill_names(root):
        src, dst = root / SKILLS_SRC / name, root / SKILLS_CLAUDE / name
        if not _tree_differs(src, dst):
            continue
        if dst.is_symlink() or dst.is_file():
            dst.unlink()
        elif dst.is_dir():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        done.append(f"synced skill {name} -> {SKILLS_CLAUDE}/{name}")
    return done


def _strip_comments(node: object) -> object:
    if isinstance(node, dict):
        return {k: _strip_comments(v) for k, v in node.items() if not k.startswith("_")}
    if isinstance(node, list):
        return [_strip_comments(v) for v in node]
    return node


def render_settings(root: Path, cfg: project_config.ProjectConfig) -> str:
    template = (root / SETTINGS_TEMPLATE).read_text(encoding="utf-8")
    values = {
        "PROJECT_ROOT": root.as_posix(),
        "PERMISSION_ROOT": permission_root(root),
        "VENV_PYTHON": venv_python(root, cfg.venv_dir).as_posix(),
    }
    data = _strip_comments(json.loads(PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], template)))
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def apply(root: Path, force_settings: bool) -> tuple[list[str], list[str]]:
    cfg = project_config.load(root)
    done: list[str] = []
    skipped: list[str] = []
    state = root / ".tmp" / "pm-state"
    if not state.is_dir():
        state.mkdir(parents=True)
        done.append("created .tmp/pm-state")
    if cfg.agents["claude"].enabled:
        done += sync_skills(root)
        target = root / SETTINGS_LOCAL
        rendered = render_settings(root, cfg)
        current = target.read_text(encoding="utf-8") if target.is_file() else None
        if current == rendered:
            pass
        elif current is not None and not force_settings:
            skipped.append(f"{SETTINGS_LOCAL} exists and differs; rerun with --force-settings to replace it")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(rendered, encoding="utf-8")
            done.append(f"rendered {SETTINGS_LOCAL} for {root.as_posix()}")
    memory_path = root / MEMORY_HOOKS
    text = memory_path.read_text(encoding="utf-8")
    new = re.sub(r'("batch_branch":\s*)"[^"]*"', lambda m: m.group(1) + json.dumps(cfg.integration_branch), text, count=1)
    if new != text:
        memory_path.write_text(new, encoding="utf-8")
        done.append(f"memory_hooks.json batch_branch = {cfg.integration_branch}")
    if not venv_python(root, cfg.venv_dir).exists():
        skipped.append(f"no {cfg.venv_dir}: create it yourself (python -m venv {cfg.venv_dir}), apply does not install")
    return done, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="read-only verification")
    c.add_argument("--json", action="store_true", help="print the results as JSON")
    c.add_argument("--no-cli", action="store_true", help="only look the CLIs up on PATH, do not run --version")
    a = sub.add_parser("apply", help="idempotent repairs")
    a.add_argument("--force-settings", action="store_true", help=f"replace an existing {SETTINGS_LOCAL}")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.cmd == "apply":
        try:
            done, skipped = apply(root, args.force_settings)
        except project_config.ProjectConfigError as exc:
            print(f"FAIL binding: {exc}")
            return 1
        for line in done or ["nothing to change"]:
            print(f"done: {line}")
        for line in skipped:
            print(f"skipped: {line}")
        print("next: setup_project.py check")
        return 0
    results = run_checks(root, run_cli=not args.no_cli)
    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2, ensure_ascii=False))
    else:
        for r in results:
            print(f"{r.level:<4} {r.area:<12} {r.message}" + (f"\n{'':17}fix: {r.fix}" if r.fix and r.level != "OK" else ""))
        counts = {lvl: sum(r.level == lvl for r in results) for lvl in ("OK", "WARN", "FAIL")}
        print(f"summary: {counts['OK']} ok, {counts['WARN']} warn, {counts['FAIL']} fail")
    return 1 if any(r.level == "FAIL" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
