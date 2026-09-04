#!/usr/bin/env python3
"""Tests for record-demo scaffold behavior."""

from pathlib import Path
from unittest import TestCase

from scaffold import _matching_image_pairs


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
