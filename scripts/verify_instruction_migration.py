#!/usr/bin/env python3
"""Verify lossless global-instruction migration into scoped skills."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RUNTIME_BASENAMES = {"AGENTS.md", "CLAUDE.md", "SKILL.md", "copilot-instructions.md"}
MIGRATION_BOUNDARY = "<!-- This boundary keeps the migrated baseline block byte-verifiable. -->\n"


def sha256(text: str) -> str:
    """Return a stable UTF-8 digest."""
    return hashlib.sha256(text.encode()).hexdigest()


def parse_h2_blocks(text: str) -> list[tuple[str, str]]:
    """Split Markdown at H2 headings outside fenced code blocks."""
    lines = text.splitlines(keepends=True)
    starts: list[tuple[int, str]] = []
    in_fence = False
    fence_marker = ""
    offset = 0
    for line in lines:
        stripped = line.lstrip()
        fence = re.match(r"(`{3,}|~{3,})", stripped)
        if fence:
            marker = fence.group(1)[0]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
                fence_marker = ""
        if not in_fence:
            heading = re.match(r"^## ([^\n]+)\n?$", line)
            if heading:
                starts.append((offset, heading.group(1)))
        offset += len(line)
    if in_fence:
        raise ValueError("baseline contains an unclosed fenced code block")
    if not starts:
        raise ValueError("baseline contains no H2 headings")

    blocks = [("preamble", text[: starts[0][0]])]
    for index, (start, name) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(text)
        blocks.append((name, text[start:end]))
    return blocks


def parse_frontmatter(path: Path) -> dict[str, str]:
    """Read the simple scalar fields used by Agent Skills frontmatter."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError("missing opening frontmatter delimiter")
    try:
        frontmatter, _ = text[4:].split("\n---\n", 1)
    except ValueError as error:
        raise ValueError("missing closing frontmatter delimiter") from error
    values: dict[str, str] = {}
    for line in frontmatter.splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            raise ValueError(f"unsupported frontmatter line: {line}")
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip()
    return values


def load_manifest(path: Path) -> dict[str, Any]:
    """Load a migration manifest."""
    return json.loads(path.read_text(encoding="utf-8"))


def destination_blocks(root: Path, relative_path: str) -> list[tuple[str, str]]:
    """Read one runtime destination as Markdown blocks."""
    return parse_h2_blocks((root / relative_path).read_text(encoding="utf-8"))


def migrated_content(content: str) -> str:
    """Remove the non-guidance marker that follows a migrated block."""
    if content.endswith(MIGRATION_BOUNDARY):
        return content[: -len(MIGRATION_BOUNDARY)]
    return content


def update_baseline(root: Path, manifest_path: Path, *, acknowledged: bool = False) -> list[str]:
    """Rebuild reviewed hashes after an acknowledged guidance edit."""
    if not acknowledged:
        raise ValueError("rebaselining requires explicit guidance-change acknowledgement")
    manifest = load_manifest(manifest_path)
    baseline_path = root / manifest["baseline"]["path"]
    previous_blocks = dict(parse_h2_blocks(baseline_path.read_text(encoding="utf-8")))
    changed: list[str] = []
    cache: dict[str, dict[str, str]] = {}

    def blocks_for(relative_path: str) -> dict[str, str]:
        if relative_path not in cache:
            parsed = destination_blocks(root, relative_path)
            names = [name for name, _ in parsed]
            if len(names) != len(set(names)):
                raise ValueError(f"duplicate H2 block identifiers in {relative_path}")
            cache[relative_path] = dict(parsed)
        return cache[relative_path]

    content_parts: list[str] = []
    line = 1
    for entry in manifest["blocks"]:
        identifier = entry["id"]
        content = blocks_for(entry["destination"]).get(identifier)
        if content is None:
            raise ValueError(f"cannot rebaseline missing block {identifier!r}")
        content = migrated_content(content)
        content_parts.append(content)
        if previous_blocks.get(identifier) != content:
            changed.append(f"baseline:{identifier}")
        entry["sha256"] = sha256(content)
        entry["source_start_line"] = line
        line_count = content.count("\n")
        entry["source_end_line"] = line + line_count - 1 if content.endswith("\n") else line + line_count
        line += line_count

    baseline_text = "".join(content_parts)
    baseline_path.write_text(baseline_text, encoding="utf-8")
    manifest["baseline"]["sha256"] = sha256(baseline_text)

    for contract in manifest.get("runtime_contracts", []):
        content = blocks_for(contract["destination"]).get(contract["id"])
        if content is None:
            raise ValueError(f"cannot rebaseline missing runtime contract {contract['id']!r}")
        new_hash = sha256(content)
        if contract["sha256"] != new_hash:
            changed.append(f"runtime:{contract['id']}")
        contract["sha256"] = new_hash

    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return changed


def validate(root: Path, manifest_path: Optional[Path] = None) -> list[str]:
    """Return every preservation or reachability error."""
    errors: list[str] = []
    manifest_path = manifest_path or root / "tests/fixtures/instruction-migration/manifest.json"
    manifest = load_manifest(manifest_path)
    baseline_config = manifest["baseline"]
    baseline_path = root / baseline_config["path"]

    relative_baseline = baseline_path.relative_to(root)
    if relative_baseline.name in RUNTIME_BASENAMES:
        errors.append(f"baseline uses a runtime filename: {relative_baseline}")
    if relative_baseline.parts[0] in {".github", ".copilot", ".agents"}:
        errors.append(f"baseline is inside a runtime discovery path: {relative_baseline}")

    baseline_text = baseline_path.read_text(encoding="utf-8")
    if sha256(baseline_text) != baseline_config["sha256"]:
        errors.append("baseline SHA-256 does not match the manifest")

    try:
        parsed_blocks = parse_h2_blocks(baseline_text)
    except ValueError as error:
        return errors + [str(error)]
    parsed = {name: content for name, content in parsed_blocks}
    if len(parsed) != len(parsed_blocks):
        errors.append("baseline has duplicate H2 block identifiers")

    entries = manifest["blocks"]
    entry_ids = [entry["id"] for entry in entries]
    if len(entry_ids) != len(set(entry_ids)):
        errors.append("manifest has duplicate block identifiers")
    if set(entry_ids) != set(parsed):
        missing = sorted(set(parsed) - set(entry_ids))
        extra = sorted(set(entry_ids) - set(parsed))
        errors.append(f"manifest block coverage differs: missing={missing}, extra={extra}")

    runtime_paths = {root / entry["destination"] for entry in entries}
    runtime_paths.update(root.glob(".copilot/skills/*/SKILL.md"))
    runtime_paths.add(root / ".github/copilot-instructions.md")
    runtime_blocks: dict[Path, list[tuple[str, str]]] = {}
    for path in sorted(runtime_paths):
        if not path.is_file():
            continue
        try:
            blocks = parse_h2_blocks(path.read_text(encoding="utf-8"))
        except ValueError as error:
            errors.append(f"runtime instructions cannot be parsed in {path.relative_to(root)}: {error}")
            continue
        names = [name for name, _ in blocks]
        if len(names) != len(set(names)):
            errors.append(f"runtime instructions have duplicate H2 identifiers: {path.relative_to(root)}")
        runtime_blocks[path] = blocks

    destination_positions: dict[Path, list[tuple[int, int, str]]] = {}
    for order, entry in enumerate(entries):
        identifier = entry["id"]
        content = parsed.get(identifier)
        if content is None:
            continue
        if sha256(content) != entry["sha256"]:
            errors.append(f"source block hash changed: {identifier}")
        destination = root / entry["destination"]
        if not destination.is_file():
            errors.append(f"destination is missing: {entry['destination']}")
            continue
        destination_matches = [
            index
            for index, (name, candidate) in enumerate(runtime_blocks.get(destination, []))
            if name == identifier and migrated_content(candidate) == content
        ]
        if len(destination_matches) != 1:
            errors.append(
                f"{identifier} has {len(destination_matches)} structural matches in "
                f"{entry['destination']}, expected 1"
            )
        all_count = sum(
            1
            for blocks in runtime_blocks.values()
            for name, candidate in blocks
            if name == identifier and migrated_content(candidate) == content
        )
        if all_count != 1:
            errors.append(f"{identifier} has {all_count} structural matches across runtime instructions, expected 1")
        if destination_matches:
            destination_positions.setdefault(destination, []).append(
                (order, destination_matches[0], identifier)
            )

    for destination, positions in destination_positions.items():
        expected = [identifier for _, _, identifier in sorted(positions)]
        actual = [identifier for _, _, identifier in sorted(positions, key=lambda item: item[1])]
        if expected != actual:
            errors.append(f"baseline block order changed in {destination.relative_to(root)}")

    global_text = (root / ".github/copilot-instructions.md").read_text(encoding="utf-8")
    external_skills = set(manifest.get("external_skills", []))
    requirements = manifest.get("skill_requirements", {})
    repo_skill_names: set[str] = set()
    for skill_path in sorted(root.glob(".copilot/skills/*/SKILL.md")):
        directory_name = skill_path.parent.name
        try:
            fields = parse_frontmatter(skill_path)
        except ValueError as error:
            errors.append(f"{skill_path.relative_to(root)}: {error}")
            continue
        name = fields.get("name", "")
        description = fields.get("description", "")
        if name != directory_name:
            errors.append(f"skill name {name!r} does not match directory {directory_name!r}")
        if not SKILL_NAME.fullmatch(name) or len(name) > 64:
            errors.append(f"invalid skill name: {name!r}")
        if not description or len(description) > 1024:
            errors.append(f"invalid skill description length for {name!r}")
        if fields.get("disable-model-invocation", "").lower() == "true":
            errors.append(f"routed skill disables model invocation: {name}")
        if name in repo_skill_names:
            errors.append(f"duplicate repository skill name: {name}")
        repo_skill_names.add(name)
        for term in requirements.get(name, []):
            if term.casefold() not in description.casefold():
                errors.append(f"skill description for {name} lacks trigger term {term!r}")

    for name in manifest["routed_skills"]:
        if f"`{name}`" not in global_text:
            errors.append(f"global routing table does not name skill: {name}")
        if name not in repo_skill_names and name not in external_skills:
            errors.append(f"routed skill is not discoverable from this repository: {name}")

    contract_cache: dict[str, dict[str, str]] = {}
    for contract in manifest.get("runtime_contracts", []):
        relative_path = contract["destination"]
        if relative_path not in contract_cache:
            try:
                parsed = destination_blocks(root, relative_path)
            except ValueError as error:
                errors.append(f"runtime contracts cannot be parsed in {relative_path}: {error}")
                contract_cache[relative_path] = {}
            else:
                names = [name for name, _ in parsed]
                if len(names) != len(set(names)):
                    errors.append(f"runtime destination has duplicate H2 identifiers: {relative_path}")
                contract_cache[relative_path] = dict(parsed)
        identifier = contract["id"]
        content = contract_cache[relative_path].get(identifier)
        if content is None:
            errors.append(f"runtime contract is missing: {identifier}")
        elif sha256(content) != contract["sha256"]:
            errors.append(f"runtime contract changed: {identifier}")

    return errors


def verify_source(manifest: dict[str, Any], source: Path) -> list[str]:
    """Verify that the external source did not change after capture."""
    if not source.is_file():
        return [f"source file is missing: {source}"]
    actual = sha256(source.read_text(encoding="utf-8"))
    expected = manifest["baseline"]["sha256"]
    return [] if actual == expected else [f"source SHA-256 changed: expected {expected}, got {actual}"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--source", type=Path, help="also verify the original working-copy source")
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="rebuild baseline and contract hashes after an intentional reviewed guidance edit",
    )
    parser.add_argument(
        "--acknowledge-guidance-changes",
        action="store_true",
        help="confirm that the rebaseline diff was reviewed for removed or weakened guidance",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = args.root.resolve()
    manifest_path = args.manifest or root / "tests/fixtures/instruction-migration/manifest.json"
    if args.update_baseline:
        try:
            changed = update_baseline(
                root,
                manifest_path,
                acknowledged=args.acknowledge_guidance_changes,
            )
        except ValueError as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 1
        print("Updated guidance contracts: " + (", ".join(changed) if changed else "none"))
    errors = validate(root, manifest_path)
    if args.source:
        errors.extend(verify_source(load_manifest(manifest_path), args.source))
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        print(
            "After an intentional guidance edit, review the diff and run "
            "scripts/verify_instruction_migration.py --update-baseline "
            "--acknowledge-guidance-changes.",
            file=sys.stderr,
        )
        return 1
    print("Instruction migration preserves every baseline block and routed destination.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
