"""Pure scope checks shared by the scanner and standard-library reporter."""

from __future__ import annotations


def in_scope(repo: str, config: dict) -> bool:
    repo = repo.lower()
    return repo not in config["exclude_repositories"] and (
        repo in config["repositories"]
        or repo.partition("/")[0] in config["organizations"]
    )


def confirmed_scope(report: dict):
    scope = report.get("scope")
    keys = ("organizations", "repositories", "exclude_repositories")
    if report.get("scope_confirmed") is not True or not isinstance(scope, dict):
        return None
    if not all(
        isinstance(scope.get(key), list)
        and all(isinstance(name, str) for name in scope[key])
        for key in keys
    ):
        return None
    return scope
