"""Bind consumer configuration and CI results to the reviewed dependency revision."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any

from dependabot_api import latest_statuses

MAX_CONTEXT_BYTES = 64_000
CONFIGURATION = {
    "Makefile",
    "Dockerfile",
    "action.yml",
    "action.yaml",
    "pyproject.toml",
    "package.json",
    "tox.ini",
    "setup.cfg",
    ".pylintrc",
    "pylintrc",
    ".github/linters/.python-lint",
}


def collect_ci_context(client: Any, repo: str, head: str) -> dict[str, Any]:
    checks = client.pages(
        f"/repos/{repo}/commits/{head}/check-runs?filter=latest", ".check_runs"
    )
    if any(check.get("head_sha") != head for check in checks):
        raise ValueError("consumer CI evidence belongs to a different revision")
    statuses = latest_statuses(client.pages(f"/repos/{repo}/commits/{head}/statuses"))
    canonical = lambda records: sorted(
        records, key=lambda item: json.dumps(item, sort_keys=True)
    )
    return {
        "head_sha": head,
        "checks": canonical(
            [
                {
                    key: check.get(key)
                    for key in ("name", "head_sha", "status", "conclusion")
                }
                for check in checks
            ]
        ),
        "statuses": canonical(
            [
                {key: status.get(key) for key in ("context", "state")}
                for status in statuses
            ]
        ),
    }


def collect_context(client: Any, repo: str, head: str) -> dict[str, Any]:
    tree = client.request("GET", f"/repos/{repo}/git/trees/{head}?recursive=1")
    if tree.get("truncated") is not False:
        raise ValueError("consumer configuration tree is incomplete")
    files = []
    total = 0
    paths = set()
    for entry in tree["tree"]:
        path = entry["path"]
        if path not in CONFIGURATION and not re.fullmatch(
            r"\.github/workflows/[^/]+\.ya?ml", path
        ):
            continue
        if (
            path in paths
            or entry["type"] != "blob"
            or entry["mode"] not in {"100644", "100755"}
        ):
            raise ValueError(
                "consumer configuration is ambiguous or not a regular file"
            )
        paths.add(path)
        size = entry["size"]
        if type(size) is not int or size < 0 or total + size > MAX_CONTEXT_BYTES:
            raise ValueError("consumer configuration exceeds the review limit")
        sha = entry["sha"]
        if not re.fullmatch(r"[a-f0-9]{40}", sha):
            raise ValueError("consumer configuration has an invalid blob identity")
        blob = client.request("GET", f"/repos/{repo}/git/blobs/{sha}")
        if (
            blob.get("encoding") != "base64"
            or blob.get("sha") != sha
            or blob.get("size") != size
        ):
            raise ValueError(
                "consumer configuration blob metadata does not match its tree"
            )
        raw = base64.b64decode("".join(blob["content"].split()), validate=True)
        actual = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
        if len(raw) != size or actual != sha:
            raise ValueError(
                "consumer configuration blob content does not match its identity"
            )
        files.append({"path": path, "blob_sha": sha, "content": raw.decode("utf-8")})
        total += size
    context = {
        **collect_ci_context(client, repo, head),
        "scope": "Selected configuration and workflow files, not a complete usage inventory.",
        "files": sorted(files, key=lambda item: item["path"]),
    }
    if len(json.dumps(context).encode()) > MAX_CONTEXT_BYTES:
        raise ValueError("complete consumer context exceeds the review limit")
    return context
