"""The scanner and reporter apply the same confirmed inclusion and exclusion policy."""

import copy
import unittest

from dependabot_scope import confirmed_scope, in_scope


class ScopeTests(unittest.TestCase):
    def test_confirmed_snapshot_controls_org_exact_and_excluded_repositories(self):
        scope = {
            "organizations": ["example"],
            "repositories": ["other/tools"],
            "exclude_repositories": ["example/.github", "other/blocked"],
        }
        report = {"scope_confirmed": True, "scope": scope}
        self.assertEqual(scope, confirmed_scope(report))
        self.assertTrue(in_scope("EXAMPLE/FUTURE", scope))
        self.assertTrue(in_scope("other/tools", scope))
        for name in ("example/.github", "other/blocked", "removed/repo"):
            self.assertFalse(in_scope(name, scope))
        for key in scope:
            invalid = copy.deepcopy(report)
            invalid["scope"][key] = "not a list"
            self.assertIsNone(confirmed_scope(invalid))
        self.assertIsNone(confirmed_scope({"scope_confirmed": True}))
        self.assertIsNone(confirmed_scope({"scope": scope}))


if __name__ == "__main__":
    unittest.main()
