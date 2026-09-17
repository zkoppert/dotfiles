#!/usr/bin/env python3
"""Exercise pr-marker and gh-guard's marker and publication boundaries.

Publication fixtures intercept the native gh command. Their marker content and
model names are test inputs, never evidence of real reviews or publication.

Run: python3 bin/test_pr_marker.py
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from importlib.machinery import SourceFileLoader
from pathlib import Path

# pr-marker has no .py extension, so point the loader at it explicitly.
_MODULE_PATH = Path(__file__).with_name("pr-marker")
_LOADER = SourceFileLoader("pr_marker", str(_MODULE_PATH))
_SPEC = importlib.util.spec_from_loader("pr_marker", _LOADER)
assert _SPEC
pr_marker = importlib.util.module_from_spec(_SPEC)
# Register before exec so @dataclass can resolve the module on Python 3.9.
sys.modules["pr_marker"] = pr_marker
_LOADER.exec_module(pr_marker)


def sed_encode(branch: str) -> str:
    """Encode a branch name exactly as gh-guard does, via the real sed pipeline."""
    result = subprocess.run(
        ["sed", "-e", "s/%/%25/g", "-e", "s|/|%2F|g"],
        input=branch,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def bash_count_models(path: str) -> int:
    """Count declared models via gh-guard's exact shell pipeline, for parity.

    Mirrors count_review_models in gh-guard *including* `set -euo pipefail` and
    the substitution-with-default guard, so the Python reader and the bash gate
    cannot silently disagree. Faithfully reproducing pipefail is what catches the
    header-less "0\\n0" fail-open class of bug: a regression to `grep -v '^$'`
    here would make this return "0\\n0" and raise in int(), failing the test.
    """
    script = (
        "set -euo pipefail\n"
        'count="$(\n'
        r"""  sed -n 's/^<!-- reviewed-by-models: \(.*\) -->$/\1/p' "$1" 2>/dev/null """
        "\\\n"
        "    | head -n1 | tr ',' '\\n' \\\n"
        r"""    | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' """
        "\\\n"
        "    | tr '[:upper:]' '[:lower:]' | sed '/^$/d' | sort -u | wc -l | tr -d ' '\n"
        ')" || count=""\n'
        'printf "%s" "${count:-0}"\n'
    )
    result = subprocess.run(
        ["bash", "-c", script, "bash", path],
        capture_output=True,
        text=True,
    )
    return int(result.stdout.strip() or "0")


def bash_convergence_rounds(path: str) -> int:
    """Read convergence metadata with gh-guard's exact shell rules."""
    script = (
        "set -euo pipefail\n"
        'count="$(grep -c \'^<!-- review-convergence:\' "$1" 2>/dev/null || true)"\n'
        'rounds="$(sed -n '
        "'s/^<!-- review-convergence: clean; rounds: \\([1-9][0-9]*\\) -->$/\\1/p' "
        '"$1" 2>/dev/null)"\n'
        'if [ "${count:-0}" = "1" ] && [[ "$rounds" =~ ^[1-9][0-9]?$ ]]; then\n'
        '  printf "%s" "$rounds"\n'
        "else\n"
        '  printf "0"\n'
        "fi\n"
    )
    result = subprocess.run(
        ["bash", "-c", script, "bash", path],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(result.stdout.strip() or "0")


def _gh_guard_create(repo: Path, fake_bin: Path) -> subprocess.CompletedProcess:
    """Run the real gh-guard `pr create` in repo with fake gh on PATH, confirmed."""
    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["ZACK_CONFIRMED_PR_CREATE"] = "1"
    guard = Path(__file__).resolve().with_name("gh-guard")
    return subprocess.run(
        [str(guard), "pr", "create", "--title", "x"],
        cwd=str(repo),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )


def _gh_guard_ready(
    args: list[str], *, confirmed_draft: bool = False
) -> subprocess.CompletedProcess:
    """Run gh-guard against a fake gh binary in a noninteractive shell."""
    with tempfile.TemporaryDirectory() as tmp:
        fake_bin = Path(tmp)
        fake_gh = fake_bin / "gh"
        fake_gh.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\"\n", encoding="utf-8")
        fake_gh.chmod(0o755)
        env = dict(os.environ)
        env["PATH"] = f"{fake_bin}{os.pathsep}/usr/bin:/bin"
        if confirmed_draft:
            env["ZACK_CONFIRMED_PR_DRAFT"] = "1"
        guard = Path(__file__).resolve().with_name("gh-guard")
        return subprocess.run(
            [str(guard), *args],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )


SAMPLE_BRANCHES = [
    "main",
    "feat/foo",
    "feat/foo/bar",
    "feat_foo",
    "a%b",
    "already%2Fencoded",
    "zkoppert/triage-dependabot-force-close-prerelease",
    "release/2026.01",
    "%",
    "/",
    "a/b%c/d",
]


def test_encoding_matches_sed() -> None:
    """pr-marker's encode_branch agrees with gh-guard's real sed pipeline."""
    for branch in SAMPLE_BRANCHES:
        expected = sed_encode(branch)
        actual = pr_marker.encode_branch(branch)
        assert (
            actual == expected
        ), f"encoding drift for {branch!r}: pr-marker={actual!r} sed={expected!r}"


def test_known_encodings() -> None:
    """Spot-check a few branch names against hand-computed encodings."""
    cases = {
        "main": "main",
        "feat/foo": "feat%2Ffoo",
        "a%b": "a%25b",
        "a/b%c": "a%2Fb%25c",
    }
    for branch, expected in cases.items():
        assert pr_marker.encode_branch(branch) == expected


def test_kinds_and_thresholds() -> None:
    """The kind filenames, byte floors, and model requirement match gh-guard."""
    assert pr_marker.KINDS["code-review"].filename == "code-review.md"
    assert pr_marker.KINDS["code-review"].min_bytes == 200
    for name in ("plan", "demo", "pr-review", "tests"):
        assert pr_marker.KINDS[name].filename == f"{name}.md"
        assert pr_marker.KINDS[name].min_bytes == 120
    # The three review kinds require a multi-model review; demo and tests are exempt.
    for name in ("code-review", "plan", "pr-review"):
        assert pr_marker.KINDS[name].requires_models, name
    assert not pr_marker.KINDS["demo"].requires_models
    assert not pr_marker.KINDS["tests"].requires_models
    assert pr_marker.KINDS["code-review"].requires_convergence
    for name in ("plan", "demo", "pr-review", "tests"):
        assert not pr_marker.KINDS[name].requires_convergence, name
    # code-review and tests are HEAD-pinned; the others are not.
    for name in ("code-review", "tests"):
        assert pr_marker.KINDS[name].pinned, name
    for name in ("plan", "demo", "pr-review"):
        assert not pr_marker.KINDS[name].pinned, name
    # Only tests is machine-produced (requires the tests-result header); the
    # rest are hand-written and must NOT require it.
    assert pr_marker.KINDS["tests"].requires_result
    for name in ("code-review", "plan", "demo", "pr-review"):
        assert not pr_marker.KINDS[name].requires_result, name


def test_branch_dir_and_paths() -> None:
    """Each branch gets its own encoded directory holding one file per kind."""
    demo = pr_marker.KINDS["demo"]
    code = pr_marker.KINDS["code-review"]
    bdir = pr_marker.branch_dir(branch="feat/foo")
    assert bdir.name == "feat%2Ffoo"
    demo_path = pr_marker.marker_path(demo, branch="feat/foo")
    code_path = pr_marker.marker_path(code, branch="feat/foo")
    assert demo_path.name == "demo.md"
    assert code_path.name == "code-review.md"
    assert demo_path.parent == bdir
    # The pre-refactor collision: branch `x`'s plan vs branch `x.plan`'s code
    # review must now resolve to different files.
    plan_of_x = pr_marker.marker_path(pr_marker.KINDS["plan"], branch="feat/foo")
    cr_of_x_plan = pr_marker.marker_path(code, branch="feat/foo.plan")
    assert plan_of_x != cr_of_x_plan


def test_artifacts_dir_derivation() -> None:
    """The artifacts dir is a sibling of the demo marker inside the branch dir."""
    demo = pr_marker.KINDS["demo"]
    path = pr_marker.marker_path(demo, branch="feat/foo")
    art = pr_marker.artifacts_dir(demo, branch="feat/foo")
    assert path.name == "demo.md"
    assert art.name == "demo.artifacts"
    assert art.parent == path.parent


def fixture_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class PublicationFixture:
    """Isolated legacy fixtures, never review evidence or a real publication."""

    def __init__(self, root: Path):
        self.repo = root / "repo"
        self.repo.mkdir()
        home = root / "home"
        home.mkdir()
        fake_bin = root / "bin"
        fake_bin.mkdir()
        self.publications = root / "publications.jsonl"
        self.env = {
            "HOME": str(home),
            "PATH": os.pathsep.join(
                [str(fake_bin), str(Path(sys.executable).parent), "/usr/bin", "/bin"]
            ),
            "FIXTURE_PUBLICATIONS": str(self.publications),
            "TMPDIR": str(root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_LAZY_FETCH": "1",
        }
        fake_gh = fake_bin / "gh"
        fake_gh.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, subprocess, sys\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "if args[:2] in (['pr', 'create'], ['pr', 'edit']):\n"
            "    record = {'argv': args, 'cwd': os.getcwd()}\n"
            "    for index, arg in enumerate(args):\n"
            "        if arg == '--body-file':\n"
            "            record['body'] = Path(args[index + 1]).read_bytes().decode('utf-8')\n"
            "    with open(os.environ['FIXTURE_PUBLICATIONS'], 'a') as log:\n"
            "        log.write(json.dumps(record) + '\\n')\n"
            "    print('fixture publication intercepted')\n"
            "elif args == ['repo', 'view', '--json', 'nameWithOwner', '--jq', '.nameWithOwner']:\n"
            "    print('fixture/public')\n"
            "elif args == ['api', 'repos/fixture/public', '--jq', '.visibility']:\n"
            "    effect = os.environ.get('FIXTURE_VISIBILITY_EFFECT')\n"
            "    if effect == 'head':\n"
            "        subprocess.run(['git', 'commit', '-q', '--allow-empty', '-m', 'fixture during lint'], check=True)\n"
            "    elif effect == 'body':\n"
            "        with open(os.environ['FIXTURE_BODY_FILE'], 'a') as body:\n"
            "            body.write('Fixture body changed during visibility read.\\n')\n"
            "    print('public')\n"
            "elif args == ['api', 'repos/fixture/private', '--jq', '.visibility']:\n"
            "    print('private')\n"
            "else:\n"
            "    sys.exit('unexpected fixture gh request: ' + repr(args))\n",
            encoding="utf-8",
        )
        fake_gh.chmod(0o755)
        lint = home / ".copilot/skills/validate-style/lint.py"
        lint.parent.mkdir(parents=True)
        lint.symlink_to(
            Path(__file__).resolve().parents[1]
            / ".copilot/skills/validate-style/lint.py"
        )
        for args in (
            ("init", "-q"),
            ("config", "user.email", "fixture@example.com"),
            ("config", "user.name", "publication fixture"),
            ("checkout", "-q", "-b", "fixture/proof"),
            ("commit", "-q", "--allow-empty", "-m", "fixture base"),
        ):
            result = self.run(["git", *args])
            assert result.returncode == 0, result.stderr
        self.head = self.run(["git", "rev-parse", "HEAD"]).stdout.strip()
        self.paths = {
            name: Path(self.marker("path", name).stdout.strip())
            for name in pr_marker.KINDS
        }

    def run(self, argv: list[str], **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(
            argv,
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
            **kwargs,
        )

    def marker(self, *args: str, **kwargs) -> subprocess.CompletedProcess:
        return self.run([sys.executable, str(_MODULE_PATH), *args], **kwargs)

    def create(self, *args: str, confirmed: bool = True) -> subprocess.CompletedProcess:
        env = {**self.env}
        if confirmed:
            env["ZACK_CONFIRMED_PR_CREATE"] = "1"
        return subprocess.run(
            [
                str(_MODULE_PATH.with_name("gh-guard")),
                "pr",
                "create",
                "--title",
                "fixture",
                *args,
            ],
            cwd=self.repo,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def populate_legacy_markers(self) -> None:
        for name in ("plan", "code-review", "demo", "pr-review"):
            args = ["write", name, "-"]
            if name != "demo":
                args += ["--models", "a,b,c"]
            if name == "code-review":
                args += ["--convergence-rounds", "2"]
            result = self.marker(
                *args,
                input="Test fixture only. No real reviewers were run for this content.\n"
                * 8,
            )
            assert result.returncode == 0, result.stderr
        result = self.marker(
            "run-tests",
            "--cmd",
            f"{shlex.quote(sys.executable)} -c 'print(\"fixture machine test\")'",
        )
        assert result.returncode == 0, result.stderr

    def snapshot(self) -> dict[str, bytes]:
        return {
            name: path.read_bytes()
            for name, path in self.paths.items()
            if path.exists()
        }

    def assert_markers_block_publication(self, name: str) -> None:
        before = self.snapshot()
        checked = self.marker("check")
        assert checked.returncode != 0, checked.stdout
        assert str(self.paths[name]) in checked.stderr, checked.stderr
        guarded = self.create()
        assert guarded.returncode != 0, guarded.stdout
        assert str(self.paths[name]) in guarded.stderr, guarded.stderr
        assert (
            not self.publications.exists()
        ), "rejected markers reached native publication"
        assert self.snapshot() == before, "validation rewrote the rejected evidence"

    def assert_legacy_publication_intercepted(self) -> None:
        checked = self.marker("check")
        assert checked.returncode == 0, checked.stderr
        guarded = self.create()
        assert guarded.returncode == 0, guarded.stderr
        calls = [
            json.loads(line)
            for line in self.publications.read_text(encoding="utf-8").splitlines()
        ]
        assert calls == [
            {"argv": ["pr", "create", "--title", "fixture"], "cwd": str(self.repo)}
        ]
        self.publications.unlink()


class NativePublicationFixture(PublicationFixture):
    """Explicit synthetic native records, never genuine producer certification."""

    def __init__(self, root: Path):
        super().__init__(root)
        self.baseline = self.head
        baseline_branch = self.run(["git", "branch", "main", self.baseline])
        assert baseline_branch.returncode == 0, baseline_branch.stderr
        submitted = self.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "fixture implementation"]
        )
        assert submitted.returncode == 0, submitted.stderr
        self.submitted_head = self.run(["git", "rev-parse", "HEAD"]).stdout.strip()
        finalized = self.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "fixture finalization"]
        )
        assert finalized.returncode == 0, finalized.stderr
        self.head = self.run(["git", "rev-parse", "HEAD"]).stdout.strip()
        self.populate_legacy_markers()
        self.body = root / "native-body.md"
        self.body.write_text(
            "I exercise an explicit publication protocol fixture.\n", encoding="utf-8"
        )
        self.body.chmod(0o600)
        self.payload = root / "native-response.json"
        self.verifier_calls = root / "verifier-calls.jsonl"
        self.env.update(
            {
                "NO_MISTAKES_PUBLICATION_RUN": "fixture-run",
                "NO_MISTAKES_PUBLICATION_ATTEMPT": "fixture-attempt",
                "FIXTURE_NATIVE_RESPONSE": str(self.payload),
                "FIXTURE_NATIVE_CALLS": str(self.verifier_calls),
            }
        )
        self.argv = [
            "pr",
            "create",
            "--head",
            "fixture/proof",
            "--base",
            "main",
            "--repo",
            "fixture/public",
            "--draft",
            "--title",
            "fixture title",
            "--body-file",
            str(self.body),
        ]
        self.proof = self.protocol_fixture()
        self.save()
        producer = root / "bin" / "no-mistakes"
        producer.write_text(
            "#!/usr/bin/env python3\n"
            "import argparse, json, os, subprocess, sys\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "calls = Path(os.environ['FIXTURE_NATIVE_CALLS'])\n"
            "with calls.open('a') as log:\n"
            "    log.write(json.dumps({'argv': args, 'cwd': os.getcwd()}) + '\\n')\n"
            "if args == ['axi', '--help']:\n"
            "    command = 'status' if os.environ.get('FIXTURE_OLD_CLI') else 'publication'\n"
            "    print('Available Commands:\\n  ' + command + '  fixture command\\n\\nFlags:\\n  -h help')\n"
            "    sys.exit(0)\n"
            "if args[:3] != ['axi', 'publication', 'verify']:\n"
            "    sys.exit('unexpected fixture producer operation')\n"
            "parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)\n"
            "for name in ('run', 'attempt', 'expected-store', 'branch', 'head', 'body-file'):\n"
            "    parser.add_argument('--' + name, required=True)\n"
            "parser.add_argument('--json', action='store_true', required=True)\n"
            "request = parser.parse_args(args[3:])\n"
            "reject_call = os.environ.get('FIXTURE_NATIVE_REJECT_ON_VERIFY')\n"
            "if reject_call:\n"
            "    verify_count = sum(json.loads(line)['argv'][:3] == ['axi', 'publication', 'verify'] for line in calls.read_text().splitlines())\n"
            "    if str(verify_count) == reject_call:\n"
            "        print(json.dumps({'protocol': 'no-mistakes.publication/v1', 'error': 'explicit fixture refusal'}))\n"
            "        sys.exit(1)\n"
            "effect = os.environ.get('FIXTURE_NATIVE_EFFECT')\n"
            "if effect == 'head':\n"
            "    subprocess.run(['git', 'commit', '-q', '--allow-empty', '-m', 'fixture advance'], check=True)\n"
            "elif effect == 'branch':\n"
            "    branch = 'fixture/switched-' + str(len(calls.read_text().splitlines()))\n"
            "    subprocess.run(['git', 'checkout', '-q', '-b', branch], check=True)\n"
            "elif effect == 'body':\n"
            "    with open(request.body_file, 'a') as body:\n"
            "        body.write('fixture body advance\\n')\n"
            "if os.environ.get('FIXTURE_NATIVE_EXIT'):\n"
            "    sys.exit(int(os.environ['FIXTURE_NATIVE_EXIT']))\n"
            "sys.stdout.buffer.write(Path(os.environ['FIXTURE_NATIVE_RESPONSE']).read_bytes())\n"
            "sys.exit(int(os.environ.get('FIXTURE_NATIVE_EXIT_AFTER_OUTPUT', '0')))\n",
            encoding="utf-8",
        )
        producer.chmod(0o755)

    def protocol_fixture(self) -> dict:
        body = self.body.read_bytes().decode("utf-8")
        plan_text = "Explicit prospective plan fixture. No real preparation was run."
        artifacts = {}
        for name, kind in pr_marker.KINDS.items():
            subject = (
                body
                if name == "pr-review"
                else plan_text if name == "plan" else self.head
            )
            content = (
                f"Explicit {name} protocol fixture. No real native execution is claimed.\n"
                * 4
            )
            artifact = {
                "content": content,
                "sha256": fixture_digest(content),
                "bytes": len(content.encode()),
                "round_id": f"fixture-{name}-round",
                "subject_sha256": fixture_digest(subject),
            }
            if kind.requires_models:
                reviews = []
                start = 300 if name == "plan" else 3000
                for index in range(3):
                    reviews.append(
                        {
                            "invocation_id": f"fixture-{name}-{index}",
                            "agent": "fixture-agent",
                            "requested_model": f"fixture/model-{index}",
                            "served_model": f"fixture/model-{index}",
                            "model_id": f"model-{index}",
                            "model_provider": "fixture",
                            "head_sha": self.baseline if name == "plan" else self.head,
                            "subject_sha256": artifact["subject_sha256"],
                            "started_at": start + index,
                            "completed_at": start + 10 + index,
                            "outcome": "clean",
                            "content": "Explicit test fixture analysis, not a real review.",
                        }
                    )
                artifact["reviews"] = reviews
                artifact["attempts"] = [
                    {
                        **review,
                        "outcome": "returned",
                        "content": '{"publication_analysis":"explicit fixture"}',
                    }
                    for review in reviews
                ]
            if name == "tests":
                artifact["checks"] = [
                    {
                        "command": command,
                        "exit_code": 0,
                        "started_at": 1000 + index * 500,
                        "completed_at": 1500 + index * 500,
                        "head_sha": self.head,
                        "output": "Synthetic protocol result, not actual task testing evidence.",
                    }
                    for index, command in enumerate(
                        ("fixture full unit suite", "fixture full lint/build checks")
                    )
                ]
            if name == "demo":
                artifact["observations"] = {
                    "verdict": "go",
                    "summary": "Explicit scenario fixture",
                }
            artifacts[name] = artifact
        proof = {
            "protocol": "no-mistakes.publication/v1",
            "run_id": "fixture-run",
            "attempt_id": "fixture-attempt",
            "repo_id": "fixture-repository",
            "preparation_id": "fixture-finalization-round",
            "preparation_admitted_at": 500,
            "submitted_head_sha": self.submitted_head,
            "title": "fixture title",
            "body": body,
            "body_sha256": fixture_digest(body),
            "history_known": True,
            "total_code_rounds": 2,
            "total_plan_rounds": 1,
            "total_description_rounds": 1,
            "binding": {
                "requested_store": str(self.repo / ".git"),
                "worktree": str(self.repo),
                "branch": "fixture/proof",
                "head_sha": self.head,
                "repository": "fixture/public",
                "head_repository": "fixture/public",
                "base_branch": "main",
                "draft": True,
                "operation": "create",
                "push_target": "opaque-fixture-target",
                "before_push": False,
            },
            "policy": {
                "protocol": "no-mistakes.publication/v1",
                "artifacts": {
                    name: {"min_bytes": kind.min_bytes}
                    for name, kind in pr_marker.KINDS.items()
                },
                "min_review_models": 3,
                "excluded_model_prefixes": ["gemini"],
                "create_confirmation_env": "ZACK_CONFIRMED_PR_CREATE",
                "review_budget_scope": "code-only",
                "plan_order": "before-implementation",
                "max_review_rounds": 10,
                "requires_full_tests": True,
            },
            "artifacts": artifacts,
        }
        checks = copy.deepcopy(artifacts["tests"])
        checks.update(
            round_id="fixture-baseline-checks-round",
            subject_sha256=fixture_digest(self.baseline),
        )
        for index, check in enumerate(checks["checks"]):
            check.update(
                head_sha=self.baseline,
                started_at=100 + index * 50,
                completed_at=150 + index * 50,
            )
        proof["plan"] = {
            "run_id": "fixture-earlier-plan-run",
            "repo_id": proof["repo_id"],
            "branch": proof["binding"]["branch"],
            "requested_store": proof["binding"]["requested_store"],
            "worktree": proof["binding"]["worktree"],
            "baseline_sha": self.baseline,
            "intent_sha256": fixture_digest("Explicit synthetic intent"),
            "plan_text": plan_text,
            "plan_sha256": fixture_digest(plan_text),
            "checks": checks,
            "artifact": artifacts["plan"],
            "policy": copy.deepcopy(proof["policy"]),
            "execution_config_sha256": fixture_digest(
                "Explicit synthetic execution pin"
            ),
            "completed_at": 400,
        }
        return proof

    def planning_exception_fixture(self) -> dict:
        """Source-derived protocol fixture, not executed native authorization."""
        proof = self.protocol_fixture()
        proof["required_commands"] = [
            "fixture full unit suite",
            "fixture full lint/build checks",
        ]
        proof["policy"]["planning_exception_support"] = True
        # Native PolicyDigest hashes struct field order and sorted map keys.
        # Keep those bytes as fixture data, not a second production encoder.
        policy_json = (
            '{"protocol":"no-mistakes.publication/v1","artifacts":{'
            '"code-review":{"min_bytes":200},"demo":{"min_bytes":120},'
            '"plan":{"min_bytes":120},"pr-review":{"min_bytes":120},'
            '"tests":{"min_bytes":120}},"min_review_models":3,'
            '"excluded_model_prefixes":["gemini"],'
            '"create_confirmation_env":"ZACK_CONFIRMED_PR_CREATE",'
            '"review_budget_scope":"code-only","plan_order":"before-implementation",'
            '"planning_exception_support":true,"max_review_rounds":10,'
            '"requires_full_tests":true}'
        )
        assert json.loads(policy_json) == proof["policy"], "native policy fixture drift"
        proof["plan"] = None
        proof["preparation_admitted_at"] = 0
        proof["total_plan_rounds"] = 0
        del proof["artifacts"]["plan"]
        proof["planning_exception"] = {
            "scope": "missing-pre-implementation-planning",
            "run_id": proof["run_id"],
            "repo_id": proof["repo_id"],
            "requested_store": proof["binding"]["requested_store"],
            "worktree": proof["binding"]["worktree"],
            "branch": proof["binding"]["branch"],
            "work_head_sha": proof["submitted_head_sha"],
            "intent_sha256": fixture_digest("Explicit synthetic intent"),
            "policy_sha256": fixture_digest(policy_json),
            "id": "fixture-native-authorization",
            "authorized_at": 500,
            "authority": "native-operator-control",
            "authorizer_pid": 42,
            "reason": "Explicit authorization fixture, not a real native operator action.",
        }
        return proof

    def save(self) -> None:
        self.payload.write_text(json.dumps(self.proof), encoding="utf-8")

    def native(self, *, guard=False, confirmed=True) -> subprocess.CompletedProcess:
        env = dict(self.env)
        if confirmed and self.argv[1] == "create":
            env["ZACK_CONFIRMED_PR_CREATE"] = "1"
        command = (
            [str(_MODULE_PATH.with_name("gh-guard")), *self.argv]
            if guard
            else [
                sys.executable,
                str(_MODULE_PATH),
                "check",
                "--publication",
                *self.argv,
            ]
        )
        return subprocess.run(
            command,
            cwd=self.repo,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def assert_fixture_accepted(self, *, confirmed=True) -> None:
        markers, payload = self.snapshot(), self.payload.read_bytes()
        for guard in (False, True):
            result = self.native(guard=guard, confirmed=confirmed)
            assert result.returncode == 0, (result.stdout, result.stderr)
            assert result.stdout == (
                "fixture publication intercepted\n" if guard else ""
            )
            assert result.stderr == "", result.stderr
            assert self.snapshot() == markers, "acceptance rewrote legacy markers"
            assert (
                self.payload.read_bytes() == payload
            ), "acceptance rewrote native history"
            if not guard:
                assert not self.publications.exists(), "helper performed publication"
        calls = [
            json.loads(line) for line in self.publications.read_text().splitlines()
        ]
        assert calls == [
            {
                "argv": self.argv,
                "cwd": str(self.repo),
                "body": self.body.read_bytes().decode("utf-8"),
            }
        ], calls
        self.publications.unlink()

    def assert_denied(self, diagnostic: str, *, confirmed=True) -> None:
        markers, payload = self.snapshot(), self.payload.read_bytes()
        # Only immutable refusal cases can share the fixture concurrently;
        # verification-time Git/body mutations must keep their original order.
        if self.env.get("FIXTURE_NATIVE_EFFECT"):
            results = [
                self.native(guard=guard, confirmed=confirmed) for guard in (False, True)
            ]
        else:
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending = [
                    pool.submit(self.native, guard=guard, confirmed=confirmed)
                    for guard in (False, True)
                ]
                results = [result.result() for result in pending]
        for result in results:
            assert result.returncode == 1, (result.stdout, result.stderr)
            assert diagnostic in result.stderr, result.stderr
            assert result.stdout == "", result.stdout
            assert (
                not self.publications.exists()
            ), "rejected native proof reached publication"
            assert (
                self.snapshot() == markers
            ), "native rejection rewrote legacy evidence"
            assert (
                self.payload.read_bytes() == payload
            ), "native rejection rewrote producer evidence/counts"


def test_native_capabilities_preserve_the_consumer_policy() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = NativePublicationFixture(Path(tmp))
        before = fixture.snapshot()
        result = fixture.marker("capabilities", "--json")
        assert result.returncode == 0, result.stderr
        assert result.stderr == "", result.stderr
        assert json.loads(result.stdout) == {
            "protocol": "no-mistakes.publication/v1",
            "artifacts": {
                "plan": {"min_bytes": 120},
                "code-review": {"min_bytes": 200},
                "pr-review": {"min_bytes": 120},
                "demo": {"min_bytes": 120},
                "tests": {"min_bytes": 120},
            },
            "min_review_models": 3,
            "excluded_model_prefixes": ["gemini"],
            "create_confirmation_env": "ZACK_CONFIRMED_PR_CREATE",
            "review_budget_scope": "code-only",
            "plan_order": "before-implementation",
            "planning_exception_support": True,
            "max_review_rounds": 10,
            "requires_full_tests": True,
        }
        assert not fixture.verifier_calls.exists(), "discovery invoked a producer"
        assert not fixture.publications.exists(), "discovery performed publication"
        assert fixture.snapshot() == before


def test_native_complete_fixture_does_not_require_or_write_legacy_markers() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = NativePublicationFixture(Path(tmp))
        assert len({fixture.baseline, fixture.submitted_head, fixture.head}) == 3
        assert fixture.proof["plan"]["run_id"] != fixture.proof["preparation_id"]
        for path in fixture.paths.values():
            path.unlink()
        fixture.assert_fixture_accepted()
        assert fixture.snapshot() == {}, "native acceptance synthesized markers"
        fixture.assert_denied("same-call ZACK_CONFIRMED_PR_CREATE", confirmed=False)
        fixture.env["FIXTURE_NATIVE_EXIT"] = "1"
        fixture.assert_denied("native verifier refused")


def test_native_preparation_binding_and_admission_are_required() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = NativePublicationFixture(Path(tmp))
        original = copy.deepcopy(fixture.proof)
        for field in ("plan", "preparation_admitted_at", "submitted_head_sha"):
            fixture.proof = copy.deepcopy(original)
            del fixture.proof[field]
            fixture.save()
            fixture.assert_denied("native proof: missing required fields")
        fixture.proof = copy.deepcopy(original)
        fixture.proof["plan"] = None
        fixture.save()
        fixture.assert_denied("pre-implementation plan: missing required fields")
        for field in original["plan"]:
            fixture.proof = copy.deepcopy(original)
            del fixture.proof["plan"][field]
            fixture.save()
            fixture.assert_denied("pre-implementation plan: missing required fields")
        for field, value, diagnostic in (
            ("run_id", original["run_id"], "plan context mismatch"),
            ("repo_id", "wrong-repository", "plan context mismatch"),
            ("branch", "fixture/wrong", "plan context mismatch"),
            ("requested_store", str(Path(tmp) / "wrong.git"), "plan context mismatch"),
            ("worktree", str(Path(tmp) / "elsewhere"), "plan context mismatch"),
            ("baseline_sha", "short", "prepared baseline"),
            ("intent_sha256", "g" * 64, "plan intent_sha256"),
            ("execution_config_sha256", "x" * 64, "plan execution_config_sha256"),
            ("plan_text", "Different synthetic subject", "plan subject mismatch"),
            ("plan_sha256", "0" * 64, "plan subject mismatch"),
            ("completed_at", True, "plan completion"),
            ("completed_at", 501, "preparation admission"),
        ):
            fixture.proof = copy.deepcopy(original)
            fixture.proof["plan"][field] = value
            fixture.save()
            fixture.assert_denied(diagnostic)
        for field, value, diagnostic in (
            ("preparation_admitted_at", 399, "preparation admission"),
            ("preparation_admitted_at", None, "preparation admission"),
            ("preparation_admitted_at", 500.0, "preparation admission"),
            ("submitted_head_sha", "short", "submitted head"),
        ):
            fixture.proof = copy.deepcopy(original)
            fixture.proof[field] = value
            fixture.save()
            fixture.assert_denied(diagnostic)
        fixture.proof = copy.deepcopy(original)
        fixture.proof["plan"]["policy"]["max_review_rounds"] = 9
        fixture.save()
        fixture.assert_denied("policy differs from pre-implementation plan")
        for field, value in (("max_review_rounds", 10.0), ("requires_full_tests", 1)):
            fixture.proof = copy.deepcopy(original)
            fixture.proof["plan"]["policy"][field] = value
            fixture.save()
            fixture.assert_denied("policy differs from pre-implementation plan")
        fixture.proof = copy.deepcopy(original)
        fixture.proof["plan"]["artifact"] = copy.deepcopy(
            fixture.proof["artifacts"]["plan"]
        )
        fixture.proof["plan"]["artifact"]["bytes"] = float(
            fixture.proof["plan"]["artifact"]["bytes"]
        )
        fixture.save()
        fixture.assert_denied("pre-implementation plan was replaced or relabeled")
        fixture.proof = copy.deepcopy(original)
        replacement = copy.deepcopy(fixture.proof["artifacts"]["plan"])
        replacement["content"] += "Replaced plan synthesis."
        replacement["sha256"] = fixture_digest(replacement["content"])
        replacement["bytes"] = len(replacement["content"].encode())
        fixture.proof["artifacts"]["plan"] = replacement
        fixture.save()
        fixture.assert_denied("pre-implementation plan was replaced or relabeled")


def test_native_baseline_checks_and_plan_reviews_precede_implementation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = NativePublicationFixture(Path(tmp))
        original = copy.deepcopy(fixture.proof)
        for field, value, diagnostic in (
            ("checks", [], "baseline machine checks are missing"),
            (
                "subject_sha256",
                fixture_digest(fixture.head),
                "baseline tests subject is stale",
            ),
            ("bytes", 0, "baseline tests artifact floor/digest mismatch"),
        ):
            fixture.proof = copy.deepcopy(original)
            fixture.proof["plan"]["checks"][field] = value
            fixture.save()
            fixture.assert_denied(diagnostic)
        for field, value, diagnostic in (
            ("exit_code", 1, "baseline machine checks did not pass"),
            ("exit_code", False, "baseline machine checks did not pass"),
            ("head_sha", fixture.head, "baseline machine checks did not pass"),
            ("started_at", 0, "baseline machine check start"),
            ("completed_at", 99, "baseline machine check completion"),
            ("completed_at", 401, "baseline machine checks did not pass"),
        ):
            fixture.proof = copy.deepcopy(original)
            fixture.proof["plan"]["checks"]["checks"][0][field] = value
            fixture.save()
            fixture.assert_denied(diagnostic)
        for field, value, diagnostic in (
            (
                "head_sha",
                fixture.head,
                "plan review preceded passing baseline checks or is stale",
            ),
            (
                "started_at",
                199,
                "plan review preceded passing baseline checks or is stale",
            ),
            (
                "completed_at",
                401,
                "plan review completed after the preparation receipt",
            ),
        ):
            fixture.proof = copy.deepcopy(original)
            fixture.proof["artifacts"]["plan"]["reviews"][0][field] = value
            fixture.save()
            fixture.assert_denied(diagnostic)
        fixture.proof = copy.deepcopy(original)
        fixture.proof["artifacts"]["tests"]["checks"][0]["started_at"] = 499
        fixture.save()
        fixture.assert_denied(
            "machine checks did not pass on the final head after implementation admission"
        )
        for change in ("missing-last", "extra", "reordered", "different"):
            fixture.proof = copy.deepcopy(original)
            checks = fixture.proof["artifacts"]["tests"]["checks"]
            if change == "missing-last":
                checks.pop()
            elif change == "extra":
                checks.append(copy.deepcopy(checks[-1]))
            elif change == "reordered":
                checks.reverse()
            else:
                checks[-1]["command"] = "fixture narrowed command"
            fixture.save()
            fixture.assert_denied(
                "final checks differ from the prepared command manifest"
            )
        fixture.proof = copy.deepcopy(original)
        fixture.proof["preparation_admitted_at"] = 400
        fixture.proof["artifacts"]["tests"]["checks"][0]["started_at"] = 400
        for field in ("reviews", "attempts"):
            fixture.proof["artifacts"]["plan"][field][0]["started_at"] = 200
            fixture.proof["artifacts"]["plan"][field][-1]["completed_at"] = 400
            fixture.proof["artifacts"]["code-review"][field][0]["started_at"] = 2000
        fixture.save()
        fixture.assert_fixture_accepted()


def test_native_proposed_exception_requires_bound_native_authorization() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = NativePublicationFixture(Path(tmp))
        original = fixture.planning_exception_fixture()
        for field, value, diagnostic in (
            ("authority", "caller-note", "authority/scope mismatch"),
            ("scope", "all-planning", "authority/scope mismatch"),
            ("run_id", "different-run", "run/context mismatch"),
            ("repo_id", "different-repository", "run/context mismatch"),
            ("branch", "fixture/other", "run/context mismatch"),
            ("requested_store", str(Path(tmp) / "other.git"), "run/context mismatch"),
            ("worktree", str(Path(tmp) / "other"), "run/context mismatch"),
            ("work_head_sha", fixture.head, "run/context mismatch"),
            ("id", "", "native authorization identity"),
            ("intent_sha256", "not-a-digest", "native authorization intent_sha256"),
            ("policy_sha256", "x" * 64, "native authorization policy_sha256"),
            ("authorized_at", True, "native authorization time"),
            ("authorizer_pid", 0, "native authorizer PID"),
            ("reason", " ", "native authorization reason is empty"),
        ):
            fixture.proof = copy.deepcopy(original)
            fixture.proof["planning_exception"][field] = value
            fixture.save()
            fixture.assert_denied(diagnostic)
        fixture.proof = copy.deepcopy(original)
        fixture.proof["planning_exception"] = {"reason": "A caller's approval note"}
        fixture.save()
        fixture.assert_denied("native planning exception: missing required fields")


def test_native_proposed_exception_preserves_remaining_proof_and_history() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = NativePublicationFixture(Path(tmp))
        original = fixture.planning_exception_fixture()
        for field, value, diagnostic in (
            (
                "plan",
                fixture.protocol_fixture()["plan"],
                "cannot invent a plan or prior admission",
            ),
            (
                "preparation_admitted_at",
                False,
                "cannot invent a plan or prior admission",
            ),
            ("preparation_admitted_at", 500, "cannot invent a plan or prior admission"),
            ("total_plan_rounds", -1, "expected an integer"),
            ("history_known", False, "history is unknown"),
            ("total_code_rounds", 11, "cumulative review budget is exhausted"),
        ):
            fixture.proof = copy.deepcopy(original)
            fixture.proof[field] = value
            fixture.save()
            fixture.assert_denied(diagnostic)
        for support, diagnostic in (
            (False, "requires explicit policy support"),
            (1, "planning-exception support must be boolean"),
        ):
            fixture.proof = copy.deepcopy(original)
            fixture.proof["policy"]["planning_exception_support"] = support
            fixture.save()
            fixture.assert_denied(diagnostic)
        fixture.proof = copy.deepcopy(original)
        fixture.proof["artifacts"]["plan"] = fixture.protocol_fixture()["artifacts"][
            "plan"
        ]
        fixture.save()
        fixture.assert_denied("native artifacts: unsupported fields")
        fixture.proof = copy.deepcopy(original)
        fixture.proof["planning_exception"]["authorized_at"] = 1001
        fixture.save()
        fixture.assert_denied(
            "machine checks did not pass on the final head after native authorization"
        )
        for kind in ("code-review", "pr-review"):
            fixture.proof = copy.deepcopy(original)
            fixture.proof["artifacts"][kind]["reviews"].pop()
            fixture.save()
            fixture.assert_denied(f"{kind} has insufficient distinct reviewers")
        for kind in original["artifacts"]:
            fixture.proof = copy.deepcopy(original)
            del fixture.proof["artifacts"][kind]
            fixture.save()
            fixture.assert_denied("native artifacts: missing required fields")
        for plan_rounds in (0, 3):
            fixture.proof = copy.deepcopy(original)
            fixture.proof["total_plan_rounds"] = plan_rounds
            fixture.save()
            fixture.assert_fixture_accepted()
        fixture.assert_denied("same-call ZACK_CONFIRMED_PR_CREATE", confirmed=False)
        for support in (False, True):
            fixture.proof = fixture.protocol_fixture()
            fixture.proof["policy"]["planning_exception_support"] = support
            fixture.proof["plan"]["policy"]["planning_exception_support"] = support
            fixture.save()
            fixture.assert_fixture_accepted()


def test_native_authorizer_pid_matches_the_native_boundary() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = NativePublicationFixture(Path(tmp))
        fixture.proof = fixture.planning_exception_fixture()
        assert fixture.proof["planning_exception"]["policy_sha256"] == (
            "5e525653ba5a59548b8becf203d71d3722487290d9bef9c568ac3c114e8342ed"
        )
        fixture.proof["planning_exception"]["authorizer_pid"] = 1
        fixture.save()
        fixture.assert_denied("native authorizer PID")
        fixture.proof["planning_exception"]["authorizer_pid"] = 2
        fixture.save()
        fixture.assert_fixture_accepted()


def test_native_authorization_reason_limit_counts_utf8_bytes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = NativePublicationFixture(Path(tmp))
        fixture.proof = fixture.planning_exception_fixture()
        reason = "é" * 2048
        assert len(reason.encode("utf-8")) == 4096
        fixture.proof["planning_exception"]["reason"] = reason
        fixture.save()
        fixture.assert_fixture_accepted()
        fixture.proof["planning_exception"]["reason"] += "x"
        fixture.save()
        fixture.assert_denied("native authorization reason exceeds 4096 UTF-8 bytes")


def test_native_exception_requires_exact_native_command_manifest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = NativePublicationFixture(Path(tmp))
        original = fixture.planning_exception_fixture()
        for commands, diagnostic in (
            (None, "native required command manifest is missing or malformed"),
            ([], "native required command manifest is missing or malformed"),
            (
                {"command": "not-a-list"},
                "native required command manifest is missing or malformed",
            ),
            ([False], "native required command: expected a string"),
            ([" "], "native required command is empty"),
            (
                list(reversed(original["required_commands"])),
                "final checks differ from the pinned command manifest",
            ),
        ):
            fixture.proof = copy.deepcopy(original)
            if commands is None:
                del fixture.proof["required_commands"]
            else:
                fixture.proof["required_commands"] = commands
            fixture.save()
            fixture.assert_denied(diagnostic)
        for change in ("missing-last", "extra", "reordered", "different"):
            fixture.proof = copy.deepcopy(original)
            checks = fixture.proof["artifacts"]["tests"]["checks"]
            if change == "missing-last":
                checks.pop()
            elif change == "extra":
                checks.append(copy.deepcopy(checks[-1]))
            elif change == "reordered":
                checks.reverse()
            else:
                checks[-1]["command"] = "fixture narrowed command"
            fixture.save()
            fixture.assert_denied(
                "final checks differ from the pinned command manifest"
            )
        fixture.proof = fixture.protocol_fixture()
        fixture.proof["required_commands"] = original["required_commands"]
        fixture.save()
        fixture.assert_denied(
            "required_commands is only supported with a planning exception"
        )


def test_native_locators_require_proof_without_legacy_fallback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = NativePublicationFixture(Path(tmp))
        for key in pr_marker.NATIVE_LOCATORS:
            original = fixture.env.pop(key)
            fixture.assert_denied("both native run/attempt locators")
            fixture.env[key] = ""
            fixture.assert_denied("both native run/attempt locators")
            fixture.env[key] = original
        fixture.assert_denied("same-call ZACK_CONFIRMED_PR_CREATE", confirmed=False)
        assert not fixture.verifier_calls.exists()
        checked = fixture.marker("check")
        assert checked.returncode == 1 and "actual gh argv" in checked.stderr
        status = fixture.marker("status")
        assert status.returncode == 1
        assert "native handoff needs check --publication" in status.stdout
        assert "all markers satisfied" not in status.stdout
        fixture.env["FIXTURE_OLD_CLI"] = "1"
        fixture.assert_denied("has no native publication verifier")
        calls = [
            json.loads(line)["argv"]
            for line in fixture.verifier_calls.read_text().splitlines()
        ]
        assert calls == [["axi", "--help"], ["axi", "--help"]]
        del fixture.env["FIXTURE_OLD_CLI"]
        fixture.env["FIXTURE_NATIVE_EXIT"] = "1"
        fixture.assert_denied("native verifier refused")


def test_native_rejected_verifier_stdout_cannot_supply_proof() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = NativePublicationFixture(Path(tmp))
        fixture.env["FIXTURE_NATIVE_EXIT_AFTER_OUTPUT"] = "1"
        fixture.assert_denied("native verifier refused or is unavailable (exit 1)")


def test_native_recheck_refusal_prevents_publication() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = NativePublicationFixture(Path(tmp))
        fixture.env["FIXTURE_NATIVE_REJECT_ON_VERIFY"] = "2"
        before, payload = fixture.snapshot(), fixture.payload.read_bytes()
        result = fixture.native(guard=True)
        assert result.returncode == 1, (result.stdout, result.stderr)
        assert "native verifier refused or is unavailable (exit 1)" in result.stderr
        assert result.stdout == ""
        assert (
            not fixture.publications.exists()
        ), "earlier proof overrode native refusal"
        assert fixture.snapshot() == before
        assert fixture.payload.read_bytes() == payload
        verified = [
            call
            for line in fixture.verifier_calls.read_text().splitlines()
            if (call := json.loads(line))["argv"][:3]
            == ["axi", "publication", "verify"]
        ]
        assert len(verified) == 2 and verified[0] == verified[1], verified


def test_native_proof_parser_rejects_malformed_and_inspection_responses() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = NativePublicationFixture(Path(tmp))
        original = fixture.payload.read_bytes()
        cases = [
            (b"", "native proof is empty"),
            (b"old review log", "malformed native proof JSON"),
            (b"\xff", "malformed native proof JSON"),
            (b"[]", "missing required fields"),
            (
                b'{"protocol":"no-mistakes.publication/v1","supported":true}',
                "missing required fields",
            ),
            (
                original.replace(
                    b'"protocol":', b'"protocol":"duplicate","protocol":', 1
                ),
                "duplicate JSON key",
            ),
            (original[:-1] + b',"unimplemented_field":true}', "unsupported fields"),
            (b"x" * (pr_marker.NATIVE_MAX_PAYLOAD + 1), "payload limit"),
        ]
        for payload, diagnostic in cases:
            fixture.payload.write_bytes(payload)
            fixture.assert_denied(diagnostic)


def test_native_proof_binds_actual_store_branch_head_body_and_call() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = NativePublicationFixture(Path(tmp))
        original = copy.deepcopy(fixture.proof)
        for field, value, diagnostic in (
            (
                "requested_store",
                str(Path(tmp) / "wrong.git"),
                "requested_store mismatch",
            ),
            ("worktree", str(Path(tmp) / "wrong"), "worktree mismatch"),
            ("branch", "fixture/wrong", "branch mismatch"),
            ("head_sha", "0" * 40, "head_sha mismatch"),
            ("repository", "other/public", "target repository mismatch"),
            (
                "head_repository",
                "other/fork",
                "head repository/branch selector mismatch",
            ),
            ("base_branch", "other-base", "base/draft mismatch"),
            ("draft", False, "base/draft mismatch"),
            ("operation", "update", "operation mismatch"),
        ):
            fixture.proof = copy.deepcopy(original)
            fixture.proof["binding"][field] = value
            fixture.save()
            fixture.assert_denied(diagnostic)
        for field, value, diagnostic in (
            ("run_id", "wrong-run", "producer/run/attempt identity mismatch"),
            ("attempt_id", "stale-attempt", "producer/run/attempt identity mismatch"),
            ("title", "different title", "title mismatch"),
            ("body", original["body"].rstrip("\n"), "exact body mismatch"),
            ("body_sha256", "0" * 64, "exact body mismatch"),
        ):
            fixture.proof = copy.deepcopy(original)
            fixture.proof[field] = value
            fixture.save()
            fixture.assert_denied(diagnostic)
        calls = [
            json.loads(line) for line in fixture.verifier_calls.read_text().splitlines()
        ]
        verify = next(
            call
            for call in calls
            if call["argv"][:3] == ["axi", "publication", "verify"]
        )
        assert verify == {
            "cwd": str(fixture.repo),
            "argv": [
                "axi",
                "publication",
                "verify",
                "--run",
                "fixture-run",
                "--attempt",
                "fixture-attempt",
                "--expected-store",
                str(fixture.repo / ".git"),
                "--branch",
                "fixture/proof",
                "--head",
                fixture.head,
                "--body-file",
                str(fixture.body),
                "--json",
            ],
        }


def test_native_context_and_body_changes_cannot_relabel_old_proof() -> None:
    for effect in ("head", "branch", "body"):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = NativePublicationFixture(Path(tmp))
            fixture.env["FIXTURE_NATIVE_EFFECT"] = effect
            fixture.assert_denied("changed during native verification")
    with tempfile.TemporaryDirectory() as tmp:
        fixture = NativePublicationFixture(Path(tmp))
        fixture.run(["git", "checkout", "-q", "--detach"])
        fixture.assert_denied("named branch, not detached HEAD")
        assert not fixture.verifier_calls.exists()


def test_native_uses_requested_common_store_in_a_linked_worktree() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = NativePublicationFixture(Path(tmp))
        common_store = str((fixture.repo / ".git").resolve())
        linked = Path(tmp) / "linked"
        added = fixture.run(
            ["git", "worktree", "add", "-q", "-b", "fixture/linked", str(linked)]
        )
        assert added.returncode == 0, added.stderr
        fixture.repo = linked
        fixture.paths = {
            name: Path(fixture.marker("path", name).stdout.strip())
            for name in pr_marker.KINDS
        }
        fixture.populate_legacy_markers()
        fixture.argv[fixture.argv.index("--head") + 1] = "fixture/linked"
        for context in (fixture.proof["binding"], fixture.proof["plan"]):
            context.update(
                worktree=str(linked),
                branch="fixture/linked",
                requested_store=common_store,
            )
        fixture.save()
        fixture.assert_fixture_accepted()
        calls = [
            json.loads(line)["argv"]
            for line in fixture.verifier_calls.read_text().splitlines()
        ]
        verify = next(
            argv for argv in calls if argv[:3] == ["axi", "publication", "verify"]
        )
        assert verify[verify.index("--expected-store") + 1] == common_store
        per_worktree = fixture.run(
            ["git", "rev-parse", "--absolute-git-dir"]
        ).stdout.strip()
        assert per_worktree != common_store
        fixture.proof["binding"]["requested_store"] = per_worktree
        fixture.save()
        fixture.assert_denied("requested_store mismatch")


def test_native_artifact_review_and_machine_check_requirements() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = NativePublicationFixture(Path(tmp))
        original = copy.deepcopy(fixture.proof)
        for name, kind in pr_marker.KINDS.items():
            fixture.proof = copy.deepcopy(original)
            del fixture.proof["artifacts"][name]
            fixture.save()
            fixture.assert_denied("native artifacts: missing required fields")
            fixture.proof = copy.deepcopy(original)
            artifact = fixture.proof["artifacts"][name]
            artifact["content"] = "x" * (kind.min_bytes - 1)
            artifact["bytes"] = kind.min_bytes - 1
            artifact["sha256"] = fixture_digest(artifact["content"])
            fixture.save()
            fixture.assert_denied(f"{name} artifact floor/digest mismatch")
            for size in (kind.min_bytes, kind.min_bytes + 1):
                artifact["content"] = "x" * size
                artifact["bytes"] = size
                artifact["sha256"] = fixture_digest(artifact["content"])
                fixture.save()
                fixture.assert_fixture_accepted()
        for name in ("plan", "code-review", "pr-review"):
            fixture.proof = copy.deepcopy(original)
            fixture.proof["artifacts"][name]["reviews"].pop()
            fixture.save()
            fixture.assert_denied(f"{name} has insufficient distinct reviewers")
            for served, model_id, diagnostic in (
                ("google/Gemini-3", "gemini-3", "prohibited or duplicate reviewers"),
                ("other/model-0", "model-0", "prohibited or duplicate reviewers"),
                (
                    "fixture/model-1",
                    "invented",
                    "requested/served model identity mismatch",
                ),
                (
                    "provider/nested/model-1",
                    "model-1",
                    "unsupported native model spelling",
                ),
            ):
                fixture.proof = copy.deepcopy(original)
                review = fixture.proof["artifacts"][name]["reviews"][1]
                review.update(
                    requested_model=served, served_model=served, model_id=model_id
                )
                fixture.save()
                fixture.assert_denied(diagnostic)
            fixture.proof = copy.deepcopy(original)
            fixture.proof["artifacts"][name]["attempts"].pop()
            fixture.save()
            fixture.assert_denied("clean review does not match its recorded attempt")
        for field, value, diagnostic in (
            ("checks", [], "machine checks are missing"),
            ("exit_code", 1, "machine checks did not pass"),
            ("head_sha", "0" * 40, "machine checks did not pass"),
            ("completed_at", 500, "machine check completion"),
        ):
            fixture.proof = copy.deepcopy(original)
            if field == "checks":
                fixture.proof["artifacts"]["tests"][field] = value
            else:
                fixture.proof["artifacts"]["tests"]["checks"][0][field] = value
            fixture.save()
            fixture.assert_denied(diagnostic)
        fixture.proof = copy.deepcopy(original)
        fixture.proof["artifacts"]["code-review"]["reviews"][0]["started_at"] = 1999
        fixture.save()
        fixture.assert_denied("review preceded passing final-head checks")
        for name in ("code-review", "pr-review", "demo", "tests"):
            fixture.proof = copy.deepcopy(original)
            fixture.proof["artifacts"][name]["subject_sha256"] = "0" * 64
            fixture.save()
            fixture.assert_denied(f"{name} subject is stale")
        fixture.proof = copy.deepcopy(original)
        fixture.proof["artifacts"]["plan"]["attempts"] = [None]
        fixture.save()
        fixture.assert_denied("plan attempt: missing required fields")
        for field, value in (("min_review_models", 2), ("requires_full_tests", False)):
            fixture.proof = copy.deepcopy(original)
            fixture.proof["policy"][field] = value
            fixture.save()
            fixture.assert_denied("review/test/confirmation policy mismatch")


def test_native_code_budget_retains_separate_plan_and_description_history() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = NativePublicationFixture(Path(tmp))
        original = copy.deepcopy(fixture.proof)
        fixture.proof["history_known"] = False
        fixture.save()
        fixture.assert_denied("history is unknown")
        for field in (
            "total_code_rounds",
            "total_plan_rounds",
            "total_description_rounds",
        ):
            for value in (None, True, 1 << 63, 0, -1, 1.5):
                fixture.proof = copy.deepcopy(original)
                fixture.proof[field] = value
                fixture.save()
                fixture.assert_denied("expected an integer")
        fixture.proof = copy.deepcopy(original)
        fixture.proof["total_code_rounds"] = 11
        fixture.save()
        fixture.assert_denied("cumulative review budget is exhausted")
        for field, value, diagnostic in (
            (
                "review_budget_scope",
                "combined",
                "code-only review budget policy mismatch",
            ),
            ("max_review_rounds", 9, "code-only review budget policy mismatch"),
            ("max_review_rounds", 11, "code-only review budget policy mismatch"),
            ("max_review_rounds", True, "native review ceiling"),
            ("plan_order", "after-implementation", "plan ordering policy mismatch"),
        ):
            fixture.proof = copy.deepcopy(original)
            fixture.proof["policy"][field] = value
            fixture.save()
            fixture.assert_denied(diagnostic)
        for code_rounds in (1, 10):
            fixture.proof = copy.deepcopy(original)
            fixture.proof.update(
                total_code_rounds=code_rounds,
                total_plan_rounds=11,
                total_description_rounds=12,
            )
            failed = copy.deepcopy(
                fixture.proof["artifacts"]["code-review"]["attempts"][0]
            )
            failed.update(
                invocation_id="fixture-failed-format-attempt",
                outcome="failed",
                served_model="",
                model_id="",
                content="fixture formatting failure",
            )
            fixture.proof["artifacts"]["code-review"]["attempts"].insert(0, failed)
            fixture.save()
            fixture.assert_fixture_accepted()
            fixture.assert_fixture_accepted()
        for served, canonical in (
            ("Gemini/model-1", "model-1"),
            ("fixture/not-gemini-model", "not-gemini-model"),
            ("fixture/ΟΣ", "οσ"),
            ("fixture/İ", "i"),
        ):
            fixture.proof = copy.deepcopy(original)
            for field in ("reviews", "attempts"):
                row = fixture.proof["artifacts"]["code-review"][field][1]
                row.update(
                    requested_model=served,
                    served_model=served,
                    model_id=canonical,
                )
            fixture.save()
            fixture.assert_fixture_accepted()


def test_native_body_transport_and_update_target_cannot_bypass_guard() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = NativePublicationFixture(Path(tmp))
        original_argv = fixture.argv[:]
        for value in ("-", "relative.md"):
            fixture.argv[-1] = value
            fixture.assert_denied("body file must be an absolute regular file")
        fixture.argv = original_argv[:]
        link = Path(tmp) / "body-link.md"
        link.symlink_to(fixture.body)
        fixture.argv[-1] = str(link)
        fixture.assert_denied("not a symlink or stream")
        fixture.argv = [*original_argv, "--repo", "other/public"]
        fixture.assert_denied("duplicate native publication argument")
        fixture.argv = [*original_argv, "--proof-file", str(fixture.payload)]
        fixture.assert_denied("unsupported native publication argument")
        assert not fixture.verifier_calls.exists()
        fixture.argv = original_argv[:]
        fixture.argv[fixture.argv.index("--title") + 1] = "--help"
        fixture.proof["title"] = "--help"
        fixture.save()
        fixture.assert_fixture_accepted()
        fixture.proof["title"] = "fixture title"
        fixture.proof["binding"].update(
            operation="update",
            pr_url="https://github.com/fixture/public/pull/17",
            previous_body_sha256="a" * 64,
        )
        fixture.argv = [
            "pr",
            "edit",
            "17",
            "--repo",
            "fixture/public",
            "--title",
            "fixture title",
            "--body-file",
            str(fixture.body),
        ]
        fixture.save()
        fixture.assert_fixture_accepted(confirmed=False)
        fixture.proof["binding"]["before_push"] = True
        fixture.save()
        fixture.argv[2] = fixture.proof["binding"]["pr_url"]
        fixture.assert_fixture_accepted(confirmed=False)
        title_index = fixture.argv.index("--title")
        del fixture.argv[title_index : title_index + 2]
        fixture.proof["title"] = ""
        fixture.save()
        fixture.assert_fixture_accepted(confirmed=False)
        fixture.argv[2] = "18"
        fixture.assert_denied("update selector mismatch", confirmed=False)
        fixture.argv[2] = "17"
        fixture.proof["binding"]["pr_url"] = "https://[malformed"
        fixture.save()
        fixture.assert_denied("malformed native update URL", confirmed=False)
        fixture.argv += ["--base", "other"]
        fixture.assert_denied(
            "unsupported native publication argument", confirmed=False
        )


def test_native_lint_reads_cannot_outlive_the_certified_head_or_body() -> None:
    for effect, diagnostic in (
        ("head", "head_sha mismatch"),
        ("body", "exact body mismatch"),
    ):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = NativePublicationFixture(Path(tmp))
            before, payload = fixture.snapshot(), fixture.payload.read_bytes()
            fixture.env.update(
                FIXTURE_VISIBILITY_EFFECT=effect, FIXTURE_BODY_FILE=str(fixture.body)
            )
            result = fixture.native(guard=True)
            assert result.returncode == 1, (result.stdout, result.stderr)
            assert diagnostic in result.stderr, result.stderr
            assert result.stdout == ""
            assert not fixture.publications.exists()
            assert fixture.snapshot() == before
            assert fixture.payload.read_bytes() == payload
            calls = [
                json.loads(line)["argv"]
                for line in fixture.verifier_calls.read_text().splitlines()
            ]
            assert (
                sum(argv[:3] == ["axi", "publication", "verify"] for argv in calls) == 2
            )


def test_native_fork_selector_preserves_exact_utf8_body_bytes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fixture = NativePublicationFixture(Path(tmp))
        fixture.body.write_bytes(
            "I preserve café and CRLF in this fixture.\r\n\r\n".encode("utf-8")
        )
        fixture.proof = fixture.protocol_fixture()
        fixture.proof["binding"]["head_repository"] = "fork/public"
        fixture.argv[fixture.argv.index("--head") + 1] = "fork:fixture/proof"
        fixture.save()
        fixture.assert_fixture_accepted()
        fixture.body.write_bytes(fixture.body.read_bytes().replace(b"\r\n", b"\n"))
        fixture.assert_denied("exact body mismatch")


def test_native_proof_does_not_bypass_body_visibility_or_help_value_rules() -> None:
    for title in ("fixture title", "--help", "-h", "help"):
        for body, diagnostic in (
            ("This PR adds a guard.", "no-this-pr-subject"),
            (
                "I fixed https://github.com/fixture/private/pull/1.",
                "no-private-repo-ref",
            ),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                fixture = NativePublicationFixture(Path(tmp))
                fixture.body.write_text(body, encoding="utf-8")
                fixture.proof = fixture.protocol_fixture()
                fixture.proof["title"] = title
                fixture.argv[fixture.argv.index("--title") + 1] = title
                fixture.save()
                before, payload = fixture.snapshot(), fixture.payload.read_bytes()
                checked = fixture.native()
                assert checked.returncode == 0, checked.stderr
                guarded = fixture.native(guard=True)
                assert guarded.returncode == 1, (guarded.stdout, guarded.stderr)
                assert diagnostic in guarded.stderr, guarded.stderr
                assert guarded.stdout == ""
                assert not fixture.publications.exists()
                assert fixture.snapshot() == before
                assert fixture.payload.read_bytes() == payload


def test_legacy_marker_floors_through_entry_points() -> None:
    """Every artifact and exact byte floor is enforced before native mutation."""
    with tempfile.TemporaryDirectory() as tmp:
        fixture = PublicationFixture(Path(tmp))
        fixture.populate_legacy_markers()
        fixture.assert_legacy_publication_intercepted()
        originals = fixture.snapshot()
        for name, floor in (
            ("code-review", 200),
            ("plan", 120),
            ("demo", 120),
            ("pr-review", 120),
            ("tests", 120),
        ):
            path = fixture.paths[name]
            path.unlink()
            fixture.assert_markers_block_publication(name)
            headers = b"".join(
                line
                for line in originals[name].splitlines(keepends=True)
                if line.startswith(b"<!--")
            )
            assert len(headers) < floor
            for size in (0, floor - 1, floor, floor + 1):
                content = (headers + b"x" * floor)[:size]
                path.write_bytes(content)
                if size < floor:
                    fixture.assert_markers_block_publication(name)
                else:
                    fixture.assert_legacy_publication_intercepted()
            path.write_bytes(originals[name])


def test_legacy_marker_rejections_stop_publication() -> None:
    """Invalid pins, review metadata, and test results cannot reach the sink."""
    with tempfile.TemporaryDirectory() as tmp:
        fixture = PublicationFixture(Path(tmp))
        fixture.populate_legacy_markers()
        originals = fixture.snapshot()
        for name in ("code-review", "tests"):
            fixture.paths[name].write_bytes(
                originals[name].replace(fixture.head.encode(), b"0" * 40)
            )
            fixture.assert_markers_block_publication(name)
            fixture.paths[name].write_bytes(originals[name])
        for name in ("plan", "code-review", "pr-review"):
            for models in (b"", b"a, A, b", b"a, Gemini-3, b"):
                fixture.paths[name].write_bytes(
                    originals[name].replace(b"a, b, c", models)
                )
                fixture.assert_markers_block_publication(name)
            fixture.paths[name].write_bytes(originals[name])
        convergence = b"<!-- review-convergence: clean; rounds: 2 -->\n"
        for replacement in (b"", convergence * 2, convergence.replace(b"2", b"100")):
            fixture.paths["code-review"].write_bytes(
                originals["code-review"].replace(convergence, replacement)
            )
            fixture.assert_markers_block_publication("code-review")
        fixture.paths["code-review"].write_bytes(originals["code-review"])
        fixture.paths["tests"].write_bytes(
            originals["tests"].replace(
                b"<!-- tests-result: passed -->", b"no passing result"
            )
        )
        fixture.assert_markers_block_publication("tests")


def test_legacy_confirmation_and_body_rules_stop_publication() -> None:
    """Valid markers alone cannot authorize a create or bypass body linting."""
    with tempfile.TemporaryDirectory() as tmp:
        fixture = PublicationFixture(Path(tmp))
        fixture.populate_legacy_markers()
        before = fixture.snapshot()
        for args, confirmed, diagnostic in (
            ([], False, "ZACK_CONFIRMED_PR_CREATE"),
            (["--body", "This PR adds a guard."], True, "no-this-pr-subject"),
            (
                ["--body", "I fixed https://github.com/fixture/private/pull/1."],
                True,
                "no-private-repo-ref",
            ),
        ):
            result = fixture.create(*args, confirmed=confirmed)
            assert result.returncode == 1, result.stderr
            assert diagnostic in result.stderr, result.stderr
            assert (
                not fixture.publications.exists()
            ), "rejected create reached native publication"
            assert fixture.snapshot() == before


def test_gh_guard_blocks_noninteractive_draft_conversion() -> None:
    """Automation cannot reverse a manual ready-for-review transition."""
    for flag in ("--undo", "--undo=true"):
        result = _gh_guard_ready(["pr", "ready", "32128", flag])

        assert result.returncode == 1
        assert "ZACK_CONFIRMED_PR_DRAFT=1" in result.stderr


def test_gh_guard_allows_explicit_draft_confirmation() -> None:
    """An explicit current-task confirmation can pass the draft guard."""
    result = _gh_guard_ready(["pr", "ready", "32128", "--undo"], confirmed_draft=True)

    assert result.returncode == 0
    assert result.stdout.strip() == "pr ready 32128 --undo"


def test_pin_roundtrip() -> None:
    """A pinned marker records HEAD and goes stale once HEAD advances."""
    restore = Path.cwd()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            _run("git", "init", "-q")
            # Configure a repo-local identity so the commits work in a fresh
            # container without an ambient git user.name / user.email.
            _run("git", "config", "user.email", "test@example.com")
            _run("git", "config", "user.name", "pr-marker test")
            _run("git", "checkout", "-q", "-b", "feat/pin")
            _run("git", "commit", "-q", "--allow-empty", "-m", "c1")
            code = pr_marker.KINDS["code-review"]
            content = "code review synthesis. " * 12  # > 200 bytes
            payload = Path(tmp) / "body.md"
            payload.write_text(content, encoding="utf-8")
            assert (
                pr_marker.main(
                    [
                        "write",
                        "code-review",
                        str(payload),
                        "--models",
                        "opus-4.8,sonnet-4.6,gpt-5.5",
                        "--convergence-rounds",
                        "2",
                    ]
                )
                == 0
            )

            head1 = pr_marker.current_head()
            marker = pr_marker.marker_path(code, branch="feat/pin")
            assert pr_marker.read_reviewed_commit(marker) == head1
            assert pr_marker.read_convergence_rounds(marker) == 2
            ok, detail, _size, _path = pr_marker.marker_status(code, "feat/pin")
            assert ok and detail == "ok"

            _run("git", "commit", "-q", "--allow-empty", "-m", "c2")
            ok, detail, _size, _path = pr_marker.marker_status(code, "feat/pin")
            assert not ok and detail == "stale", detail
    finally:
        os.chdir(restore)


def test_run_tests() -> None:
    """run-tests writes a pinned tests marker only when every command passes."""
    restore = Path.cwd()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            _run("git", "init", "-q")
            _run("git", "config", "user.email", "test@example.com")
            _run("git", "config", "user.name", "pr-marker test")
            _run("git", "checkout", "-q", "-b", "feat/tests")
            _run("git", "commit", "-q", "--allow-empty", "-m", "c1")
            tests = pr_marker.KINDS["tests"]
            marker = pr_marker.marker_path(tests, branch="feat/tests")

            # A failing command writes nothing and exits non-zero.
            assert pr_marker.main(["run-tests", "--cmd", "false"]) == 1
            assert not marker.exists()

            # A failure among passing commands still writes nothing.
            assert pr_marker.main(["run-tests", "--cmd", "true", "--cmd", "false"]) == 1
            assert not marker.exists()

            # All commands passing writes a HEAD-pinned marker recording them.
            assert pr_marker.main(["run-tests", "--cmd", "true", "--cmd", "true"]) == 0
            head1 = pr_marker.current_head()
            assert marker.exists()
            assert pr_marker.read_reviewed_commit(marker) == head1
            body = marker.read_text(encoding="utf-8")
            assert "RESULT: passed" in body
            assert "exit 0" in body
            assert "Test-quality evidence preflight:" in body
            assert "- evidence: not-applicable" in body
            # The machine-produced result header must be present (the gate keys on it).
            assert pr_marker.TESTS_RESULT_HEADER in body
            assert pr_marker.has_result_header(marker)
            ok, detail, _size, _path = pr_marker.marker_status(tests, "feat/tests")
            assert ok and detail == "ok", detail

            # A later failing run must invalidate the prior passing marker, so a
            # green marker can never outlive a subsequent red run.
            assert pr_marker.main(["run-tests", "--cmd", "false"]) == 1
            assert not marker.exists()

            # An empty (or whitespace-only) command is rejected before running,
            # so `run-tests --cmd ''` cannot fabricate a passing marker.
            assert pr_marker.main(["run-tests", "--cmd", "   "]) == 1
            assert not marker.exists()

            # Re-establish a green marker, then confirm a later commit makes the
            # pinned tests marker stale.
            assert pr_marker.main(["run-tests", "--cmd", "true"]) == 0
            assert marker.exists()
            _run("git", "commit", "-q", "--allow-empty", "-m", "c2")
            ok, detail, _size, _path = pr_marker.marker_status(tests, "feat/tests")
            assert not ok and detail == "stale", detail
    finally:
        os.chdir(restore)


def test_test_quality_preflight() -> None:
    """run-tests blocks source-only diffs, then records tests or a waiver."""
    restore = Path.cwd()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            _run("git", "init", "-q")
            _run("git", "config", "user.email", "test@example.com")
            _run("git", "config", "user.name", "pr-marker test")
            _run("git", "checkout", "-q", "-b", "main")
            Path("README.md").write_text("# base\n", encoding="utf-8")
            _run("git", "add", "README.md")
            _run("git", "commit", "-q", "-m", "base")
            _run("git", "checkout", "-q", "-b", "feat/quality")

            Path("tool.py").write_text("def value():\n    return 1\n", encoding="utf-8")
            _run("git", "add", "tool.py")
            _run("git", "commit", "-q", "-m", "source")
            tests = pr_marker.KINDS["tests"]
            marker = pr_marker.marker_path(tests, branch="feat/quality")

            assert (
                pr_marker.main(["run-tests", "--base-ref", "main", "--cmd", "true"])
                == 1
            )
            assert not marker.exists()

            Path("test_tool.py").write_text(
                "from tool import value\n\n"
                "def test_value():\n"
                "    assert value() == 1\n",
                encoding="utf-8",
            )
            _run("git", "add", "test_tool.py")
            _run("git", "commit", "-q", "-m", "test")
            assert (
                pr_marker.main(["run-tests", "--base-ref", "main", "--cmd", "true"])
                == 0
            )
            body = marker.read_text(encoding="utf-8")
            assert "- evidence: present" in body
            assert "- executable source files changed: 1" in body
            assert "- test files changed: 1" in body

        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            _run("git", "init", "-q")
            _run("git", "config", "user.email", "test@example.com")
            _run("git", "config", "user.name", "pr-marker test")
            _run("git", "checkout", "-q", "-b", "main")
            _run("git", "commit", "-q", "--allow-empty", "-m", "base")
            _run("git", "checkout", "-q", "-b", "feat/waiver")
            Path("tool.py").write_text("def value():\n    return 1\n", encoding="utf-8")
            _run("git", "add", "tool.py")
            _run("git", "commit", "-q", "-m", "source")
            reason = "Generated output is verified by the schema compatibility command."

            assert (
                pr_marker.main(
                    [
                        "run-tests",
                        "--base-ref",
                        "main",
                        "--no-test-change-reason",
                        reason,
                        "--cmd",
                        "true",
                    ]
                )
                == 0
            )
            marker = pr_marker.marker_path(
                pr_marker.KINDS["tests"], branch="feat/waiver"
            )
            body = marker.read_text(encoding="utf-8")
            assert "- evidence: waived" in body
            assert f"- no-test-change reason: {reason}" in body

        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            _run("git", "init", "-q")
            _run("git", "config", "user.email", "test@example.com")
            _run("git", "config", "user.name", "pr-marker test")
            _run("git", "checkout", "-q", "-b", "feature-only")
            _run("git", "commit", "-q", "--allow-empty", "-m", "c1")
            _run("git", "commit", "-q", "--allow-empty", "-m", "c2")

            assert pr_marker.main(["run-tests", "--cmd", "true"]) == 1
            marker = pr_marker.marker_path(
                pr_marker.KINDS["tests"], branch="feature-only"
            )
            assert not marker.exists()

        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            _run("git", "init", "-q")
            _run("git", "config", "user.email", "test@example.com")
            _run("git", "config", "user.name", "pr-marker test")
            _run("git", "checkout", "-q", "-b", "feat/invalid-base")
            _run("git", "commit", "-q", "--allow-empty", "-m", "base")

            assert (
                pr_marker.main(["run-tests", "--base-ref", "missing", "--cmd", "true"])
                == 1
            )
            marker = pr_marker.marker_path(
                pr_marker.KINDS["tests"], branch="feat/invalid-base"
            )
            assert not marker.exists()

        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            _run("git", "init", "-q")
            _run("git", "config", "user.email", "test@example.com")
            _run("git", "config", "user.name", "pr-marker test")
            _run("git", "checkout", "-q", "-b", "feat/legacy-checker")
            _run("git", "commit", "-q", "--allow-empty", "-m", "base")
            original_checker_path = pr_marker.test_quality_checker_path
            pr_marker.test_quality_checker_path = lambda: Path(tmp) / "missing-check.py"
            try:
                assert pr_marker.main(["run-tests", "--cmd", "true"]) == 0
            finally:
                pr_marker.test_quality_checker_path = original_checker_path
            marker = pr_marker.marker_path(
                pr_marker.KINDS["tests"], branch="feat/legacy-checker"
            )
            body = marker.read_text(encoding="utf-8")
            assert "- status: unavailable" in body
            assert "- evidence: unavailable" in body
    finally:
        os.chdir(restore)


def test_run_tests_requires_clean_repo() -> None:
    """run-tests refuses dirty inputs and commands that dirty the repository."""
    restore = Path.cwd()
    try:
        for dirty_kind in ("unstaged", "staged", "untracked"):
            with tempfile.TemporaryDirectory() as tmp:
                os.chdir(tmp)
                _run("git", "init", "-q")
                _run("git", "config", "user.email", "test@example.com")
                _run("git", "config", "user.name", "pr-marker test")
                _run("git", "checkout", "-q", "-b", f"feat/{dirty_kind}")
                tracked = Path("tracked.txt")
                tracked.write_text("base\n", encoding="utf-8")
                _run("git", "add", "tracked.txt")
                _run("git", "commit", "-q", "-m", "base")

                if dirty_kind == "unstaged":
                    tracked.write_text("changed\n", encoding="utf-8")
                elif dirty_kind == "staged":
                    tracked.write_text("changed\n", encoding="utf-8")
                    _run("git", "add", "tracked.txt")
                else:
                    Path("untracked.txt").write_text("new\n", encoding="utf-8")

                assert pr_marker.main(["run-tests", "--cmd", "true"]) == 1
                marker = pr_marker.marker_path(
                    pr_marker.KINDS["tests"], branch=f"feat/{dirty_kind}"
                )
                assert not marker.exists()

        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            _run("git", "init", "-q")
            _run("git", "config", "user.email", "test@example.com")
            _run("git", "config", "user.name", "pr-marker test")
            _run("git", "checkout", "-q", "-b", "feat/test-dirties")
            _run("git", "commit", "-q", "--allow-empty", "-m", "base")

            assert (
                pr_marker.main(
                    [
                        "run-tests",
                        "--cmd",
                        f"{sys.executable} -c \"open('generated.txt', 'w').write('x')\"",
                    ]
                )
                == 1
            )
            marker = pr_marker.marker_path(
                pr_marker.KINDS["tests"], branch="feat/test-dirties"
            )
            assert not marker.exists()
    finally:
        os.chdir(restore)


def test_run_tests_rejects_git_state_changes() -> None:
    """run-tests refuses commands that move HEAD or switch branches."""
    restore = Path.cwd()
    try:
        cases = [
            ("feat/head-move", "git commit -q --allow-empty -m c2"),
            ("feat/branch-move", "git checkout -q -b other"),
            ("feat/detach", "git checkout -q --detach"),
        ]
        for branch, command in cases:
            with tempfile.TemporaryDirectory() as tmp:
                os.chdir(tmp)
                _run("git", "init", "-q")
                _run("git", "config", "user.email", "test@example.com")
                _run("git", "config", "user.name", "pr-marker test")
                _run("git", "checkout", "-q", "-b", branch)
                _run("git", "commit", "-q", "--allow-empty", "-m", "c1")
                marker = pr_marker.marker_path(pr_marker.KINDS["tests"], branch=branch)

                assert pr_marker.main(["run-tests", "--cmd", command]) == 1
                assert not marker.exists()
    finally:
        os.chdir(restore)


def test_tests_marker_is_machine_only() -> None:
    """`write tests` is rejected, and a hand-forged marker lacks the result header."""
    restore = Path.cwd()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            _run("git", "init", "-q")
            _run("git", "config", "user.email", "test@example.com")
            _run("git", "config", "user.name", "pr-marker test")
            _run("git", "checkout", "-q", "-b", "feat/tests-write")
            _run("git", "commit", "-q", "--allow-empty", "-m", "c1")
            tests = pr_marker.KINDS["tests"]
            marker = pr_marker.marker_path(tests, branch="feat/tests-write")

            # `pr-marker write tests` must refuse: the tests marker is machine-produced.
            # The reject happens before the content is read, so a real file over the
            # byte floor still fails.
            src = Path(tmp) / "payload.md"
            src.write_text("x" * 300, encoding="utf-8")
            assert pr_marker.main(["write", "tests", str(src)]) == 1
            assert not marker.exists()

            # A hand-forged file over the byte floor with a reviewed-commit but no
            # result header must still fail marker_status (the gate keys on the
            # machine-produced header, not just size + pin).
            head = pr_marker.current_head()
            marker.parent.mkdir(parents=True, exist_ok=True)
            forged = (
                f"{pr_marker.REVIEWED_COMMIT_PREFIX}{head}"
                f"{pr_marker.REVIEWED_COMMIT_SUFFIX}\n" + ("y" * 300) + "\n"
            )
            marker.write_text(forged, encoding="utf-8")
            ok, detail, _size, _path = pr_marker.marker_status(
                tests, "feat/tests-write"
            )
            assert not ok and detail == "no result", detail
    finally:
        os.chdir(restore)


def test_parse_models() -> None:
    """parse_models trims, drops empties, and dedupes case-insensitively."""
    assert pr_marker.parse_models(None) == []
    assert pr_marker.parse_models("") == []
    assert pr_marker.parse_models("opus") == ["opus"]
    assert pr_marker.parse_models(" opus , sonnet ,gpt ") == ["opus", "sonnet", "gpt"]
    # Case-insensitive dedupe preserves first-seen casing and order.
    assert pr_marker.parse_models("Opus, opus, SONNET, sonnet") == ["Opus", "SONNET"]
    assert pr_marker.parse_models("a,,b,  ,c") == ["a", "b", "c"]


def test_models_provenance() -> None:
    """Review markers require >=3 distinct models; demo is exempt.

    Also pins that the pinned code-review marker keeps the reviewed-commit header
    on line 1 (so gh-guard's line-1 sed still works) while carrying the models
    header too, and that gh-guard's shell counter agrees with the Python reader.
    """
    restore = Path.cwd()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            _run("git", "init", "-q")
            _run("git", "config", "user.email", "test@example.com")
            _run("git", "config", "user.name", "pr-marker test")
            _run("git", "checkout", "-q", "-b", "feat/models")
            _run("git", "commit", "-q", "--allow-empty", "-m", "c1")

            prr = pr_marker.KINDS["pr-review"]
            body = Path(tmp) / "body.md"
            body.write_text("PR description review synthesis. " * 8, encoding="utf-8")

            # No --models: refused.
            assert pr_marker.main(["write", "pr-review", str(body)]) == 1
            # Fewer than 3 distinct (dupes collapse): refused.
            assert (
                pr_marker.main(
                    ["write", "pr-review", str(body), "--models", "opus, opus"]
                )
                == 1
            )
            # Gemini models are prohibited even when 3 distinct models are named.
            assert (
                pr_marker.main(
                    [
                        "write",
                        "pr-review",
                        str(body),
                        "--models",
                        "opus-4.8,gemini-3.7-flash,gpt-5.5",
                    ]
                )
                == 1
            )
            # Three distinct: accepted.
            assert (
                pr_marker.main(
                    [
                        "write",
                        "pr-review",
                        str(body),
                        "--models",
                        "opus-4.8, sonnet-4.6, gpt-5.5",
                    ]
                )
                == 0
            )
            marker = pr_marker.marker_path(prr, branch="feat/models")
            assert pr_marker.read_reviewed_models(marker) == [
                "opus-4.8",
                "sonnet-4.6",
                "gpt-5.5",
            ]
            ok, detail, _s, _p = pr_marker.marker_status(prr, "feat/models")
            assert ok and detail == "ok", detail
            assert bash_count_models(str(marker)) == 3

            marker.write_text(
                "<!-- reviewed-by-models: opus-4.8, Gemini-3.7-Flash, gpt-5.5 -->\n"
                + ("PR description review synthesis. " * 8),
                encoding="utf-8",
            )
            ok, detail, _s, _p = pr_marker.marker_status(prr, "feat/models")
            assert not ok and detail == "prohibited models", detail

            # Strip the models header -> status reports "needs models".
            kept = [
                ln
                for ln in marker.read_text(encoding="utf-8").splitlines()
                if not ln.startswith(pr_marker.REVIEWED_MODELS_PREFIX)
            ]
            marker.write_text("\n".join(kept) + "\n", encoding="utf-8")
            ok, detail, _s, _p = pr_marker.marker_status(prr, "feat/models")
            assert not ok and detail == "needs models", detail
            # The bash counter must also see 0 here (the fail-open regression case).
            assert bash_count_models(str(marker)) == 0

            # Pinned code-review: reviewed-commit on line 1, models header too.
            cr = pr_marker.KINDS["code-review"]
            crbody = Path(tmp) / "cr.md"
            crbody.write_text("code review synthesis. " * 12, encoding="utf-8")
            assert (
                pr_marker.main(
                    [
                        "write",
                        "code-review",
                        str(crbody),
                        "--models",
                        "opus-4.8,sonnet-4.6,gpt-5.5",
                    ]
                )
                == 1
            )
            assert (
                pr_marker.main(
                    [
                        "write",
                        "code-review",
                        str(crbody),
                        "--models",
                        "opus-4.8,sonnet-4.6,gpt-5.5",
                        "--convergence-rounds",
                        "3",
                    ]
                )
                == 0
            )
            crpath = pr_marker.marker_path(cr, branch="feat/models")
            first = crpath.read_text(encoding="utf-8").splitlines()[0]
            assert first.startswith(pr_marker.REVIEWED_COMMIT_PREFIX), first
            assert pr_marker.read_reviewed_models(crpath) == [
                "opus-4.8",
                "sonnet-4.6",
                "gpt-5.5",
            ]
            assert pr_marker.read_convergence_rounds(crpath) == 3
            ok, detail, _s, _p = pr_marker.marker_status(cr, "feat/models")
            assert ok and detail == "ok", detail

            # Demo is exempt: no --models needed.
            demo = pr_marker.KINDS["demo"]
            demobody = Path(tmp) / "demo.md"
            demobody.write_text("N/A - no visual surface. " * 6, encoding="utf-8")
            assert pr_marker.main(["write", "demo", str(demobody)]) == 0
            ok, detail, _s, _p = pr_marker.marker_status(demo, "feat/models")
            assert ok and detail == "ok", detail
    finally:
        os.chdir(restore)


def test_convergence_parsing_parity() -> None:
    """Python and gh-guard accept only one canonical clean-round header."""
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "code-review.md"
        cases = [
            (["<!-- review-convergence: clean; rounds: 2 -->"], 2),
            (["<!-- review-convergence: clean; rounds: 8 -->"], 8),
            (["<!-- review-convergence: clean; rounds: 99 -->"], 99),
            (["<!-- review-convergence: clean; rounds:  2 -->"], 0),
            (["<!-- review-convergence: clean; rounds: two -->"], 0),
            (["<!-- review-convergence: clean; rounds: 100 -->"], 0),
            (["<!-- review-convergence: clean; rounds: 999999999999999999 -->"], 0),
            (
                [
                    "<!-- review-convergence: clean; rounds: two -->",
                    "<!-- review-convergence: clean; rounds: 2 -->",
                ],
                0,
            ),
            (
                [
                    "<!-- review-convergence: clean; rounds: 2 -->",
                    "<!-- review-convergence: clean; rounds: 2 -->",
                ],
                0,
            ),
        ]
        for headers, expected in cases:
            marker.write_text("\n".join(headers) + "\n", encoding="utf-8")
            assert (pr_marker.read_convergence_rounds(marker) or 0) == expected
            assert bash_convergence_rounds(str(marker)) == expected


def test_code_review_marker_rewrite() -> None:
    """Rewriting an existing marker replaces generated headers instead of duplicating."""
    restore = Path.cwd()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            _run("git", "init", "-q")
            _run("git", "config", "user.email", "test@example.com")
            _run("git", "config", "user.name", "pr-marker test")
            _run("git", "checkout", "-q", "-b", "feat/rewrite")
            _run("git", "commit", "-q", "--allow-empty", "-m", "c1")
            body = Path(tmp) / "body.md"
            body.write_text("code review synthesis. " * 12, encoding="utf-8")
            args = [
                "write",
                "code-review",
                str(body),
                "--models",
                "a,b,c",
                "--convergence-rounds",
                "2",
            ]
            assert pr_marker.main(args) == 0
            marker = pr_marker.marker_path(
                pr_marker.KINDS["code-review"], branch="feat/rewrite"
            )
            rewrite_args = [
                "write",
                "code-review",
                str(marker),
                "--models",
                "d,e,f",
                "--convergence-rounds",
                "3",
            ]
            assert pr_marker.main(rewrite_args) == 0
            lines = marker.read_text(encoding="utf-8").splitlines()
            assert (
                sum(line.startswith(pr_marker.REVIEWED_COMMIT_PREFIX) for line in lines)
                == 1
            )
            assert (
                sum(line.startswith(pr_marker.REVIEWED_MODELS_PREFIX) for line in lines)
                == 1
            )
            assert (
                sum(line.startswith("<!-- review-convergence:") for line in lines) == 1
            )
            assert pr_marker.read_reviewed_models(marker) == ["d", "e", "f"]
            assert pr_marker.read_convergence_rounds(marker) == 3
            ok, detail, _size, _path = pr_marker.marker_status(
                pr_marker.KINDS["code-review"], "feat/rewrite"
            )
            assert ok and detail == "ok", detail
    finally:
        os.chdir(restore)


def test_models_argv_order() -> None:
    """--models parses in either position, incl. the `--models a,b,c -` form."""
    # Optional-before-positional (the natural / gh-guard-suggested order).
    assert pr_marker._reorder_write_argv(
        ["write", "pr-review", "--models", "a,b,c", "-"]
    ) == ["write", "pr-review", "-", "--models", "a,b,c"]
    # --models=VALUE form, also before the positional.
    assert pr_marker._reorder_write_argv(
        [
            "write",
            "code-review",
            "--models=a,b,c",
            "--convergence-rounds",
            "2",
            "body.md",
        ]
    ) == [
        "write",
        "code-review",
        "body.md",
        "--models=a,b,c",
        "--convergence-rounds",
        "2",
    ]
    # Already-trailing optional is left in a working order.
    assert pr_marker._reorder_write_argv(
        ["write", "plan", "-", "--models", "a,b,c"]
    ) == ["write", "plan", "-", "--models", "a,b,c"]
    # Non-write commands are untouched.
    assert pr_marker._reorder_write_argv(["status"]) == ["status"]


def test_gh_guard_gate() -> None:
    """The real gh-guard gate blocks insufficient or prohibited review models.

    Drives the actual gh-guard binary (not a Python reimplementation) against a
    fake `gh`, so it exercises count_review_models/eval_marker for real. Covers
    the header-less 0-model case that previously failed open and Gemini models.
    """
    restore = Path.cwd()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            fake_bin = tmp_p / "bin"
            fake_bin.mkdir()
            gh = fake_bin / "gh"
            gh.write_text("#!/bin/bash\necho FAKE-GH-EXECUTED\n", encoding="utf-8")
            gh.chmod(0o755)

            repo = tmp_p / "repo"
            repo.mkdir()
            os.chdir(repo)
            _run("git", "init", "-q")
            _run("git", "config", "user.email", "t@e.com")
            _run("git", "config", "user.name", "t")
            _run("git", "checkout", "-q", "-b", "feat/gate")
            _run("git", "commit", "-q", "--allow-empty", "-m", "c1")

            body = tmp_p / "body.md"
            body.write_text("review synthesis content padding. " * 6, encoding="utf-8")
            crbody = tmp_p / "cr.md"
            crbody.write_text("code review synthesis padding. " * 10, encoding="utf-8")
            demobody = tmp_p / "demo.md"
            demobody.write_text("N/A no visual surface here. " * 6, encoding="utf-8")

            assert (
                pr_marker.main(["write", "plan", str(body), "--models", "a,b,c"]) == 0
            )
            assert pr_marker.main(["write", "demo", str(demobody)]) == 0
            assert (
                pr_marker.main(
                    [
                        "write",
                        "code-review",
                        str(crbody),
                        "--models",
                        "a,b,c",
                        "--convergence-rounds",
                        "2",
                    ]
                )
                == 0
            )
            crpath = pr_marker.marker_path(
                pr_marker.KINDS["code-review"], branch="feat/gate"
            )
            without_convergence = [
                line
                for line in crpath.read_text(encoding="utf-8").splitlines()
                if not line.startswith(pr_marker.REVIEW_CONVERGENCE_PREFIX)
            ]
            invalid_headers = [
                [],
                ["<!-- review-convergence: clean; rounds:  2 -->"],
                ["<!-- review-convergence: clean; rounds: 999999999999999999 -->"],
                [
                    "<!-- review-convergence: clean; rounds: two -->",
                    "<!-- review-convergence: clean; rounds: 2 -->",
                ],
                [
                    "<!-- review-convergence: clean; rounds: 2 -->",
                    "<!-- review-convergence: clean; rounds: 2 -->",
                ],
            ]
            for headers in invalid_headers:
                crpath.write_text(
                    "\n".join(
                        [*without_convergence[:2], *headers, *without_convergence[2:]]
                    )
                    + "\n",
                    encoding="utf-8",
                )
                ok, detail, _size, _path = pr_marker.marker_status(
                    pr_marker.KINDS["code-review"], "feat/gate"
                )
                assert not ok and detail == "needs convergence", (headers, detail)
                res = _gh_guard_create(repo, fake_bin)
                out = res.stdout + res.stderr
                assert res.returncode == 1, (headers, out)
                assert "NO-CONVERGENCE" in out, (headers, out)
                assert "FAKE-GH-EXECUTED" not in out, (headers, out)
            assert (
                pr_marker.main(
                    [
                        "write",
                        "code-review",
                        str(crbody),
                        "--models",
                        "a,b,c",
                        "--convergence-rounds",
                        "2",
                    ]
                )
                == 0
            )
            prr = pr_marker.marker_path(
                pr_marker.KINDS["pr-review"], branch="feat/gate"
            )
            prr.parent.mkdir(parents=True, exist_ok=True)

            # (1) Header-less pr-review over the byte floor -> gate must BLOCK.
            prr.write_text("pr description review synthesis. " * 6, encoding="utf-8")
            res = _gh_guard_create(repo, fake_bin)
            out = res.stdout + res.stderr
            assert res.returncode == 1, out
            assert "FEW-MODELS" in out, out
            assert "FAKE-GH-EXECUTED" not in out, out

            # (2) Two distinct models -> still BLOCK.
            prr.write_text(
                "<!-- reviewed-by-models: a, b -->\n"
                + "pr description review synthesis. " * 6,
                encoding="utf-8",
            )
            res = _gh_guard_create(repo, fake_bin)
            out = res.stdout + res.stderr
            assert res.returncode == 1, out
            assert "FEW-MODELS" in out, out

            # (3) A Gemini model -> BLOCK even with three distinct models.
            prr.write_text(
                "<!-- reviewed-by-models: a, Gemini-3.7-Flash, c -->\n"
                + "pr description review synthesis. " * 6,
                encoding="utf-8",
            )
            res = _gh_guard_create(repo, fake_bin)
            out = res.stdout + res.stderr
            assert res.returncode == 1, out
            assert "PROHIBITED-MODEL" in out, out
            assert "FAKE-GH-EXECUTED" not in out, out

            # (4) Three allowed models -> still BLOCK until the tests marker exists.
            assert (
                pr_marker.main(["write", "pr-review", str(body), "--models", "a,b,c"])
                == 0
            )
            res = _gh_guard_create(repo, fake_bin)
            out = res.stdout + res.stderr
            assert res.returncode == 1, out
            assert "FAKE-GH-EXECUTED" not in out, out

            # (4b) A forged tests marker over the byte floor and correctly pinned to
            # HEAD, but WITHOUT the machine-produced result header, must still BLOCK
            # with NO-RESULT (the gate keys on the header, not size + pin).
            tests_path = pr_marker.marker_path(
                pr_marker.KINDS["tests"], branch="feat/gate"
            )
            head = pr_marker.current_head()
            tests_path.parent.mkdir(parents=True, exist_ok=True)
            tests_path.write_text(
                f"{pr_marker.REVIEWED_COMMIT_PREFIX}{head}"
                f"{pr_marker.REVIEWED_COMMIT_SUFFIX}\n" + ("z" * 300) + "\n",
                encoding="utf-8",
            )
            res = _gh_guard_create(repo, fake_bin)
            out = res.stdout + res.stderr
            assert res.returncode == 1, out
            assert "NO-RESULT" in out, out
            assert "FAKE-GH-EXECUTED" not in out, out

            # (4c) The result header embedded MID-LINE (not on its own line) must
            # also BLOCK: gh-guard's grep -qxF matches whole lines only, agreeing
            # with pr-marker's exact-line membership test (no substring bypass).
            tests_path.write_text(
                f"{pr_marker.REVIEWED_COMMIT_PREFIX}{head}"
                f"{pr_marker.REVIEWED_COMMIT_SUFFIX}\n"
                f"prefix {pr_marker.TESTS_RESULT_HEADER} suffix\n" + ("z" * 300) + "\n",
                encoding="utf-8",
            )
            res = _gh_guard_create(repo, fake_bin)
            out = res.stdout + res.stderr
            assert res.returncode == 1, out
            assert "NO-RESULT" in out, out
            assert "FAKE-GH-EXECUTED" not in out, out

            # (5) Machine-produced tests marker present -> PASS (execs the fake gh).
            assert pr_marker.main(["run-tests", "--cmd", "true"]) == 0
            res = _gh_guard_create(repo, fake_bin)
            out = res.stdout + res.stderr
            assert res.returncode == 0, out
            assert "FAKE-GH-EXECUTED" in res.stdout, out
    finally:
        os.chdir(restore)


def _run(*args: str) -> None:
    subprocess.run(args, check=True, capture_output=True, text=True)


def main() -> int:
    """Run every test and report a pass/fail summary."""
    # test_* helpers resolve the marker dir via git, so run from the repo root
    # regardless of the caller's cwd (allows `python3 /abs/path/bin/test...` from
    # anywhere).
    os.chdir(Path(__file__).resolve().parents[1])
    tests = [
        test_encoding_matches_sed,
        test_known_encodings,
        test_kinds_and_thresholds,
        test_branch_dir_and_paths,
        test_artifacts_dir_derivation,
        test_native_capabilities_preserve_the_consumer_policy,
        test_native_complete_fixture_does_not_require_or_write_legacy_markers,
        test_native_preparation_binding_and_admission_are_required,
        test_native_baseline_checks_and_plan_reviews_precede_implementation,
        test_native_proposed_exception_requires_bound_native_authorization,
        test_native_proposed_exception_preserves_remaining_proof_and_history,
        test_native_authorizer_pid_matches_the_native_boundary,
        test_native_authorization_reason_limit_counts_utf8_bytes,
        test_native_exception_requires_exact_native_command_manifest,
        test_native_locators_require_proof_without_legacy_fallback,
        test_native_rejected_verifier_stdout_cannot_supply_proof,
        test_native_recheck_refusal_prevents_publication,
        test_native_proof_parser_rejects_malformed_and_inspection_responses,
        test_native_proof_binds_actual_store_branch_head_body_and_call,
        test_native_context_and_body_changes_cannot_relabel_old_proof,
        test_native_uses_requested_common_store_in_a_linked_worktree,
        test_native_artifact_review_and_machine_check_requirements,
        test_native_code_budget_retains_separate_plan_and_description_history,
        test_native_body_transport_and_update_target_cannot_bypass_guard,
        test_native_lint_reads_cannot_outlive_the_certified_head_or_body,
        test_native_fork_selector_preserves_exact_utf8_body_bytes,
        test_native_proof_does_not_bypass_body_visibility_or_help_value_rules,
        test_legacy_marker_floors_through_entry_points,
        test_legacy_marker_rejections_stop_publication,
        test_legacy_confirmation_and_body_rules_stop_publication,
        test_pin_roundtrip,
        test_run_tests,
        test_test_quality_preflight,
        test_run_tests_requires_clean_repo,
        test_run_tests_rejects_git_state_changes,
        test_tests_marker_is_machine_only,
        test_parse_models,
        test_models_provenance,
        test_convergence_parsing_parity,
        test_code_review_marker_rewrite,
        test_models_argv_order,
        test_gh_guard_gate,
    ]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"ok   {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}", file=sys.stderr)
    if failures:
        print(f"\n{failures} test(s) failed", file=sys.stderr)
        return 1
    print(f"\nall {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
