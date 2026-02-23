#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path


def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def append_log(log_path: Path, text: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(text)


def run_command(
    cmd,
    cwd: Path,
    log_path: Path,
    shell: bool = False,
    env: dict | None = None,
):
    shown = cmd if isinstance(cmd, str) else " ".join(cmd)
    append_log(
        log_path,
        f"\n[{now_iso()}]$ {shown}\n[cwd] {cwd}\n",
    )
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        shell=shell,
        text=True,
        capture_output=True,
        env=env,
    )
    if proc.stdout:
        append_log(log_path, f"[stdout]\n{proc.stdout}\n")
    if proc.stderr:
        append_log(log_path, f"[stderr]\n{proc.stderr}\n")
    append_log(log_path, f"[exit] {proc.returncode}\n")
    return proc


def sanitize_cmd(line: str) -> str:
    cmd = line.strip()
    if cmd.startswith("- ") or cmd.startswith("* "):
        cmd = cmd[2:].strip()
    if len(cmd) > 2 and cmd[0].isdigit():
        dot_idx = cmd.find(".")
        if 0 < dot_idx < 4:
            prefix = cmd[:dot_idx]
            if prefix.isdigit():
                cmd = cmd[dot_idx + 1 :].strip()
    if cmd.startswith("`") and cmd.endswith("`") and len(cmd) >= 2:
        cmd = cmd[1:-1].strip()
    return cmd


def extract_acceptance_commands(prompt_text: str) -> list[str]:
    lines = prompt_text.splitlines()
    commands: list[str] = []
    in_acceptance = False
    in_fence = False

    for raw in lines:
        stripped = raw.strip()

        if not in_acceptance:
            if stripped.lower().startswith("acceptance:"):
                in_acceptance = True
                remainder = stripped.split(":", 1)[1].strip()
                if remainder:
                    cmd = sanitize_cmd(remainder)
                    if cmd:
                        commands.append(cmd)
            continue

        if stripped.startswith("```"):
            in_fence = not in_fence
            continue

        if in_fence:
            if stripped:
                cmd = sanitize_cmd(stripped)
                if cmd:
                    commands.append(cmd)
            continue

        if not stripped:
            break

        if raw == raw.lstrip() and stripped.endswith(":"):
            break

        cmd = sanitize_cmd(stripped)
        if cmd:
            commands.append(cmd)

    return commands


def parse_codex_jsonl(jsonl_text: str) -> tuple[bool, list[str]]:
    has_error_event = False
    errors: list[str] = []

    for idx, line in enumerate(jsonl_text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            has_error_event = True
            errors.append(f"invalid JSONL at line {idx}: {exc}")
            continue

        event_type = str(obj.get("type", "")).lower()
        event_name = str(obj.get("event", "")).lower()
        level = str(obj.get("level", "")).lower()
        status = str(obj.get("status", "")).lower()
        has_error_key = bool(obj.get("error"))

        if (
            "error" in event_type
            or "error" in event_name
            or level == "error"
            or status == "error"
            or has_error_key
        ):
            has_error_event = True
            snippet = json.dumps(obj, ensure_ascii=False)
            errors.append(f"codex error event line {idx}: {snippet}")

    return has_error_event, errors


def load_existing_result(result_path: Path) -> dict:
    if not result_path.exists():
        return {}
    try:
        with result_path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def write_result(result_path: Path, result: dict) -> None:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    with result_path.open("w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        f.write("\n")


def process_task(task_path: Path, agent_root: Path) -> None:
    inbox_name = task_path.name
    with task_path.open("r", encoding="utf-8") as f:
        task = json.load(f)

    task_id = str(task.get("id", "")).strip() or task_path.stem
    title = str(task.get("title", "")).strip() or "untitled"
    branch = str(task.get("branch", "")).strip() or f"agent/{task_id}"
    prompt_text = str(task.get("prompt_text", ""))

    outbox_path = agent_root / "outbox" / f"{task_id}.result.json"
    existing = load_existing_result(outbox_path)
    existing_status = str(existing.get("status", "")).strip()

    if existing_status in {"needs_review", "done"}:
        return

    attempt = 1
    if existing_status == "blocked":
        attempt = int(existing.get("attempt", 0)) + 1

    repo_root = (Path(task.get("repo_root", ".")) if task.get("repo_root") else Path(".")).resolve()
    workdir = (repo_root / Path(task.get("workdir", "."))).resolve()

    logs_dir = agent_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    codex_log = logs_dir / f"{task_id}.codex.jsonl"
    tests_log = logs_dir / f"{task_id}.tests.log"
    runner_log = logs_dir / f"{task_id}.runner.log"

    # Reset logs for current attempt.
    codex_log.write_text("", encoding="utf-8")
    tests_log.write_text("", encoding="utf-8")
    runner_log.write_text("", encoding="utf-8")

    errors: list[str] = []
    changed_files: list[str] = []
    test_commands: list[str] = []
    test_ok = True
    commit_hash = ""
    status = "needs_review"
    summary = ""
    reason = ""

    append_log(runner_log, f"[{now_iso()}] processing {inbox_name} (attempt {attempt})\n")

    # Step A: checkout/create branch.
    cp = run_command(["git", "checkout", "-b", branch], repo_root, runner_log)
    if cp.returncode != 0:
        cp2 = run_command(["git", "checkout", branch], repo_root, runner_log)
        if cp2.returncode != 0:
            status = "blocked"
            errors.append(f"git checkout failed for branch '{branch}'")

    # Step B/C: run codex exec --json and parse jsonl.
    codex_stdout = ""
    if status != "blocked":
        cp = run_command(["codex", "exec", "--json", prompt_text], workdir, runner_log)
        codex_stdout = cp.stdout or ""
        codex_log.write_text(codex_stdout, encoding="utf-8")
        if cp.returncode != 0:
            status = "blocked"
            errors.append(f"codex exec failed with code {cp.returncode}")
        has_error_event, codex_errors = parse_codex_jsonl(codex_stdout)
        if has_error_event:
            status = "blocked"
            errors.extend(codex_errors)

    # Step D: acceptance tests from prompt.
    test_commands = extract_acceptance_commands(prompt_text)
    if test_commands:
        append_log(tests_log, f"[{now_iso()}] acceptance commands for {task_id}\n")
    for cmd in test_commands:
        cp = run_command(cmd, workdir, tests_log, shell=True)
        if cp.returncode != 0:
            test_ok = False
            status = "blocked"
            errors.append(f"acceptance command failed: {cmd} (exit {cp.returncode})")
            break

    # Step E: commit if changed.
    if status != "blocked":
        cp = run_command(["git", "status", "--porcelain"], repo_root, runner_log)
        if cp.returncode != 0:
            status = "blocked"
            errors.append("git status --porcelain failed")
        else:
            lines = [line for line in cp.stdout.splitlines() if line.strip()]
            changed_files = [line[3:].strip() if len(line) > 3 else line.strip() for line in lines]

            if not changed_files:
                status = "blocked"
                reason = "no changes"
                errors.append("no changes")
            else:
                cp_add = run_command(["git", "add", "-A"], repo_root, runner_log)
                if cp_add.returncode != 0:
                    status = "blocked"
                    errors.append("git add -A failed")
                else:
                    commit_msg = f"agent({task_id}): {title}"
                    cp_commit = run_command(
                        ["git", "commit", "-m", commit_msg],
                        repo_root,
                        runner_log,
                    )
                    if cp_commit.returncode != 0:
                        status = "blocked"
                        errors.append("git commit failed")
                    else:
                        cp_rev = run_command(
                            ["git", "rev-parse", "HEAD"],
                            repo_root,
                            runner_log,
                        )
                        if cp_rev.returncode == 0:
                            commit_hash = cp_rev.stdout.strip()

    if status == "needs_review":
        summary = f"Task {task_id} completed; branch {branch} ready for review."
    elif errors and errors[-1] == "no changes":
        summary = f"Task {task_id} blocked: no changes."
    else:
        summary = f"Task {task_id} blocked. See logs."

    result = {
        "id": task_id,
        "attempt": attempt,
        "status": status,
        "reason": reason,
        "branch": branch,
        "commit": commit_hash,
        "changed_files": changed_files,
        "test_commands": test_commands,
        "test_ok": test_ok,
        "summary": summary,
        "needs_decision": [],
        "errors": errors,
        "updated_at": now_iso(),
    }
    write_result(outbox_path, result)


def main() -> int:
    repo_root = Path.cwd().resolve()
    agent_root = repo_root / ".agent"
    inbox = agent_root / "inbox"
    outbox = agent_root / "outbox"
    logs = agent_root / "logs"

    inbox.mkdir(parents=True, exist_ok=True)
    outbox.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    task_files = sorted(
        p
        for p in inbox.glob("*.json")
        if not p.name.endswith(".result.json") and p.is_file()
    )

    for task_path in task_files:
        try:
            process_task(task_path, agent_root)
        except Exception as exc:
            task_id = task_path.stem
            outbox_path = outbox / f"{task_id}.result.json"
            existing = load_existing_result(outbox_path)
            attempt = int(existing.get("attempt", 0)) + 1 if existing else 1
            result = {
                "id": task_id,
                "attempt": attempt,
                "status": "blocked",
                "reason": "runner_exception",
                "branch": "",
                "commit": "",
                "changed_files": [],
                "test_commands": [],
                "test_ok": False,
                "summary": f"Task {task_id} blocked by runner exception.",
                "needs_decision": [],
                "errors": [str(exc)],
                "updated_at": now_iso(),
            }
            write_result(outbox_path, result)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
