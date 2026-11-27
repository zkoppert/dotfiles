"""Consumer evidence comes from immutable Git blobs and the evaluated CI revision."""

import base64
import copy
import hashlib
import json
import unittest
from unittest import mock

import dependency_context as context

HEAD = "a" * 40


class ContextAPI:
    def __init__(self):
        self.blobs = {}
        self.entries = []
        self.paths = []
        self.truncated = False
        self.checks = [
            {
                "name": "lint",
                "head_sha": HEAD,
                "status": "completed",
                "conclusion": "success",
            }
        ]
        self.statuses = [{"context": "tests", "state": "success"}]
        self.add("Makefile", b"lint:\n\tpylint --rcfile=.pylintrc *.py\n")
        self.add(".pylintrc", b"[MAIN]\nload-plugins=\n[REPORTS]\nscore=yes\n")
        self.add(
            "module.py", b"sensitive application source, not selected configuration\n"
        )

    def add(self, path, raw):
        sha = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw).hexdigest()
        self.entries.append(
            {
                "path": path,
                "type": "blob",
                "mode": "100644",
                "sha": sha,
                "size": len(raw),
            }
        )
        self.blobs[sha] = {
            "sha": sha,
            "size": len(raw),
            "encoding": "base64",
            "content": base64.encodebytes(raw).decode(),
        }

    def request(self, method, path):
        self.paths.append(path)
        if path == f"/repos/example/repo/git/trees/{HEAD}?recursive=1":
            return {"truncated": self.truncated, "tree": copy.deepcopy(self.entries)}
        prefix = "/repos/example/repo/git/blobs/"
        if path.startswith(prefix):
            return copy.deepcopy(self.blobs[path.removeprefix(prefix)])
        raise AssertionError(path)

    def pages(self, path, expression="."):
        self.paths.append(path)
        if (
            path == f"/repos/example/repo/commits/{HEAD}/check-runs?filter=latest"
            and expression == ".check_runs"
        ):
            return copy.deepcopy(self.checks)
        if path == f"/repos/example/repo/commits/{HEAD}/statuses":
            return copy.deepcopy(self.statuses)
        raise AssertionError(path)


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.api = ContextAPI()

    def collect(self):
        return context.collect_context(self.api, "example/repo", HEAD)

    def test_exact_configuration_and_ci_evidence_are_bound_to_the_head(self):
        result = self.collect()
        self.assertEqual(HEAD, result["head_sha"])
        self.assertEqual(
            [".pylintrc", "Makefile"], [f["path"] for f in result["files"]]
        )
        self.assertEqual(
            "lint:\n\tpylint --rcfile=.pylintrc *.py\n", result["files"][1]["content"]
        )
        self.assertEqual(self.api.checks, result["checks"])
        self.assertEqual(self.api.statuses, result["statuses"])
        self.assertNotIn(self.api.entries[2]["sha"], " ".join(self.api.paths))

    def test_workflows_and_runtime_installation_configuration_are_included(self):
        self.api.add(".github/workflows/linter.yaml", b"jobs: {}\n")
        self.api.add("Dockerfile", b"FROM example\n")
        self.api.add(
            "pyproject.toml", b"[dependency-groups]\ndev = ['pylint==4.1.4']\n"
        )
        self.assertEqual(5, len(self.collect()["files"]))

    def test_empty_configuration_does_not_invent_absent_usage_proof(self):
        self.api.entries = []
        result = self.collect()
        self.assertEqual([], result["files"])
        self.assertEqual(self.api.checks, result["checks"])
        self.assertEqual(
            "Selected configuration and workflow files, not a complete usage inventory.",
            result["scope"],
        )

    def test_truncated_trees_links_and_duplicate_configuration_require_more_evidence(
        self,
    ):
        self.api.truncated = True
        with self.assertRaisesRegex(ValueError, "tree is incomplete"):
            self.collect()
        self.api.truncated = False
        self.api.entries[0]["mode"] = "120000"
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            self.collect()
        self.api.entries[0]["mode"] = "100644"
        self.api.entries.append(copy.deepcopy(self.api.entries[0]))
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.collect()

    def test_wrong_blob_identity_size_encoding_or_bytes_cannot_enter_evidence(self):
        original = copy.deepcopy(self.api.blobs)
        sha = self.api.entries[0]["sha"]
        for change in (
            {"sha": "b" * 40},
            {"size": 1},
            {"encoding": "utf-8"},
            {"content": "!!!"},
            {"content": base64.b64encode(b"tampered").decode()},
            {"content": base64.b64encode(b"x" * self.api.entries[0]["size"]).decode()},
        ):
            with self.subTest(change=change):
                self.api.blobs = copy.deepcopy(original)
                self.api.blobs[sha].update(change)
                with self.assertRaises(ValueError):
                    self.collect()

    def test_checks_for_another_revision_cannot_support_compatibility(self):
        self.api.checks[0]["head_sha"] = "b" * 40
        with self.assertRaisesRegex(ValueError, "different revision"):
            self.collect()

    def test_large_files_are_held_before_blob_download_and_full_context_is_bounded(
        self,
    ):
        self.api.entries[0]["size"] = context.MAX_CONTEXT_BYTES + 1
        with self.assertRaisesRegex(ValueError, "limit"):
            self.collect()
        self.assertFalse(any("/git/blobs/" in p for p in self.api.paths))
        self.api = ContextAPI()
        size = len(json.dumps(self.collect()).encode())
        with mock.patch.object(context, "MAX_CONTEXT_BYTES", size):
            self.assertEqual(2, len(self.collect()["files"]))
        with (
            mock.patch.object(context, "MAX_CONTEXT_BYTES", size - 1),
            self.assertRaisesRegex(ValueError, "complete consumer context"),
        ):
            self.collect()


if __name__ == "__main__":
    unittest.main()
