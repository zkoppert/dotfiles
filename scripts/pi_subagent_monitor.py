#!/usr/bin/env python3
"""Monitor registered Pi agents without restarting agents or changing their authority."""

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

LABEL = "com.zkoppert.pi-subagent-monitor"
STALLED_SECONDS = 1800
REPORT_STATES = {"running", "blocked", "needs_review", "pr_opened", "complete"}


def load(path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def locked(root):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def process_identity(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "stat=,lstart=,command="],
                            check=False, capture_output=True, text=True, timeout=5,
                            env={**os.environ, "LC_ALL": "C"})
    if result.returncode == 1:
        return None
    if result.returncode:
        raise RuntimeError(f"Process inspection failed: {result.stderr.strip()}")
    parts = result.stdout.strip().split(None, 6)
    if len(parts) != 7 or parts[0].startswith("Z"):
        return None
    return {"pid": pid, "started": " ".join(parts[1:6]), "command": parts[6]}


def same_process(current, expected):
    # Pi replaces its argv title after startup; PID and start time remain stable.
    return bool(current and expected and current["pid"] == expected["pid"]
                and current["started"] == expected["started"])


def process_matches(expected):
    return same_process(process_identity(expected["pid"]), expected)


def launch_time_matches(identity, item):
    recorded = item.get("resumed_at", item.get("started_at", ""))
    try:
        launched_at = datetime.datetime.fromisoformat(recorded).timestamp()
        process_started = time.mktime(time.strptime(identity["started"], "%a %b %d %H:%M:%S %Y"))
    except (ValueError, TypeError):
        return False
    return abs(launched_at - process_started) < 10


def register(root, manifest, parent_session):
    manifest, parent_session = manifest.resolve(), parent_session.resolve()
    if not parent_session.is_file():
        raise ValueError("The parent session must exist")
    launches = load(manifest)
    if not isinstance(launches, list) or not launches:
        raise ValueError("The manifest must contain at least one task")
    tasks = []
    for item in launches:
        artifacts = Path(item["artifacts"]).resolve()
        if not artifacts.is_dir():
            raise ValueError(f"Missing task artifacts: {artifacts}")
        identity = process_identity(item.get("pid"))
        session = str(artifacts / "session.jsonl")
        if (identity and session not in identity["command"]
                and (identity["command"].strip() != "pi" or not launch_time_matches(identity, item))):
            identity = None
        task_id = str(item.get("issue", item.get("id", "")))
        if not task_id or task_id in {task["id"] for task in tasks}:
            raise ValueError("Task IDs must be present and unique")
        tasks.append({"id": task_id, "name": item.get("name", task_id),
                      "artifacts": str(artifacts), "process": identity})
    batch_id = digest(str(manifest))[:16]
    with locked(root):
        store = load(root / "registry.json", {"batches": {}, "outbox": {}})
        registry = store["batches"]
        previous = registry.get(batch_id, {})
        registry[batch_id] = {"parent_session": str(parent_session), "tasks": tasks,
                              "snapshots": previous.get("snapshots", {}),
                              "registered_at": previous.get("registered_at", time.time())}
        atomic_json(root / "registry.json", store)
    return {"batch": batch_id, "tasks": len(tasks), "parent_session": str(parent_session)}


def validate_report(value):
    if not isinstance(value, dict):
        raise TypeError("A status report must be an object")
    state, summary = value.get("state"), value.get("summary")
    if state not in REPORT_STATES or not isinstance(summary, str) or not summary.strip() or len(summary) > 500:
        raise ValueError("Use a supported state and a summary of 1 to 500 characters")
    url = value.get("pr_url", "")
    if (url or state == "pr_opened") and not re.fullmatch(r"https://github\.com/[\w.-]+/[\w.-]+/pull/[1-9][0-9]*", url):
        raise ValueError("PR state requires a GitHub pull request URL")
    if not isinstance(value.get("updated_at"), (int, float)):
        raise TypeError("A status report requires an update timestamp")
    evidence = Path(value.get("evidence", ""))
    if state == "complete" and (value.get("validation") != "passed" or value.get("ci") not in {"passed", "not_applicable"}
                                or not evidence.is_file() or not evidence.stat().st_size):
        raise ValueError("Completion requires passed validation, resolved CI, and a nonempty evidence file")


def report(artifacts, state, summary, *, pr_url="", evidence=None, validation="pending", ci="pending"):
    if not artifacts.is_dir():
        raise ValueError("Task artifacts must already exist")
    value = {"state": state, "summary": summary, "pr_url": pr_url,
             "evidence": str(evidence.resolve()) if evidence else "", "validation": validation,
             "ci": ci, "updated_at": time.time()}
    validate_report(value)
    atomic_json(artifacts / "agent-status.json", value)
    return value


def observe(task, now, registered_at):
    artifacts = Path(task["artifacts"])
    status = load(artifacts / "agent-status.json")
    continuation = load(artifacts / "publication-continuation.json", {})
    if not isinstance(continuation, dict):
        raise TypeError("Invalid continuation receipt")
    if continuation.get("state") == "running":
        identity = process_identity(continuation.get("agent_pid"))
        expected = task.get("continuation_process")
        bound = identity and (str(artifacts / "session.jsonl") in identity["command"]
                              or same_process(identity, expected)
                              or (identity["command"].strip() == "pi" and launch_time_matches(
                                  identity, {"started_at": continuation.get("updated_at", "")})))
        if bound:
            if expected is None:
                task["continuation_process"] = identity
            active = same_process(identity, task["continuation_process"])
        else:
            active = False
    else:
        active = bool(task.get("process") and process_matches(task["process"]))
    if status is not None:
        validate_report(status)
        state = status["state"]
        if state != "running":
            if state == "complete":
                return {"state": "complete_reported", "summary": status["summary"] + " (not independently verified)",
                        "pr_url": status.get("pr_url", "")}
            return {"state": state, "summary": status["summary"], "pr_url": status.get("pr_url", "")}
    if active:
        heartbeat = max([registered_at] + [path.stat().st_mtime for path in (
            artifacts / "session.jsonl", artifacts / "progress.md", artifacts / "stdout.log",
            artifacts / "publication-stdout.log") if path.is_file()])
        if now - heartbeat > STALLED_SECONDS:
            return {"state": "stalled", "summary": "The process is alive but has no recent recorded activity. Check before restarting.", "pr_url": ""}
        return {"state": "running", "summary": "Agent process identity matches. Work is in progress.", "pr_url": ""}
    if continuation.get("state") == "waiting" and process_identity(continuation.get("supervisor_pid")):
        return {"state": "continuation_pending", "summary": "A continuation supervisor is waiting. The task hasn't finished.", "pr_url": ""}
    return {"state": "stopped", "summary": "No tracked agent is running. Process exit is not proof of success. Inspect result.md.", "pr_url": ""}


def notify(event, root):
    executable = shutil.which("terminal-notifier")
    if executable is None:
        raise RuntimeError("terminal-notifier is unavailable; the durable session inbox still contains the update")
    message = "; ".join(f"{item['id']}: {item['state']}" for item in event["updates"])
    result = subprocess.run([executable, "-title", "Pi agent progress", "-message", message[:240],
                             "-group", f"pi-subagents-{event['batch']}", "-open", (root / "status.json").as_uri()],
                            check=False, text=True, capture_output=True, timeout=15)
    if result.returncode:
        raise RuntimeError(f"Notification failed: {result.stderr.strip()}")


def poll(root, *, now=None, notifications=True):
    now = time.time() if now is None else now
    with locked(root):
        store = load(root / "registry.json", {"batches": {}, "outbox": {}})
        registry, outbox = store["batches"], store["outbox"]
        all_items = []
        for batch_id, batch in registry.items():
            updates = []
            for task in batch["tasks"]:
                try:
                    observation = observe(task, now, batch["registered_at"])
                except (OSError, TypeError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                    observation = {"state": "monitor_error", "summary": str(error)[:500], "pr_url": ""}
                item = {**observation, "id": task["id"], "name": task["name"],
                        "artifacts": task["artifacts"], "batch": batch_id}
                all_items.append(item)
                if batch["snapshots"].get(task["id"]) != observation:
                    updates.append(item)
                    batch["snapshots"][task["id"]] = observation
            if updates:
                event_id = uuid.uuid4().hex
                outbox[event_id] = {"id": event_id, "batch": batch_id, "parent_session": batch["parent_session"],
                                    "observed_at": now, "updates": updates, "inbox_delivered": False,
                                    "desktop_delivered": False}
        # Save observations and pending deliveries together before notifying either channel.
        atomic_json(root / "registry.json", store)
        atomic_json(root / "status.json", all_items)
        for event_id, event in list(outbox.items()):
            if not event["inbox_delivered"]:
                inbox = root / "inbox" / digest(event["parent_session"]) / f"{event_id}.json"
                atomic_json(inbox, event)
                event["inbox_delivered"] = True
            if notifications and not event["desktop_delivered"]:
                try:
                    notify(event, root)
                    event["desktop_delivered"] = True
                    event.pop("notification_error", None)
                except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                    event["notification_error"] = str(error)
                    print(str(error), file=sys.stderr)
            if event["inbox_delivered"] and event["desktop_delivered"]:
                del outbox[event_id]
        atomic_json(root / "registry.json", store)
        return all_items


def install(home, state_root, *, bootstrap=True):
    source = Path(__file__).resolve().parents[1]
    runtime = home / ".local/share/pi-subagent-monitor"
    agent_dir = Path(os.environ.get("PI_CODING_AGENT_DIR") or home / ".pi/agent").expanduser()
    if not agent_dir.is_absolute():
        raise ValueError("PI_CODING_AGENT_DIR must be absolute")
    extension = agent_dir / "extensions/subagent-monitor.ts"
    skill_source = source / ".copilot/skills/subagent-monitor/SKILL.md"
    extension_source = source / ".pi/extensions/subagent-monitor.ts"
    if not skill_source.is_file() or not extension_source.is_file():
        raise ValueError("Install from the dotfiles source checkout, not the installed runtime")
    managed = "// Managed by pi-subagent-monitor."
    if extension.exists() and not extension.read_text().startswith(managed):
        raise FileExistsError(f"Preserving unmanaged extension: {extension}")
    wrapper = home / ".local/bin/pi-subagent-monitor"
    if wrapper.exists() and "# Managed by pi-subagent-monitor." not in wrapper.read_text():
        raise FileExistsError(f"Preserving unmanaged command: {wrapper}")
    skills = [home / relative / "subagent-monitor/SKILL.md" for relative in (".agents/skills", ".copilot/skills")]
    for target in skills:
        if target.exists() and "<!-- Managed by pi-subagent-monitor. -->" not in target.read_text():
            raise FileExistsError(f"Preserving existing skill: {target}")
    plist = home / "Library/LaunchAgents" / f"{LABEL}.plist"
    if plist.exists():
        existing_plist = plistlib.loads(plist.read_bytes())
        if existing_plist.get("Comment") != "Managed by pi-subagent-monitor.":
            raise FileExistsError(f"Preserving unmanaged LaunchAgent: {plist}")
    runtime.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(__file__).resolve(), runtime / "pi_subagent_monitor.py")
    extension.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(extension_source, extension)
    for target in skills:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.resolve() != skill_source.resolve():
            shutil.copy2(skill_source, target)
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text("#!/bin/sh\n# Managed by pi-subagent-monitor.\nexec " + shlex.join([
        sys.executable, "-I", str(runtime / "pi_subagent_monitor.py")]) + ' "$@"\n')
    wrapper.chmod(0o755)
    logs = home / "Library/Logs/pi-subagent-monitor"
    logs.mkdir(parents=True, exist_ok=True)
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps({
        "Label": LABEL, "Comment": "Managed by pi-subagent-monitor.", "ProgramArguments": [sys.executable, "-I", str(runtime / "pi_subagent_monitor.py"),
                                              "--state-root", str(state_root), "poll"],
        "RunAtLoad": True, "StartInterval": 60,
        "EnvironmentVariables": {"PATH": f"{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        "StandardOutPath": str(logs / "stdout.log"), "StandardErrorPath": str(logs / "stderr.log"),
    }))
    if bootstrap:
        domain = f"gui/{os.getuid()}"
        existing = subprocess.run(["launchctl", "print", f"{domain}/{LABEL}"], check=False, capture_output=True)
        if existing.returncode == 0:
            subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], check=True, capture_output=True)
        subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True, capture_output=True)
    return {"runtime": str(runtime), "plist": str(plist), "state_root": str(state_root)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, default=Path.home() / ".local/state/pi-subagent-monitor")
    commands = parser.add_subparsers(dest="command", required=True)
    registration = commands.add_parser("register")
    registration.add_argument("--manifest", type=Path, required=True)
    registration.add_argument("--parent-session", type=Path, required=True)
    polling = commands.add_parser("poll")
    polling.add_argument("--no-notify", action="store_true")
    commands.add_parser("status")
    reporting = commands.add_parser("report")
    reporting.add_argument("--artifacts", type=Path, required=True)
    reporting.add_argument("--state", choices=sorted(REPORT_STATES), required=True)
    reporting.add_argument("--summary", required=True)
    reporting.add_argument("--pr-url", default="")
    reporting.add_argument("--evidence", type=Path)
    reporting.add_argument("--validation", choices=["passed", "pending"], default="pending")
    reporting.add_argument("--ci", choices=["passed", "pending", "not_applicable"], default="pending")
    installing = commands.add_parser("install")
    installing.add_argument("--home", type=Path, default=Path.home())
    installing.add_argument("--no-bootstrap", action="store_true")
    args = parser.parse_args()
    root = args.state_root.expanduser().resolve()
    try:
        if args.command == "register":
            result = register(root, args.manifest, args.parent_session)
        elif args.command == "poll":
            result = poll(root, notifications=not args.no_notify)
        elif args.command == "status":
            result = load(root / "status.json", [])
        elif args.command == "report":
            result = report(args.artifacts, args.state, args.summary, pr_url=args.pr_url, evidence=args.evidence,
                            validation=args.validation, ci=args.ci)
        else:
            result = install(args.home.expanduser().resolve(), root, bootstrap=not args.no_bootstrap)
        print(json.dumps(result, indent=2))
    except (OSError, TypeError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"pi-subagent-monitor: {error}\n")


if __name__ == "__main__":
    main()
