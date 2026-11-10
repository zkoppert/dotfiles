#!/usr/bin/env python3
"""Validate installed Agent Skills with Pi's YAML parser."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def pi_sdk_path() -> Path:
    executable = shutil.which("pi")
    if executable is None:
        raise ValueError("Pi executable is missing from PATH")
    wrapper = Path(__file__).resolve().parents[1] / "bin" / "pi-treehouse"
    if Path(executable).resolve() == wrapper:
        executable = os.environ.get("PI_REAL_PI") or next(
            (
                candidate
                for directory in os.get_exec_path()
                if (candidate := shutil.which("pi", path=directory or os.curdir))
                and Path(candidate).resolve() != wrapper
            ),
            None,
        )
    if (
        executable is None
        or not Path(executable).is_file()
        or not os.access(executable, os.X_OK)
        or Path(executable).resolve() == wrapper
    ):
        raise ValueError("Pi executable behind the Treehouse wrapper is unavailable")
    executable_path = Path(executable).resolve()
    prefix = executable_path.parent.parent
    package_name = "@earendil-works/pi-coding-agent"
    for root in (
        prefix,
        prefix.parent,
        prefix / "libexec" / "lib" / "node_modules" / package_name,
        prefix / "lib" / "node_modules" / package_name,
    ):
        manifest = root / "package.json"
        sdk = root / "dist" / "index.js"
        if manifest.is_file() and sdk.is_file():
            metadata = json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(metadata, dict) and metadata.get("name") == package_name:
                return sdk
    raise ValueError(f"cannot locate the installed Pi SDK for {executable_path}")


def skill_metadata_errors(skills: dict[Path, str]) -> dict[str, list[str]]:
    sdk = pi_sdk_path()
    script = r"""
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const { parseFrontmatter } = await import(pathToFileURL(process.argv[1]).href);
if (typeof parseFrontmatter !== 'function') throw new Error('Pi SDK has no frontmatter parser');
const skills = JSON.parse(readFileSync(0, 'utf8'));
const results = Object.fromEntries(Object.entries(skills).map(([path, name]) => {
    const errors = [];
    try {
        const { frontmatter } = parseFrontmatter(readFileSync(path, 'utf8'));
        if (!frontmatter || typeof frontmatter !== 'object' || Array.isArray(frontmatter)) {
            throw new Error('Skill frontmatter must be a mapping');
        }
        if (frontmatter.name !== name) errors.push('declares the wrong skill name');
        if (typeof frontmatter.description !== 'string' || !frontmatter.description.trim()) {
            errors.push('has no skill description');
        }
        if (frontmatter['disable-model-invocation'] !== undefined && frontmatter['disable-model-invocation'] !== false) {
            errors.push('is unavailable for model invocation');
        }
    } catch {
        errors.push('has invalid skill metadata');
    }
    return [path, errors];
}));
console.log(JSON.stringify(results));
"""
    requested = {str(path): name for path, name in skills.items()}
    try:
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script, str(sdk)],
            input=json.dumps(requested),
            env={**os.environ, "PI_OFFLINE": "1"},
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError("skill metadata validation requires a working Pi SDK and Node.js") from error
    errors = json.loads(result.stdout)
    if (
        not isinstance(errors, dict)
        or errors.keys() != requested.keys()
        or any(
            not isinstance(messages, list) or any(not isinstance(message, str) for message in messages)
            for messages in errors.values()
        )
    ):
        raise ValueError("Pi SDK returned invalid skill metadata results")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("name")
    args = parser.parse_args()
    try:
        errors = skill_metadata_errors({args.path: args.name})[str(args.path)]
    except (OSError, ValueError) as error:
        print(f"Cannot validate skill metadata: {error}", file=sys.stderr)
        return 1
    for error in errors:
        print(f"{args.name} {error}", file=sys.stderr)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
