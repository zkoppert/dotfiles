#!/usr/bin/env python3
"""Tests for record-demo scaffold behavior."""

from pathlib import Path
from unittest import TestCase

from scaffold import TEMPLATE, _matching_image_pairs


class TemplateTest(TestCase):
    """Keep generated guidance pointed at its runtime owner."""

    def test_impact_guidance_points_to_pull_request_skill(self) -> None:
        rendered = TEMPLATE.format(branch="feature", rel="artifacts")

        self.assertIn("~/.copilot/skills/pull-request-workflow/SKILL.md", rendered)
        self.assertNotIn("under Pull Requests in ~/.copilot/copilot-instructions.md", rendered)


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
