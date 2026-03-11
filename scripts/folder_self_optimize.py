#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATE_VERSION = 2
STATE_ROOT = Path.home() / ".codex" / "state" / "folder-self-optimize"
SNAPSHOT_DIRNAME = "baseline"
STATE_FILENAME = "state.json"
JOURNAL_FILENAME = "journal.jsonl"
PROMPT_FILENAME = "last_prompt.md"
REPORT_FILENAME = "latest_report.md"
LOCK_FILENAME = "process.lock"
SESSION_FILENAME = "active_run.json"
WORKSPACES_DIRNAME = "workspaces"
STDOUT_LIMIT = 12_000
CODEX_TIMEOUT_SECONDS = 60 * 25

IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".turbo",
    ".next",
    "dist",
    "build",
    "coverage",
    ".idea",
    ".vscode",
    ".home",
    ".tmp",
}

PROTECTED_BASENAMES = {
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "Gemfile",
    "Gemfile.lock",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "setup.cfg",
    "tox.ini",
    "Makefile",
    ".env",
    ".tool-versions",
}

PROTECTED_PREFIXES = (
    ".github/",
    "infra/",
    "terraform/",
    "migrations/",
    "alembic/",
    "helm/",
    ".devcontainer/",
)

DEFAULT_OBJECTIVE = (
    "Converge the target folder toward a smaller, cleaner, lower-entropy "
    "implementation. Reuse existing code, delete dead code, preserve behavior, "
    "and do not expand architecture."
)


@dataclass
class CommandResult:
    command: str
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float


@dataclass
class Evaluation:
    verify_success: bool
    score: float
    score_source: str
    static_score: float
    metric_score: float | None
    parse_errors: int
    metrics: dict[str, Any]
    commands: list[dict[str, Any]]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stable_slug(path: Path) -> str:
    clean = "".join(ch if ch.isalnum() else "-" for ch in str(path.resolve()).lower())
    clean = "-".join(part for part in clean.split("-") if part)
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:10]
    if len(clean) > 60:
        clean = clean[-60:]
    return f"{clean}-{digest}"


def state_dir_for(target_dir: Path) -> Path:
    return STATE_ROOT / stable_slug(target_dir)


def run_shell(
    command: str,
    cwd: Path,
    timeout_seconds: int | None = None,
    extra_env: dict[str, str] | None = None,
) -> CommandResult:
    started = time.time()
    proc = subprocess.run(
        ["bash", "-lc", command],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        env=extra_env,
        check=False,
    )
    return CommandResult(
        command=command,
        returncode=proc.returncode,
        stdout=proc.stdout[-STDOUT_LIMIT:],
        stderr=proc.stderr[-STDOUT_LIMIT:],
        duration_seconds=round(time.time() - started, 3),
    )


def is_text_file(path: Path) -> bool:
    try:
        sample = path.read_bytes()[:2048]
    except OSError:
        return False
    return b"\x00" not in sample


def read_text(path: Path) -> str | None:
    if not path.exists() or not path.is_file() or not is_text_file(path):
        return None
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    except OSError:
        return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def should_skip_dir(path: Path) -> bool:
    return path.name in IGNORED_DIRS


def is_protected_path(rel_path: str, allow_protected_edits: set[str]) -> bool:
    if rel_path in allow_protected_edits:
        return False
    basename = Path(rel_path).name
    if basename in PROTECTED_BASENAMES:
        return True
    return any(rel_path.startswith(prefix) for prefix in PROTECTED_PREFIXES)


def discover_files(target_dir: Path) -> list[Path]:
    files: list[Path] = []
    for root, dirnames, filenames in os.walk(target_dir):
        root_path = Path(root)
        dirnames[:] = [name for name in dirnames if name not in IGNORED_DIRS]
        if should_skip_dir(root_path):
            continue
        for filename in filenames:
            path = root_path / filename
            if path.name == STATE_FILENAME:
                continue
            if path.is_file():
                files.append(path)
    return sorted(files)


def build_lock_manifest(target_dir: Path, allow_protected_edits: set[str]) -> dict[str, Any]:
    locked_files = []
    for path in discover_files(target_dir):
        rel_path = path.relative_to(target_dir).as_posix()
        locked_files.append(
            {
                "path": rel_path,
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
                "protected": is_protected_path(rel_path, allow_protected_edits),
            }
        )
    return {
        "version": STATE_VERSION,
        "target_dir": str(target_dir.resolve()),
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "locked_files": locked_files,
        "protected_files": [item["path"] for item in locked_files if item["protected"]],
    }


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_target_dir(target_dir: Path) -> None:
    resolved = target_dir.resolve()
    codex_root = (Path.home() / ".codex").resolve()
    if resolved == codex_root or codex_root in resolved.parents:
        raise SystemExit(f"Refusing to optimize Codex control paths: {resolved}")


@contextmanager
def state_lock(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / LOCK_FILENAME
    with lock_path.open("w", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SystemExit(f"Another folder-self-optimize run already holds {lock_path}") from exc
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def session_path(state_dir: Path) -> Path:
    return state_dir / SESSION_FILENAME


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def cleanup_workspaces(state_dir: Path) -> None:
    workspaces_root = state_dir / WORKSPACES_DIRNAME
    if workspaces_root.exists():
        shutil.rmtree(workspaces_root)


def recover_if_needed(state: dict[str, Any]) -> None:
    state_dir = Path(state["state_dir"])
    path = session_path(state_dir)
    if not path.exists():
        return
    session = load_json(path)
    session_pid = int(session.get("pid", -1))
    if session_pid == os.getpid():
        return
    if process_is_alive(session_pid):
        raise SystemExit(
            f"State for {state['target_dir']} is already active under pid {session_pid}. "
            "Wait for it to finish or terminate it before starting a new run."
        )
    if session.get("applying"):
        restore_locked_files(
            target_dir=Path(state["target_dir"]),
            snapshot_dir=state_dir / SNAPSHOT_DIRNAME,
            locked_files=state["locked_files"],
        )
    cleanup_workspaces(state_dir)
    path.unlink(missing_ok=True)


def write_session(state_dir: Path, payload: dict[str, Any]) -> None:
    path = session_path(state_dir)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def clear_session(state_dir: Path) -> None:
    session_path(state_dir).unlink(missing_ok=True)


def build_eval_env(root_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    home_dir = root_dir / ".home"
    tmp_dir = root_dir / ".tmp"
    home_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home_dir)
    env["TMPDIR"] = str(tmp_dir)
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return env


def copy_manifest_files(source_dir: Path, dest_dir: Path, locked_files: list[dict[str, Any]]) -> None:
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    for item in locked_files:
        src = source_dir / item["path"]
        dst = dest_dir / item["path"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def restore_locked_files(target_dir: Path, snapshot_dir: Path, locked_files: list[dict[str, Any]]) -> None:
    baseline_paths = {item["path"] for item in locked_files}
    current_paths = {path.relative_to(target_dir).as_posix() for path in discover_files(target_dir)}
    for rel_path in sorted(current_paths - baseline_paths):
        path = target_dir / rel_path
        if path.exists():
            path.unlink()
            prune_empty_parents(path.parent, target_dir)
    for item in locked_files:
        src = snapshot_dir / item["path"]
        dst = target_dir / item["path"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def prune_empty_parents(start: Path, stop: Path) -> None:
    current = start
    stop = stop.resolve()
    while current.exists() and current.is_dir():
        if current.resolve() == stop:
            return
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def collect_current_hashes(target_dir: Path) -> dict[str, str]:
    return {
        path.relative_to(target_dir).as_posix(): sha256_file(path)
        for path in discover_files(target_dir)
    }


def current_changes(target_dir: Path, locked_files: list[dict[str, Any]]) -> dict[str, Any]:
    locked_map = {item["path"]: item for item in locked_files}
    current_map = collect_current_hashes(target_dir)
    locked_paths = set(locked_map)
    current_paths = set(current_map)
    added = sorted(current_paths - locked_paths)
    deleted = sorted(locked_paths - current_paths)
    modified = sorted(
        path for path in locked_paths & current_paths if current_map[path] != locked_map[path]["sha256"]
    )
    return {
        "added": added,
        "deleted": deleted,
        "modified": modified,
        "current_map": current_map,
    }


def detect_python_metrics(text: str) -> tuple[int, int]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return 1, 0
    large_functions = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = getattr(node, "lineno", 0)
            end = getattr(node, "end_lineno", start)
            if end - start + 1 > 50:
                large_functions += 1
    return 0, large_functions


def static_metrics(target_dir: Path, locked_files: list[dict[str, Any]]) -> tuple[dict[str, Any], float]:
    total_lines = 0
    text_files = 0
    todos = 0
    parse_errors = 0
    large_functions = 0
    repeated_lines: Counter[str] = Counter()
    for item in locked_files:
        path = target_dir / item["path"]
        text = read_text(path)
        if text is None:
            continue
        text_files += 1
        lines = text.splitlines()
        total_lines += len(lines)
        for line in lines:
            line_stripped = line.strip()
            if "TODO" in line or "FIXME" in line:
                todos += 1
            if len(line_stripped) >= 24 and not line_stripped.startswith("#"):
                repeated_lines[line_stripped] += 1
        if path.suffix == ".py":
            path_parse_errors, path_large_functions = detect_python_metrics(text)
            parse_errors += path_parse_errors
            large_functions += path_large_functions
    repeated_line_penalty = sum(count - 1 for count in repeated_lines.values() if count > 1)
    penalty = (
        total_lines
        + todos * 40
        + parse_errors * 2000
        + large_functions * 120
        + repeated_line_penalty * 3
    )
    metrics = {
        "text_files": text_files,
        "total_lines": total_lines,
        "todo_count": todos,
        "parse_errors": parse_errors,
        "large_functions": large_functions,
        "repeated_line_penalty": repeated_line_penalty,
    }
    return metrics, float(-penalty)


def evaluate_metric_command(
    target_dir: Path,
    metric_command: str | None,
    metric_direction: str,
    extra_env: dict[str, str] | None = None,
) -> tuple[float | None, dict[str, Any] | None, CommandResult | None, bool, list[str]]:
    if not metric_command:
        return None, None, None, True, []
    result = run_shell(
        metric_command,
        cwd=target_dir,
        timeout_seconds=CODEX_TIMEOUT_SECONDS,
        extra_env=extra_env,
    )
    if result.returncode != 0:
        return None, None, result, False, ["metric command returned non-zero exit status"]
    stdout = result.stdout.strip()
    score_value: float | None = None
    payload: dict[str, Any] | None = None
    gate_ok = True
    gate_reasons: list[str] = []
    if stdout:
        try:
            decoded = json.loads(stdout)
            if isinstance(decoded, dict) and "score" in decoded:
                payload = decoded
                score_value = float(decoded["score"])
                if "pass" in decoded and not bool(decoded["pass"]):
                    gate_ok = False
                    gate_reasons.append(str(decoded.get("reason") or "metric pass=false"))
                if bool(decoded.get("veto", False)):
                    gate_ok = False
                    gate_reasons.append(str(decoded.get("reason") or "metric veto=true"))
                if isinstance(decoded.get("constraints"), list):
                    for item in decoded["constraints"]:
                        if isinstance(item, dict) and not bool(item.get("pass", True)):
                            gate_ok = False
                            gate_reasons.append(
                                str(item.get("reason") or item.get("name") or "metric constraint failed")
                            )
            elif isinstance(decoded, (int, float)):
                score_value = float(decoded)
                payload = {"score": score_value}
        except json.JSONDecodeError:
            try:
                score_value = float(stdout.splitlines()[-1].strip())
                payload = {"score": score_value}
            except ValueError:
                score_value = None
    if score_value is None:
        return None, payload, result, False, ["metric command did not yield a numeric score"]
    if metric_direction == "lower-is-better":
        score_value = -score_value
    return score_value, payload, result, gate_ok, gate_reasons


def evaluate_dir(root_dir: Path, state: dict[str, Any], locked_files: list[dict[str, Any]]) -> Evaluation:
    command_results: list[CommandResult] = []
    shell_success = True
    env = build_eval_env(root_dir)
    for command in state["verify_commands"]:
        result = run_shell(
            command,
            cwd=root_dir,
            timeout_seconds=CODEX_TIMEOUT_SECONDS,
            extra_env=env,
        )
        command_results.append(result)
        if result.returncode != 0:
            shell_success = False
    metrics, static_score = static_metrics(root_dir, locked_files)
    metric_score, metric_payload, metric_result, metric_gate_ok, metric_gate_reasons = evaluate_metric_command(
        target_dir=root_dir,
        metric_command=state.get("metric_command"),
        metric_direction=state.get("metric_direction", "higher-is-better"),
        extra_env=env,
    )
    if metric_result is not None:
        command_results.append(metric_result)
    metric_ok = metric_score is not None if state.get("metric_command") else True
    verify_success = shell_success and metrics["parse_errors"] == 0 and metric_ok and metric_gate_ok
    total_score = metric_score if metric_score is not None else static_score
    if not verify_success:
        total_score -= 1_000_000_000.0
    merged_metrics = dict(metrics)
    if metric_payload is not None:
        merged_metrics["metric_payload"] = metric_payload
    if metric_gate_reasons:
        merged_metrics["metric_gate_reasons"] = metric_gate_reasons
    return Evaluation(
        verify_success=verify_success,
        score=float(total_score),
        score_source="metric" if metric_score is not None else "static",
        static_score=float(static_score),
        metric_score=metric_score,
        parse_errors=metrics["parse_errors"],
        metrics=merged_metrics,
        commands=[asdict(item) for item in command_results],
    )


def evaluate_target(state: dict[str, Any]) -> Evaluation:
    state_dir = Path(state["state_dir"])
    workspaces_root = state_dir / WORKSPACES_DIRNAME
    workspaces_root.mkdir(parents=True, exist_ok=True)
    eval_dir = Path(tempfile.mkdtemp(prefix="baseline-eval-", dir=str(workspaces_root)))
    try:
        copy_manifest_files(Path(state["target_dir"]), eval_dir, state["locked_files"])
        return evaluate_dir(eval_dir, state, state["locked_files"])
    finally:
        remove_workspace(eval_dir)


def compute_diff_limits(
    target_dir: Path,
    snapshot_dir: Path,
    modified_files: list[str],
) -> tuple[int, int]:
    touched = len(modified_files)
    net_new_lines = 0
    for rel_path in modified_files:
        before_text = read_text(snapshot_dir / rel_path) or ""
        after_text = read_text(target_dir / rel_path) or ""
        net_new_lines += len(after_text.splitlines()) - len(before_text.splitlines())
    return touched, net_new_lines


def guard_candidate(state: dict[str, Any]) -> tuple[bool, list[str], dict[str, Any]]:
    target_dir = Path(state["target_dir"])
    snapshot_dir = Path(state["state_dir"]) / SNAPSHOT_DIRNAME
    changes = current_changes(target_dir, state["locked_files"])
    reasons: list[str] = []
    if changes["added"]:
        reasons.append(f"new files are forbidden: {', '.join(changes['added'][:10])}")
    if changes["deleted"]:
        reasons.append(f"deleted files are forbidden: {', '.join(changes['deleted'][:10])}")
    protected_set = set(state["protected_files"])
    protected_touched = sorted(path for path in changes["modified"] if path in protected_set)
    if protected_touched:
        reasons.append(f"protected files changed: {', '.join(protected_touched[:10])}")
    touched, net_new_lines = compute_diff_limits(target_dir, snapshot_dir, changes["modified"])
    if touched > state["touch_limit"]:
        reasons.append(f"touched files {touched} exceeds limit {state['touch_limit']}")
    if net_new_lines > state["net_line_limit"]:
        reasons.append(f"net new lines {net_new_lines} exceeds limit {state['net_line_limit']}")
    if not changes["modified"] and not changes["added"] and not changes["deleted"]:
        reasons.append("no file changes were produced")
    details = {
        "modified": changes["modified"],
        "added": changes["added"],
        "deleted": changes["deleted"],
        "touched_files": touched,
        "net_new_lines": net_new_lines,
    }
    return not reasons, reasons, details


def better_than(candidate: Evaluation, baseline: Evaluation) -> tuple[bool, str]:
    if candidate.verify_success and not baseline.verify_success:
        return True, "candidate turns a red baseline green"
    if not candidate.verify_success and baseline.verify_success:
        return False, "candidate breaks verification"
    if candidate.score > baseline.score:
        return True, f"candidate score improved from {baseline.score:.3f} to {candidate.score:.3f}"
    return False, f"candidate score did not improve baseline {baseline.score:.3f}"


def human_file_list(locked_files: list[dict[str, Any]], limit: int = 200) -> str:
    paths = [item["path"] for item in locked_files]
    if len(paths) > limit:
        head = "\n".join(f"- {path}" for path in paths[:limit])
        return f"{head}\n- ... ({len(paths) - limit} more locked files omitted)"
    return "\n".join(f"- {path}" for path in paths)


def recent_journal_summary(state_dir: Path, limit: int = 6) -> str:
    entries = load_journal_entries(state_dir)
    if not entries:
        return "- none"
    summary_lines: list[str] = []
    for entry in entries[-limit:]:
        changed = entry.get("guard_details", {}).get("modified", [])
        changed_label = ", ".join(changed[:3]) if changed else "no source change"
        summary_lines.append(
            f"- iter {entry.get('iteration')}: "
            f"{'keep' if entry.get('accepted') else 'discard'} | "
            f"{changed_label} | {entry.get('reason', 'no reason')}"
        )
    return "\n".join(summary_lines)


def load_journal_entries(state_dir: Path) -> list[dict[str, Any]]:
    path = state_dir / JOURNAL_FILENAME
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def format_score(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f}"


def render_report(state: dict[str, Any], max_entries: int = 12) -> str:
    state_dir = Path(state["state_dir"])
    baseline = state.get("baseline")
    drift = drift_details(state)
    entries = load_journal_entries(state_dir)
    keeps = [entry for entry in entries if entry.get("accepted")]
    discards = [entry for entry in entries if not entry.get("accepted")]
    lines = [
        "# Folder Self Optimize Report",
        "",
        "English summary first. Chinese notes are kept short under each section.",
        "",
        "## Scope",
        f"- Target: `{state['target_dir']}`",
        f"- Locked files: `{len(state['locked_files'])}`",
        f"- Protected files: `{len(state['protected_files'])}`",
        f"- Verify commands: `{len(state['verify_commands'])}`",
        f"- Metric command: `{state.get('metric_command') or 'none'}`",
        "",
        "CN: 這裡是在說它現在鎖住哪個資料夾、驗證方式是什麼、目前是不是有自定義評分。",
        "",
        "## Baseline",
    ]
    if baseline:
        lines.extend(
            [
                f"- Verification: `{'pass' if baseline['verify_success'] else 'fail'}`",
                f"- Score: `{baseline['score']:.3f}`",
                f"- Score source: `{baseline['score_source']}`",
                f"- Static score: `{baseline['static_score']:.3f}`",
                f"- Metric score: `{format_score(baseline.get('metric_score'))}`",
            ]
        )
    else:
        lines.append("- Baseline has not been computed yet.")
    lines.extend(
        [
            "",
            "CN: baseline 就是目前被接受的版本，不是最後一次嘗試的版本。",
            "",
            "## Drift",
            f"- Modified: `{len(drift['modified'])}`",
            f"- Added: `{len(drift['added'])}`",
            f"- Deleted: `{len(drift['deleted'])}`",
        ]
    )
    if drift["modified"]:
        lines.append(f"- Sample modified paths: `{', '.join(drift['modified'][:5])}`")
    if drift["added"]:
        lines.append(f"- Sample added paths: `{', '.join(drift['added'][:5])}`")
    if drift["deleted"]:
        lines.append(f"- Sample deleted paths: `{', '.join(drift['deleted'][:5])}`")
    lines.extend(
        [
            "",
            "CN: drift 不等於壞掉，只代表現在目錄和記錄中的 baseline 不一致。",
            "",
            "## Loop History",
            f"- Total iterations logged: `{len(entries)}`",
            f"- Keeps: `{len(keeps)}`",
            f"- Discards: `{len(discards)}`",
        ]
    )
    if entries:
        lines.extend(["", "### Recent iterations"])
        for entry in entries[-max_entries:]:
            changed = entry.get("guard_details", {}).get("modified", [])
            changed_label = ", ".join(changed[:3]) if changed else "no source change"
            lines.append(
                f"- iter `{entry.get('iteration')}` | "
                f"`{'keep' if entry.get('accepted') else 'discard'}` | "
                f"`{changed_label}` | {entry.get('reason', 'no reason')}"
            )
    else:
        lines.append("- No iterations have been logged yet.")
    lines.extend(
        [
            "",
            "CN: 看 `keep/discard` 和 reason，通常就能知道它到底是在進步，還是在原地打轉。",
        ]
    )
    if keeps:
        last_keep = keeps[-1]
        candidate = last_keep.get("candidate", {})
        lines.extend(
            [
                "",
                "## Last Accepted Candidate",
                f"- Iteration: `{last_keep.get('iteration')}`",
                f"- Score: `{format_score(candidate.get('score'))}`",
                f"- Verification: `{'pass' if candidate.get('verify_success') else 'fail'}`",
                f"- Reason: {last_keep.get('reason', 'n/a')}",
                "",
                "CN: 這是最後一次真的寫回目標資料夾的候選版本。",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def write_report(state: dict[str, Any], output_path: Path | None = None) -> Path:
    state_dir = Path(state["state_dir"])
    path = output_path or (state_dir / REPORT_FILENAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(state), encoding="utf-8")
    return path


def build_prompt(state: dict[str, Any], baseline: Evaluation, iteration: int) -> str:
    verify_commands = state["verify_commands"] or ["(no external verify command configured)"]
    metric_line = state.get("metric_command") or "(no custom metric command configured; static simplification score is the fallback)"
    recent_memory = recent_journal_summary(Path(state["state_dir"]))
    prompt = f"""
You are running one bounded optimization iteration inside the current working directory.

Hard rules:
- Modify only existing files in the current working directory.
- Do not create, delete, rename, or move files.
- Do not change dependency or lock files.
- Do not add wrappers, managers, services, adapters, helper layers, or duplicate subsystems.
- Prefer deletions, simplifications, and direct edits to existing code.
- Touch at most {state["touch_limit"]} files.
- Keep net new lines at or below {state["net_line_limit"]}.
- Stop after one iteration worth of changes.

Objective:
{state["objective"]}

Acceptance gate after your edit:
- Verification commands:
{chr(10).join(f"  - {cmd}" for cmd in verify_commands)}
- Metric command:
  - {metric_line}
- Current baseline verification: {"pass" if baseline.verify_success else "fail"}
- Current baseline score: {baseline.score:.3f}
- Static score: {baseline.static_score:.3f}
- Custom metric score: {baseline.metric_score if baseline.metric_score is not None else "n/a"}

Locked file scope:
{human_file_list(state["locked_files"])}

Recent loop memory:
{recent_memory}

Work in place, keep the architecture flat, and optimize for a strictly better score without scope creep.
Iteration: {iteration}
"""
    return textwrap.dedent(prompt).strip()


def current_manifest(target_dir: Path, protected_paths: set[str]) -> list[dict[str, Any]]:
    manifest = []
    for path in discover_files(target_dir):
        rel_path = path.relative_to(target_dir).as_posix()
        manifest.append(
            {
                "path": rel_path,
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
                "protected": rel_path in protected_paths,
            }
        )
    return manifest


def drift_details(state: dict[str, Any]) -> dict[str, Any]:
    return current_changes(Path(state["target_dir"]), state["locked_files"])


def ensure_no_unapproved_drift(state: dict[str, Any], allow_rebaseline: bool) -> None:
    if allow_rebaseline:
        return
    changes = drift_details(state)
    if changes["added"] or changes["deleted"] or changes["modified"]:
        raise SystemExit(
            "Target directory drifted from the locked baseline. "
            "Run restore, or rerun with --rebaseline if you intentionally want to accept the current tree."
        )


def create_workspace(state: dict[str, Any], iteration: int, label: str) -> Path:
    state_dir = Path(state["state_dir"])
    workspaces_root = state_dir / WORKSPACES_DIRNAME
    workspaces_root.mkdir(parents=True, exist_ok=True)
    workspace = Path(
        tempfile.mkdtemp(
            prefix=f"iter-{iteration:03d}-{label}-",
            dir=str(workspaces_root),
        )
    )
    copy_manifest_files(state_dir / SNAPSHOT_DIRNAME, workspace, state["locked_files"])
    return workspace


def remove_workspace(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def apply_workspace_to_target(state: dict[str, Any], workspace_dir: Path) -> None:
    target_dir = Path(state["target_dir"])
    state_dir = Path(state["state_dir"])
    session = {
        "pid": os.getpid(),
        "started_at": utc_now(),
        "applying": True,
        "workspace_dir": str(workspace_dir),
    }
    write_session(state_dir, session)
    for item in state["locked_files"]:
        src = workspace_dir / item["path"]
        dst = target_dir / item["path"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def codex_command(
    target_dir: Path,
    prompt: str,
    model: str | None,
    extra_args: list[str],
    skip_git_repo_check: bool,
) -> list[str]:
    command = ["codex", "exec", "-C", str(target_dir), "-s", "workspace-write"]
    if skip_git_repo_check:
        command.append("--skip-git-repo-check")
    if model:
        command.extend(["-m", model])
    command.extend(extra_args)
    command.append(prompt)
    return command


def run_codex_iteration(
    state: dict[str, Any],
    baseline: Evaluation,
    iteration: int,
    prompt_only: bool,
    working_dir: Path,
) -> tuple[bool, str]:
    prompt = build_prompt(state, baseline, iteration)
    prompt_path = Path(state["state_dir"]) / PROMPT_FILENAME
    prompt_path.write_text(prompt + "\n", encoding="utf-8")
    if prompt_only:
        print(prompt)
        return True, "prompt_only"
    command = codex_command(
        target_dir=working_dir,
        prompt=prompt,
        model=state.get("codex_model"),
        extra_args=state.get("codex_extra_args", []),
        skip_git_repo_check=state.get("skip_git_repo_check", False),
    )
    try:
        proc = subprocess.run(
            command,
            cwd=working_dir,
            capture_output=True,
            text=True,
            timeout=state["codex_timeout_seconds"],
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "codex iteration timed out"
    transcript = {
        "timestamp": utc_now(),
        "working_dir": str(working_dir),
        "command": command,
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-STDOUT_LIMIT:],
        "stderr_tail": proc.stderr[-STDOUT_LIMIT:],
    }
    (Path(state["state_dir"]) / f"codex-iteration-{iteration}.json").write_text(
        json.dumps(transcript, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if proc.returncode != 0:
        return False, f"codex exited with {proc.returncode}"
    return True, "codex_completed"


def append_journal(state_dir: Path, payload: dict[str, Any]) -> None:
    path = state_dir / JOURNAL_FILENAME
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def init_or_load_state(args: argparse.Namespace) -> dict[str, Any]:
    target_dir = Path(args.target_dir).expanduser().resolve()
    if not target_dir.exists() or not target_dir.is_dir():
        raise SystemExit(f"Target directory does not exist: {target_dir}")
    validate_target_dir(target_dir)
    state_dir = state_dir_for(target_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / STATE_FILENAME
    if state_path.exists() and not args.relock:
        state = load_json(state_path)
    else:
        allow_protected_edits = set(args.allow_protected_edit or [])
        state = build_lock_manifest(target_dir, allow_protected_edits=allow_protected_edits)
    state["state_dir"] = str(state_dir)
    state["objective"] = args.objective or state.get("objective") or DEFAULT_OBJECTIVE
    state["verify_commands"] = args.verify or state.get("verify_commands") or autodetect_verify_commands(target_dir)
    state["metric_command"] = args.metric_command if args.metric_command is not None else state.get("metric_command")
    state["metric_direction"] = args.metric_direction or state.get("metric_direction") or "higher-is-better"
    state["touch_limit"] = args.touch_limit or state.get("touch_limit") or 3
    state["net_line_limit"] = args.net_line_limit or state.get("net_line_limit") or 120
    state["codex_model"] = args.codex_model or state.get("codex_model")
    state["codex_extra_args"] = args.codex_extra_arg or state.get("codex_extra_args") or []
    state["codex_timeout_seconds"] = args.codex_timeout_minutes * 60
    state["skip_git_repo_check"] = args.skip_git_repo_check or state.get("skip_git_repo_check", False)
    state["updated_at"] = utc_now()
    state["version"] = STATE_VERSION
    save_json(state_path, state)
    recover_if_needed(state)
    snapshot_dir = state_dir / SNAPSHOT_DIRNAME
    if not snapshot_dir.exists() or args.relock:
        copy_manifest_files(target_dir, snapshot_dir, state["locked_files"])
    return state


def autodetect_verify_commands(target_dir: Path) -> list[str]:
    commands: list[str] = []
    has_python = any(path.suffix == ".py" for path in discover_files(target_dir))
    if (target_dir / "tests").exists() or (target_dir / "pytest.ini").exists() or (target_dir / "conftest.py").exists():
        commands.append("pytest -q")
    if (target_dir / "Cargo.toml").exists():
        commands.append("cargo test --quiet")
    if (target_dir / "go.mod").exists():
        commands.append("go test ./...")
    if (target_dir / "package.json").exists():
        commands.append("npm test -- --runInBand")
    if has_python and not commands:
        commands.append("python3 -m compileall .")
    deduped: list[str] = []
    for command in commands:
        if command not in deduped:
            deduped.append(command)
    return deduped


def refresh_baseline(state: dict[str, Any], evaluation: Evaluation, source_dir: Path | None = None) -> None:
    baseline_source = source_dir or Path(state["target_dir"])
    protected_paths = set(state.get("protected_files", []))
    state["locked_files"] = current_manifest(baseline_source, protected_paths)
    state["protected_files"] = [item["path"] for item in state["locked_files"] if item["protected"]]
    state["baseline"] = asdict(evaluation)
    state["updated_at"] = utc_now()
    state_dir = Path(state["state_dir"])
    copy_manifest_files(baseline_source, state_dir / SNAPSHOT_DIRNAME, state["locked_files"])
    save_json(state_dir / STATE_FILENAME, state)


def restore_baseline(state: dict[str, Any]) -> None:
    restore_locked_files(
        target_dir=Path(state["target_dir"]),
        snapshot_dir=Path(state["state_dir"]) / SNAPSHOT_DIRNAME,
        locked_files=state["locked_files"],
    )


def print_status(state: dict[str, Any]) -> None:
    baseline = state.get("baseline")
    drift = drift_details(state)
    print(f"Target:        {state['target_dir']}")
    print(f"Locked files:  {len(state['locked_files'])}")
    print(f"Protected:     {len(state['protected_files'])}")
    print(f"Touch limit:   {state['touch_limit']}")
    print(f"Line limit:    {state['net_line_limit']}")
    print(f"Objective:     {state['objective']}")
    print("Verify cmds:")
    if state["verify_commands"]:
        for command in state["verify_commands"]:
            print(f"  - {command}")
    else:
        print("  - none")
    print(f"Metric cmd:    {state.get('metric_command') or 'none'}")
    if baseline:
        print(f"Baseline ok:   {baseline['verify_success']}")
        print(f"Baseline score:{baseline['score']:.3f}")
        print(f"Score source:  {baseline['score_source']}")
    else:
        print("Baseline:      not evaluated yet")
    drift_count = len(drift["modified"]) + len(drift["added"]) + len(drift["deleted"])
    print(f"Target drift:  {drift_count}")
    if drift_count:
        if drift["modified"]:
            print(f"  modified:    {', '.join(drift['modified'][:5])}")
        if drift["added"]:
            print(f"  added:       {', '.join(drift['added'][:5])}")
        if drift["deleted"]:
            print(f"  deleted:     {', '.join(drift['deleted'][:5])}")


def cmd_status(args: argparse.Namespace) -> int:
    state = init_or_load_state(args)
    if not state.get("baseline"):
        evaluation = evaluate_target(state)
        refresh_baseline(state, evaluation)
    print_status(state)
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    state = init_or_load_state(args)
    restore_baseline(state)
    write_report(state)
    print(f"Restored baseline for {state['target_dir']}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    state = init_or_load_state(args)
    if not state.get("baseline"):
        evaluation = evaluate_target(state)
        refresh_baseline(state, evaluation)
    output_path = Path(args.output).expanduser().resolve() if args.output else None
    report_path = write_report(state, output_path=output_path)
    if args.output:
        print(f"Wrote report to {report_path}")
    else:
        print(report_path.read_text(encoding="utf-8"), end="")
    return 0


def cmd_prompt(args: argparse.Namespace) -> int:
    state = init_or_load_state(args)
    ensure_no_unapproved_drift(state, allow_rebaseline=args.rebaseline)
    baseline = state.get("baseline")
    if baseline is None or args.rebaseline:
        baseline_eval = evaluate_target(state)
        refresh_baseline(state, baseline_eval)
        baseline = asdict(baseline_eval)
    prompt = build_prompt(state, Evaluation(**baseline), iteration=1)
    print(prompt)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    state = init_or_load_state(args)
    ensure_no_unapproved_drift(state, allow_rebaseline=args.rebaseline)
    state_dir = Path(state["state_dir"])
    run_session = {
        "pid": os.getpid(),
        "started_at": utc_now(),
        "applying": False,
        "workspace_dir": None,
    }
    write_session(state_dir, run_session)
    baseline_data = state.get("baseline")
    completed_cleanly = False
    no_improve_streak = 0
    try:
        if baseline_data is None or args.rebaseline:
            baseline_eval = evaluate_target(state)
            refresh_baseline(state, baseline_eval)
        else:
            baseline_eval = Evaluation(**baseline_data)
        write_report(state)
        if args.iterations == 0:
            print_status(state)
            completed_cleanly = True
            return 0
        if args.prompt_only:
            workspace_dir = create_workspace(state, iteration=1, label="prompt")
            try:
                run_codex_iteration(
                    state=state,
                    baseline=baseline_eval,
                    iteration=1,
                    prompt_only=True,
                    working_dir=workspace_dir,
                )
            finally:
                remove_workspace(workspace_dir)
            completed_cleanly = True
            return 0
        for iteration in range(1, args.iterations + 1):
            workspace_dir = create_workspace(state, iteration=iteration, label="mutate")
            run_session["workspace_dir"] = str(workspace_dir)
            write_session(state_dir, run_session)
            apply_started = False
            try:
                codex_ok, codex_reason = run_codex_iteration(
                    state=state,
                    baseline=baseline_eval,
                    iteration=iteration,
                    prompt_only=False,
                    working_dir=workspace_dir,
                )
                guard_ok, guard_reasons, guard_details = guard_candidate(
                    {
                        **state,
                        "target_dir": str(workspace_dir),
                    }
                )
                eval_dir = create_workspace(state, iteration=iteration, label="eval")
                try:
                    if codex_ok and guard_ok:
                        copy_manifest_files(workspace_dir, eval_dir, state["locked_files"])
                        candidate_eval = evaluate_dir(eval_dir, state, state["locked_files"])
                    else:
                        candidate_eval = Evaluation(
                            verify_success=False,
                            score=-1_000_000_000.0,
                            score_source="static",
                            static_score=-1_000_000_000.0,
                            metric_score=None,
                            parse_errors=0,
                            metrics={},
                            commands=[],
                        )
                finally:
                    remove_workspace(eval_dir)
                accept = False
                reason = codex_reason
                if not codex_ok:
                    reason = codex_reason
                elif not guard_ok:
                    reason = "; ".join(guard_reasons)
                else:
                    accept, reason = better_than(candidate_eval, baseline_eval)
                journal_entry = {
                    "timestamp": utc_now(),
                    "iteration": iteration,
                    "codex_ok": codex_ok,
                    "guard_ok": guard_ok,
                    "guard_reasons": guard_reasons,
                    "guard_details": guard_details,
                    "candidate": asdict(candidate_eval),
                    "baseline_before": asdict(baseline_eval),
                    "accepted": accept,
                    "reason": reason,
                }
                if accept:
                    apply_started = True
                    apply_workspace_to_target(state, workspace_dir)
                    baseline_eval = candidate_eval
                    refresh_baseline(state, baseline_eval, source_dir=workspace_dir)
                    no_improve_streak = 0
                    apply_started = False
                    run_session["applying"] = False
                    run_session["workspace_dir"] = None
                    write_session(state_dir, run_session)
                else:
                    no_improve_streak += 1
                append_journal(state_dir, journal_entry)
                write_report(state)
                status = "keep" if accept else "discard"
                print(
                    f"[{status}] iteration={iteration} "
                    f"baseline={baseline_eval.score:.3f} candidate={candidate_eval.score:.3f} "
                    f"reason={reason}"
                )
                if args.max_no_improve_streak and no_improve_streak >= args.max_no_improve_streak:
                    print(
                        f"[stop] no improvement streak reached {no_improve_streak} "
                        f"(limit={args.max_no_improve_streak})"
                    )
                    break
            finally:
                remove_workspace(workspace_dir)
                if not apply_started:
                    run_session["workspace_dir"] = None
                    run_session["applying"] = False
                    write_session(state_dir, run_session)
        completed_cleanly = True
        return 0
    finally:
        if completed_cleanly:
            clear_session(state_dir)


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("target_dir", help="Folder to lock and optimize.")
    parser.add_argument("--objective", help="Optimization objective text.")
    parser.add_argument("--verify", action="append", help="Verification command. Repeatable.")
    parser.add_argument(
        "--metric-command",
        help="Command that prints a JSON object with a numeric score field, or a plain numeric score.",
    )
    parser.add_argument(
        "--metric-direction",
        choices=["higher-is-better", "lower-is-better"],
        help="How to interpret the metric command score.",
    )
    parser.add_argument(
        "--allow-protected-edit",
        action="append",
        help="Protected filename that may be edited during relock.",
    )
    parser.add_argument("--touch-limit", type=int, help="Maximum files touched per iteration.")
    parser.add_argument("--net-line-limit", type=int, help="Maximum net new lines per iteration.")
    parser.add_argument("--codex-model", help="Optional model to pass to codex exec.")
    parser.add_argument(
        "--codex-extra-arg",
        action="append",
        help="Extra raw argument for codex exec. Repeatable.",
    )
    parser.add_argument(
        "--codex-timeout-minutes",
        type=int,
        default=25,
        help="Per-iteration timeout for codex exec.",
    )
    parser.add_argument(
        "--skip-git-repo-check",
        action="store_true",
        help="Pass --skip-git-repo-check to codex exec.",
    )
    parser.add_argument(
        "--relock",
        action="store_true",
        help="Rebuild the locked file manifest from the current folder contents.",
    )
    parser.add_argument(
        "--rebaseline",
        action="store_true",
        help="Recompute the baseline from current contents before iterating.",
    )
    parser.add_argument(
        "--output",
        help="Optional output path for report-related commands.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Folder-scoped closed-loop optimizer.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run the closed-loop optimizer.")
    add_common_args(run_parser)
    run_parser.add_argument("--iterations", type=int, default=3, help="Number of optimization iterations.")
    run_parser.add_argument(
        "--prompt-only",
        action="store_true",
        help="Emit the next codex prompt and exit without invoking codex.",
    )
    run_parser.add_argument(
        "--max-no-improve-streak",
        type=int,
        default=0,
        help="Stop early after this many consecutive non-improving iterations. 0 disables the stop condition.",
    )
    run_parser.set_defaults(func=cmd_run)

    status_parser = subparsers.add_parser("status", help="Show the current lock and baseline.")
    add_common_args(status_parser)
    status_parser.set_defaults(func=cmd_status)

    prompt_parser = subparsers.add_parser("prompt", help="Print the next mutation prompt.")
    add_common_args(prompt_parser)
    prompt_parser.set_defaults(func=cmd_prompt)

    restore_parser = subparsers.add_parser("restore", help="Restore the saved baseline.")
    add_common_args(restore_parser)
    restore_parser.set_defaults(func=cmd_restore)

    report_parser = subparsers.add_parser("report", help="Render a human-readable loop report.")
    add_common_args(report_parser)
    report_parser.set_defaults(func=cmd_report)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        target_dir = Path(args.target_dir).expanduser().resolve()
        with state_lock(state_dir_for(target_dir)):
            return int(args.func(args))
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
