"""Share the standard-library GitHub adapter with the worker and startup reporter."""

from __future__ import annotations

import base64
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any


class AutomationError(Exception):
    """Stop an action when configuration or GitHub evidence is incomplete."""


def _diagnostics(stdout: Any, stderr: Any) -> str:
    streams = []
    for name, output in (("stdout", stdout), ("stderr", stderr)):
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        if output and output.strip():
            streams.append(f"{name}: {output.strip()}")
    return "\n".join(streams)


def run_command(command, *, input=None, timeout=90):
    with subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            # Descendants can keep captured pipes open after the parent exits.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            exc.stdout, exc.stderr = process.communicate()
            raise
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


class GitHub:
    """Preserve structured JSON responses through gh-axi's text renderer."""

    def request(
        self, method: str, path: str, payload: Any = None, expression: str = "."
    ) -> Any:
        executable = (
            Path(__file__).resolve().parents[1]
            / ".dependabot-runtime/node/node_modules/.bin/gh-axi"
        )
        launcher = (
            [str(executable)]
            if executable.is_file()
            else [
                "gh",
                "cfs",
                "--",
                "npx",
                "-y",
                "gh-axi@0.1.35",
            ]
        )
        command = launcher + [
            "api",
            "--hostname",
            "github.com",
            method,
            path,
            "--jq",
            f"({expression}) | @base64",
            "--full",
        ]
        if payload is not None:
            command += ["--input", "-"]
        attempts = 3 if method == "GET" else 1
        failures = []
        for attempt in range(1, attempts + 1):
            try:
                result = run_command(
                    command,
                    input=json.dumps(payload) if payload is not None else None,
                )
            except subprocess.TimeoutExpired as exc:
                detail = (
                    f"timed out after {exc.timeout} seconds\n"
                    f"{_diagnostics(exc.stdout, exc.stderr)}"
                )
            else:
                if result.returncode == 0:
                    break
                detail = (
                    f"exit status {result.returncode}\n"
                    f"{_diagnostics(result.stdout, result.stderr)}"
                )
            failures.append(f"attempt {attempt}: {detail.strip()}")
            if attempt == attempts:
                raise AutomationError(f"{method} {path}: " + "\n".join(failures))
            # A write can succeed before its response is lost, so only GET retries.
            time.sleep(attempt)
        match = re.search(r"^\s*body:\s*(.+)$", result.stdout, re.MULTILINE)
        if not match or re.search(r"truncated:\s*true", result.stdout):
            raise AutomationError(f"invalid or truncated API response: {method} {path}")
        text = match.group(1).strip()
        encoded = json.loads(text) if text.startswith('"') else text
        return json.loads(base64.b64decode(encoded, validate=True))

    def pages(self, path: str, expression: str = ".") -> list[dict[str, Any]]:
        records = []
        separator = "&" if "?" in path else "?"
        page = 1
        while True:
            batch = self.request(
                "GET",
                f"{path}{separator}per_page=100&page={page}",
                expression=expression,
            )
            if not isinstance(batch, list) or any(
                not isinstance(item, dict) for item in batch
            ):
                raise AutomationError(f"expected API list: {path}")
            records.extend(batch)
            if len(batch) < 100:
                return records
            page += 1
