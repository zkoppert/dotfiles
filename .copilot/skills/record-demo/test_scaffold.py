#!/usr/bin/env python3
"""Tests for record-demo scaffold behavior."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from scaffold import _matching_image_pairs, main


class TemplateTest(TestCase):
    """Keep generated guidance pointed at its runtime owner."""

    def test_init_emits_guidance_and_creates_artifacts(self) -> None:
        with TemporaryDirectory() as temporary:
            marker = Path(temporary) / "demo.md"
            artifacts = Path(temporary) / "demo.artifacts"
            stdout = StringIO()
            stderr = StringIO()
            with patch("scaffold.demo_paths", return_value=(marker, artifacts)), patch(
                "scaffold.current_branch", return_value="feature/scoped-guidance"
            ), redirect_stdout(stdout), redirect_stderr(stderr):
                result = main(["init"])

            self.assertEqual(result, 0)
            self.assertTrue(artifacts.is_dir())
            self.assertFalse(marker.exists())
            rendered = stdout.getvalue()
            self.assertIn("# Demo: feature/scoped-guidance", rendered)
            self.assertIn("![before](demo.artifacts/before-<view>.png)", rendered)
            self.assertIn("![after](demo.artifacts/after-<view>.png)", rendered)
            self.assertIn("~/.copilot/skills/pull-request-workflow/SKILL.md", rendered)
            self.assertNotIn("under Pull Requests in ~/.copilot/copilot-instructions.md", rendered)
            self.assertIn(f"artifacts dir ready at {artifacts}", stderr.getvalue())


class MatchingImagePairsTest(TestCase):
    """Verify that artifact validation requires both sides of a view."""

    def test_returns_only_views_with_before_and_after_images(self) -> None:
        images = [
            Path("before-settings.png"),
            Path("after-settings.png"),
            Path("before-dashboard.png"),
            Path("unrelated.png"),
        ]

        self.assertEqual(["settings"], _matching_image_pairs(images))

    def test_rejects_one_sided_capture(self) -> None:
        self.assertEqual([], _matching_image_pairs([Path("after-settings.png")]))
