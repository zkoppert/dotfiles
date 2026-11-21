"""Publish private reports and send one deduplicated, clickable macOS help alert."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dependabot_api import AutomationError, GitHub
from dependabot_scope import confirmed_scope, in_scope

LOG = logging.getLogger("dependabot-automerge")
GROUP = "dependabot-automerge-help"


class AlertHistoryError(OSError):
    """Notification history could not be saved, so delivery cannot be deduplicated."""


def save(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as output:
        temporary = Path(output.name)
        json.dump(payload, output, indent=2)
        output.write("\n")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def held_decisions(report: dict[str, Any]) -> list[dict[str, Any]]:
    scope = confirmed_scope(report)
    if scope is None:
        return []
    return [
        item
        for item in [*report.get("previous_holds", []), *report.get("decisions", [])]
        if item["action"] == "hold" and in_scope(item["repo"], scope)
    ]


def actionable(report: dict[str, Any]) -> dict[str, tuple[str, str]]:
    items = {}
    for decision in held_decisions(report):
        key = f"{decision['repo']}#{decision['number']}@{decision['sha']}"
        security = decision.get("security_review") or {}
        fingerprint = json.dumps(
            [
                decision["sha"],
                decision["reason"],
                security.get("summary"),
                security.get("findings"),
            ],
            sort_keys=True,
        )
        url = decision.get("url", "")
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "github.com":
            url = ""
        items[key] = (hashlib.sha256(fingerprint.encode()).hexdigest(), url)
    for error in [*report.get("previous_errors", []), *report.get("errors", [])]:
        digest = hashlib.sha256(error.encode()).hexdigest()
        items["error:" + digest] = (digest, "")
    return items


def incomplete_observation(key: str, report: dict[str, Any]) -> bool:
    identity = key.partition("@")[0]
    return bool(report.get("errors")) and (
        identity in report.get("failed_prs", [])
        or identity.rsplit("#", 1)[0] not in report.get("completed_repositories", [])
    )


def report_markdown(report: dict[str, Any]) -> str:
    stamp = report.get("finished_at") or time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
    )
    if not re.fullmatch(r"[0-9T:Z+.\-]+", stamp):
        raise ValueError("Invalid report timestamp")
    lines = [
        "# Dependabot needs attention",
        "",
        "Secret gists are unlisted. Anyone with this URL can read this report.",
        "GitHub retains earlier gist revisions. Anyone with this URL can read them.",
        "",
        "Each pull request (PR) needs passing continuous integration (CI) checks and security review before an automatic merge.",
        "",
        "This report shows the last published alert, not a live scan. Earlier snapshots stay on the Mac.",
        "",
        f"Report time: `{stamp}`",
        "",
        "## Review blocked updates",
        "",
    ]
    held = {}
    for item in held_decisions(report):
        held[(item["repo"], item["number"])] = item
    for index, item in enumerate(held.values(), 1):
        repo, number = item["repo"], item["number"]
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*/\.?[A-Za-z0-9][A-Za-z0-9_.-]*", repo
        ):
            raise ValueError("Invalid report repository")
        if type(number) is not int or number < 1:
            raise ValueError("Invalid report pull request")
        reason = item["reason"]
        if reason.startswith("CI checks absent"):
            explanation = (
                "No successful CI checks were reported for the current PR revision."
            )
            step = "Find why no check succeeded, then obtain a passing check."
        elif reason.startswith("CI checks failing"):
            explanation = (
                "At least one CI check failed or reported an unsupported conclusion."
            )
            step = (
                "Inspect the failed checks and obtain passing results before approval."
            )
        elif reason.startswith("CI checks"):
            explanation = "The required CI checks have not finished successfully."
            step = "Wait for the checks and inspect any failures before approval."
        elif reason.startswith("human"):
            explanation = "Human activity or requested changes stopped automation."
            step = "Review the discussion and address the human feedback."
        elif reason.startswith("security review unavailable"):
            explanation = "The dependency security review could not finish."
            step = "Inspect the private diagnostics and complete the upstream review."
        elif reason.startswith("security review:"):
            explanation = "The dependency security review requires manual attention."
            step = "Inspect the recorded findings before approving the update."
        elif reason == "existing deferred auto-merge needs human attention":
            explanation = "An existing auto-merge request prevents the handler from taking action."
            step = "Inspect the auto-merge setting and resolve the hold manually."
        elif reason == "waiting for branch policy after approval":
            explanation = "GitHub branch protections are not yet satisfied."
            step = "Review required approvals and branch rules. Keep branch protection enabled."
        elif reason == "dependency excluded by existing safety policy":
            explanation = "The dependency is excluded by a separate safety policy."
            step = "Review that policy manually before taking action."
        elif reason == "prerelease or unknown upgrade":
            explanation = "The upgrade could not be classified as a stable release."
            step = "Verify the target version and review compatibility manually."
        else:
            explanation = "The update did not satisfy the automation policy."
            step = (
                "Inspect the pull request and private diagnostics before taking action."
            )
        url = f"https://github.com/{repo}/pull/{number}"
        lines += [f"### {index}. `{repo}` #{number}", "", explanation, "", step, ""]
        if item.get("url") == url:
            lines += [f"[Open the pull request]({url})", ""]
    if not held:
        if confirmed_scope(report) is not None:
            lines += [
                "No pull requests currently require attention in this report.",
                "",
            ]
        else:
            lines += [
                "PR details are withheld until the current repository scope is confirmed.",
                "",
            ]
    errors = list(
        dict.fromkeys([*report.get("previous_errors", []), *report.get("errors", [])])
    )
    lines += ["## Check scan problems", ""]
    if errors:
        for error in errors:
            lower = error.lower()
            if "enetunreach" in lower or "timed out" in lower or "network" in lower:
                text = "The diagnostics suggest a network or package-feed problem. Check connectivity and retry."
            elif (
                "authentication" in lower
                or "permission" in lower
                or "401" in lower
                or "403" in lower
            ):
                text = "The diagnostics suggest an access problem. Check GitHub authentication and repository permissions."
            elif "bootstrap failure" in lower:
                text = "The scheduled worker could not start. Check its runtime and package-feed access."
            else:
                text = "The scan could not finish a request. Inspect the private diagnostics before retrying."
            lines += [f"- {text}"]
        lines += [""]
    else:
        lines += ["The scan reported no worker errors.", ""]
    lines += [
        "## Find private diagnostics",
        "",
        "Detailed reasons remain in `~/Library/Logs/dependabot-automerge.json`.",
        "The diagnostic log is `~/Library/Logs/triage-dependabot.log`.",
        "",
        "The full repository scope, raw logs, review findings, and source patches are not uploaded.",
        "No approval or merge safeguard is disabled by this report.",
        "",
    ]
    return "\n".join(lines)


def lint_report(body: str) -> None:
    try:
        subprocess.run(
            [
                sys.executable,
                str(Path.home() / ".copilot/skills/validate-style/lint.py"),
                "-",
            ],
            input=body,
            text=True,
            capture_output=True,
            timeout=30,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        LOG.error(
            "Help report style validation failed: %s\n%s",
            exc.stdout or "",
            exc.stderr or "",
        )
        raise


def publish_gist(report: dict[str, Any], report_path: Path) -> str:
    body = report_markdown(report)
    if len(body.encode()) > 128000:
        raise ValueError("The help report is too large to publish safely")
    lint_report(body)
    client = GitHub()
    owner = client.request("GET", "/user")["login"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", owner):
        raise ValueError("Invalid report account identity")
    state_path = report_path.with_suffix(".gist.json")
    if state_path.exists():
        state = json.loads(state_path.read_text())
    else:
        state = {"owner": owner, "marker": uuid.uuid4().hex, "id": None}
        save(state_path, state)
    if state["owner"] != owner or not re.fullmatch(r"[0-9a-f]{32}", state["marker"]):
        raise ValueError("The report gist state does not match the current account")
    description = f"Dependabot help report [{state['marker']}]"
    filename = "dependabot-help.md"

    def verify(gist):
        identity = gist["id"]
        if not re.fullmatch(r"[0-9a-f]{20,64}", identity):
            raise ValueError("Invalid report gist identity")
        url = f"https://gist.github.com/{owner}/{identity}"
        if (
            gist["public"] is not False
            or (state["id"] and identity != state["id"])
            or gist["owner"]["login"] != owner
            or gist["description"] != description
        ):
            raise ValueError(
                "The help gist must be secret, owned, and managed by this handler"
            )
        return url

    if not state["id"]:
        matches = [
            g for g in client.pages("/gists") if g.get("description") == description
        ]
        if len(matches) > 1:
            raise ValueError("More than one managed help gist needs manual inspection")
        gist = (
            matches[0]
            if matches
            else client.request(
                "POST",
                "/gists",
                {
                    "public": False,
                    "description": description,
                    "files": {filename: {"content": body}},
                },
            )
        )
        verify(gist)
        state["id"] = gist["id"]
        save(state_path, state)
    if not re.fullmatch(r"[0-9a-f]{20,64}", state["id"]):
        raise ValueError("Invalid cached report gist identity")
    path = f"/gists/{state['id']}"
    published = client.request("GET", path)
    verify(published)
    if published.get("files", {}).get(filename, {}).get("content") != body:
        client.request("PATCH", path, {"files": {filename: {"content": body}}})
        published = client.request("GET", path)
    url = verify(published)
    file = published["files"][filename]
    if published.get("truncated") or file.get("truncated") or file["content"] != body:
        raise ValueError("The published gist differs from the rendered report")
    return url


def send_alert(message: str, destination: str = "") -> None:
    binary = "/opt/homebrew/bin/terminal-notifier"
    executable = binary if Path(binary).exists() else shutil.which("terminal-notifier")
    if not executable:
        raise OSError("terminal-notifier is unavailable; help alert was not delivered")
    subprocess.run(
        [
            executable,
            "-title",
            "Dependabot needs attention",
            "-message",
            message,
            "-group",
            GROUP,
            *(["-open", destination] if destination else []),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )


def notify(report: dict[str, Any], report_path: Path) -> None:
    state_path = report_path.with_suffix(".alerts.json")
    try:
        previous = (
            json.loads(state_path.read_text())
            if state_path.exists() and not report.get("alert_history_invalid")
            else {}
        )
        if not isinstance(previous, dict):
            raise TypeError("alert state must be a mapping")
    except (TypeError, ValueError, OSError) as exc:
        LOG.warning("Alert history unavailable; retrying actionable alerts: %s", exc)
        previous = {}
    items = actionable(report)
    pending = [value for key, value in items.items() if previous.get(key) != value[0]]
    scope = confirmed_scope(report)
    observed = (
        {
            f"{item['repo']}#{item['number']}": item["sha"]
            for item in report.get("decisions", [])
        }
        if scope is not None
        else {}
    )
    delivered = {}
    for key, value in previous.items():
        identity, _, revision = key.partition("@")
        if key.startswith("error:") or observed.get(identity, revision) != revision:
            continue
        if scope is not None and not in_scope(identity.rsplit("#", 1)[0], scope):
            continue
        if scope is None or (
            identity not in observed and incomplete_observation(key, report)
        ):
            delivered[key] = value
    current_errors = actionable({"errors": report.get("errors", [])})
    updated = {
        key: value[0]
        for key, value in items.items()
        if not key.startswith("error:") or key in current_errors
    }
    delivered.update(
        {key: value for key, value in updated.items() if previous.get(key) == value}
    )
    # Retire resolved entries even if delivery of a different issue fails.
    try:
        save(state_path, delivered)
        report["alert_history_invalid"] = False
    except OSError:
        report["alert_history_invalid"] = True
        LOG.warning("Unable to retire alert history before delivery")
    if pending:
        snapshot = {
            key: value for key, value in report.items() if key != "notification_pending"
        }
        digest = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True).encode()
        ).hexdigest()
        directory = report_path.parent / f"{report_path.stem}-alerts"
        directory.mkdir(mode=0o700, exist_ok=True)
        target = directory / f"{digest}.json"
        save(target, snapshot)
        try:
            destination = publish_gist(snapshot, report_path)
        except (
            AutomationError,
            OSError,
            KeyError,
            TypeError,
            ValueError,
            subprocess.SubprocessError,
        ) as exc:
            LOG.exception("Unable to publish the secret help gist")
            raise OSError(
                "Secret gist publication failed; inspect the diagnostic log"
            ) from exc
        report["gist_url"] = destination
        quantity = "item needs" if len(pending) == 1 else "items need"
        message = f"{len(pending)} new or changed {quantity} attention. Select to open the details."
        report["notification_attempted"] = True
        send_alert(message, destination)
    # Only successful delivery can acknowledge newly observed problems.
    delivered.update(updated)
    try:
        save(state_path, delivered)
        report["alert_history_invalid"] = False
    except OSError as exc:
        report["alert_history_invalid"] = True
        raise AlertHistoryError(str(exc)) from exc


def publish(report: dict[str, Any], path: Path) -> None:
    report["run_receipt"] = os.environ.get("DEPENDABOT_WORKER_STARTED")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            path.with_suffix(".publish.lock"), os.O_CREAT | os.O_RDWR, 0o600
        )
        with os.fdopen(descriptor, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            _publish(report, path)
            if report.get("errors") and (
                receipt := os.environ.get("DEPENDABOT_FAILURE_REPORTED")
            ):
                try:
                    Path(receipt).touch(mode=0o600)
                except OSError:
                    LOG.warning(
                        "The failure report was saved, but its startup receipt could not be written"
                    )
    except OSError:
        try:
            emergency_help(report)
        except (OSError, ValueError, subprocess.SubprocessError):
            LOG.exception("The emergency help alert also failed")
        raise


def emergency_help(
    report: dict[str, Any],
    message: str = "Dependabot couldn't save its report. Check ~/Library/Logs/triage-dependabot.log.",
) -> None:
    receipt = os.environ.get("DEPENDABOT_FALLBACK_ALERT_SENT")
    if report.get("fallback_alert_sent") or (receipt and Path(receipt).exists()):
        report["fallback_alert_sent"] = True
        return
    send_alert(message)
    report["fallback_alert_sent"] = True
    if receipt:
        try:
            Path(receipt).touch(mode=0o600)
        except OSError:
            LOG.warning("Unable to record the emergency alert receipt")


def _publish(report: dict[str, Any], path: Path) -> None:
    report.pop("previous_errors", None)
    report["notification_attempted"] = False
    try:
        previous = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(previous, dict):
            raise TypeError("previous report must be a mapping")
        if previous.get("alert_history_invalid"):
            report["alert_history_invalid"] = True
        if previous.get("notification_pending"):
            errors = previous.get("previous_errors", []) + previous.get("errors", [])
            if not isinstance(errors, list) or any(
                not isinstance(item, str) for item in errors
            ):
                raise TypeError("previous errors must be a list of strings")
            report["previous_errors"] = list(
                dict.fromkeys(
                    error
                    for error in errors
                    if not error.startswith(
                        ("macOS help alert failed:", "help report publication failed:")
                    )
                )
            )
        observed = {
            (item["repo"], item["number"]): item for item in report.get("decisions", [])
        }
        excluded = report.get(
            "excluded_repositories", previous.get("excluded_repositories", [])
        )
        report["excluded_repositories"] = excluded
        scope = confirmed_scope(report)
        retained = {}
        for held in [
            *previous.get("previous_holds", []),
            *previous.get("decisions", []),
        ]:
            if not isinstance(held, dict):
                raise TypeError("previous decisions must be mappings")
            if held.get("action") != "hold" or (
                scope is not None and not in_scope(held["repo"], scope)
            ):
                continue
            key = (held["repo"], held["number"])
            current = observed.get(key)
            if (
                scope is None
                or (
                    current
                    and current["sha"] == held["sha"]
                    and current["action"] in {"wait", "rebase"}
                    and held["reason"] != "rebase already requested for this revision"
                )
                or (
                    current is None
                    and incomplete_observation(f"{key[0]}#{key[1]}", report)
                )
            ):
                retained[key] = held
        report["previous_holds"] = list(retained.values())
    except (OSError, TypeError, ValueError, KeyError) as exc:
        report["errors"].append(f"previous alert report unavailable: {exc}")
    report["notification_pending"] = True
    history_invalid = report.get("alert_history_invalid", False)
    # A stopped publication cannot prove which history changes reached disk.
    report["alert_history_invalid"] = True
    save(path, report)
    report["alert_history_invalid"] = history_invalid
    try:
        notify(report, path)
        report["notification_pending"] = False
    except AlertHistoryError as exc:
        message = f"alert history persistence failed: {exc}"
        LOG.error(message)
        report["errors"].append(message)
        if not report["notification_attempted"]:
            report["notification_attempted"] = True
            try:
                send_alert(
                    "Dependabot couldn't save alert history. Check ~/Library/Logs/triage-dependabot.log.",
                )
            except (OSError, ValueError, subprocess.SubprocessError) as delivery_error:
                report["errors"].append(f"macOS help alert failed: {delivery_error}")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        stage = (
            "macOS help alert failed"
            if report["notification_attempted"]
            else "help report publication failed"
        )
        message = f"{stage}: {exc}"
        LOG.error(message)
        report["errors"].append(message)
        if not report["notification_attempted"]:
            report["notification_attempted"] = True
            if any(
                error.startswith("bootstrap failure:") for error in report["errors"]
            ):
                fallback = "Dependabot couldn't start or publish its help report."
            elif any(error.startswith("worker failure:") for error in report["errors"]):
                fallback = "Dependabot stopped and couldn't publish its help report."
            else:
                fallback = "Dependabot couldn't publish its help report."
            try:
                emergency_help(
                    report, f"{fallback} Check ~/Library/Logs/triage-dependabot.log."
                )
            except (OSError, ValueError, subprocess.SubprocessError) as delivery_error:
                report["errors"].append(f"macOS help alert failed: {delivery_error}")
    save(path, report)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Report a dependency-runtime startup failure without third-party packages."
    )
    parser.add_argument("--bootstrap-error", type=int, required=True)
    parser.add_argument("--worker-started", action="store_true")
    parser.add_argument(
        "--report",
        type=Path,
        default=Path.home() / "Library/Logs/dependabot-automerge.json",
    )
    args = parser.parse_args(argv)
    unfinished = None
    if args.worker_started and (receipt := os.environ.get("DEPENDABOT_WORKER_STARTED")):
        try:
            existing = json.loads(args.report.read_text())
            if (
                isinstance(existing, dict)
                and existing.get("run_receipt") == receipt
                and existing.get("errors")
            ):
                if not existing.get("notification_pending") or existing.get(
                    "fallback_alert_sent"
                ):
                    return 0
                unfinished = existing
        except (OSError, ValueError):
            pass
    runtime = Path(__file__).resolve().parents[1]
    failure = (
        f"worker failure: runtime {runtime.name} exited {args.bootstrap_error} without a failure report"
        if args.worker_started
        else f"bootstrap failure: runtime {runtime.name} exited {args.bootstrap_error} before worker startup"
    )
    report = {
        "decisions": [],
        "errors": [failure],
        "runtime": str(runtime),
        "diagnostic_log": (Path.home() / "Library/Logs/triage-dependabot.log").as_uri(),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if unfinished is not None:
        report = unfinished
        report["errors"].append(
            f"worker failure: runtime {runtime.name} exited {args.bootstrap_error} with unfinished notification delivery"
        )
        report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    report["scope_confirmed"] = False
    try:
        publish(report, args.report)
    except OSError:
        if report.get("fallback_alert_sent"):
            return 0
        raise
    return (
        1
        if report.get("notification_pending") and not report.get("fallback_alert_sent")
        else 0
    )


if __name__ == "__main__":
    sys.exit(main())
