"""Pytest configuration for triage-dependabot tests."""


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "no_archive_stub: opt out of the autouse is_archived_repo stub.",
    )
