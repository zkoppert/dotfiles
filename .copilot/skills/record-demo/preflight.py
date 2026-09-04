#!/usr/bin/env python3
"""Verify that a demo page and its required assets are ready for capture."""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser


class AssetParser(HTMLParser):
    """Collect browser assets referenced directly by the page HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.stylesheets: list[str] = []
        self.scripts: list[str] = []
        self.images: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        values = dict(attrs)
        if tag == "link" and values.get("rel") == "stylesheet" and values.get("href"):
            self.stylesheets.append(values["href"])
        elif tag == "script" and values.get("src"):
            self.scripts.append(values["src"])
        elif tag == "img" and values.get("src"):
            self.images.append(values["src"])


def fetch(url: str, timeout: float) -> tuple[int, bytes]:
    """Fetch a URL and return its HTTP status and body."""
    request = urllib.request.Request(
        url,
        headers={
            "Range": "bytes=0-1048575",
            "User-Agent": "record-demo-preflight/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(1048576)
    except urllib.error.HTTPError as error:
        return error.code, error.read(4096)
    except urllib.error.URLError as error:
        raise RuntimeError(f"{url}: {error.reason}") from error


def absolute_assets(base_url: str, assets: list[str]) -> list[str]:
    """Resolve fetchable asset URLs and remove duplicates."""
    resolved: list[str] = []
    for asset in assets:
        url = urllib.parse.urljoin(base_url, asset)
        if urllib.parse.urlparse(url).scheme not in {"http", "https"}:
            continue
        if url not in resolved:
            resolved.append(url)
    return resolved


def check_assets(label: str, urls: list[str], timeout: float) -> list[str]:
    """Return failures for referenced assets."""
    failures: list[str] = []
    for url in urls:
        try:
            status, body = fetch(url, timeout)
        except RuntimeError as error:
            failures.append(str(error))
            continue
        if status >= 400:
            failures.append(f"{url}: HTTP {status}")
        elif not body:
            failures.append(f"{url}: empty response")
    if urls:
        print(f"preflight: checked {len(urls)} {label} asset(s)")
    return failures


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        description="Fail fast when a demo page or its browser assets are incomplete."
    )
    parser.add_argument("--url", required=True, help="page URL to verify")
    parser.add_argument(
        "--expect-text",
        action="append",
        default=[],
        help="stable text that must appear in the initial HTML; repeat as needed",
    )
    parser.add_argument(
        "--require-frontend",
        action="store_true",
        help="require at least one stylesheet and one external script",
    )
    parser.add_argument(
        "--require-image",
        action="store_true",
        help="require at least one image and verify referenced images",
    )
    parser.add_argument(
        "--timeout", type=float, default=15.0, help="request timeout in seconds"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the page and asset readiness checks."""
    args = build_parser().parse_args(argv)
    try:
        status, body = fetch(args.url, args.timeout)
    except RuntimeError as error:
        sys.stderr.write(f"preflight: FAIL: {error}\n")
        return 1

    if status >= 400:
        sys.stderr.write(f"preflight: FAIL: page returned HTTP {status}: {args.url}\n")
        return 1

    html = body.decode("utf-8", errors="replace")
    missing_text = [text for text in args.expect_text if text not in html]
    if missing_text:
        sys.stderr.write(
            "preflight: FAIL: expected text missing: "
            + ", ".join(repr(text) for text in missing_text)
            + "\n"
        )
        return 1

    parser = AssetParser()
    parser.feed(html)
    stylesheets = absolute_assets(args.url, parser.stylesheets)
    scripts = absolute_assets(args.url, parser.scripts)
    images = absolute_assets(args.url, parser.images)
    print(
        f"preflight: page HTTP {status}; found {len(stylesheets)} stylesheet(s), "
        f"{len(scripts)} script(s), and {len(images)} image(s)"
    )

    if args.require_frontend and (not stylesheets or not scripts):
        sys.stderr.write(
            "preflight: FAIL: full frontend required, but the page did not reference "
            "both a stylesheet and an external script\n"
        )
        return 1
    if args.require_image and not images:
        sys.stderr.write(
            "preflight: FAIL: at least one image is required, but none were found\n"
        )
        return 1

    failures = check_assets("stylesheet", stylesheets, args.timeout)
    failures.extend(check_assets("script", scripts, args.timeout))
    if args.require_image:
        failures.extend(check_assets("image", images, args.timeout))
    if failures:
        sys.stderr.write("preflight: FAIL: required assets are unavailable\n")
        for failure in failures:
            sys.stderr.write(f"  - {failure}\n")
        return 1

    print("preflight: PASS: page and required assets are ready for demo capture")
    return 0


if __name__ == "__main__":
    sys.exit(main())
