---
name: record-demo
description: This skill should be used AFTER code changes have been reviewed and tested in a Codespace, and BEFORE drafting the PR, to record a demo of the change and capture before/after images. It drives Playwright against a Codespace-forwarded app to screenshot the baseline branch and the feature branch, records a before/after video walkthrough (preferred), saves artifacts in a standard per-branch layout, and writes the `demo` marker that gh-guard's `gh pr create` gate requires. Also triggered when the user asks to "record a demo", "capture before/after", "make a demo video", or "screenshot the change in a codespace".
---

# record-demo: capture a before/after demo before opening a PR

Zack's staged change workflow is: plan, multi-model review the plan, draft changes
locally, multi-model review the code, test in a Codespace, **record a demo and
capture before/after images**, draft the PR with a multi-model review of the PR
description, human review, then push and open a draft PR. This skill owns the
demo-and-before/after step.

The output feeds the `demo` marker that gh-guard gates on. `gh pr create` stays
blocked until `<branch>/demo.md` exists (see the `pr-marker` helper below), so
running this skill is what unblocks that part of the gate.

## When to invoke

Run this once the change is reviewed and verified in a Codespace, before writing
the PR description. Two paths:

- **Visual surface** (UI, a rendered report, a CLI with meaningful output): record
  a before/after **video walkthrough** (preferred), plus matching before/after
  stills for the PR table.
- **No visual surface** (pure backend, config, workflow, or refactor with no
  user-visible output): record an explicit `N/A` justification plus an alternative
  visual aid (a before/after table, a mermaid diagram, or captured terminal
  output). The gate still requires the marker; it just documents why there is no
  screen recording. This matches the "acknowledge harness gaps explicitly" rule.

## Prerequisites

- The change is already reviewed (plan + code markers) and tested in a Codespace.
- Playwright MCP browser tools are available (`browser_navigate`,
  `browser_take_screenshot`, `browser_snapshot`, `browser_resize`, ...).
- `pr-marker` is on PATH (installed to `~/.local/bin`), or call it at
  `~/repos/dotfiles/bin/pr-marker`.

## Step 1: scaffold the artifact layout

```bash
# Creates the per-branch artifacts directory and prints a fill-in marker template.
python3 ~/.copilot/skills/record-demo/scaffold.py init
```

Artifacts live next to the review markers, in the per-branch directory under
`<git-dir>/copilot-pr-review/<branch>/demo.artifacts/`. Save before/after images
and any video there so the paths in the marker stay stable and never get
committed into the working tree.

Suggested filenames inside that directory:

- `before-<view>.png`, `after-<view>.png` (one pair per view you are changing)
- `demo.webm` (before/after screen recording, **preferred**)

## Step 2: bring up the app in a Codespace and forward the port

Start (or reuse) a Codespace on the feature branch, run the app, and forward its
port so Playwright can reach it. Non-interactive `gh codespace ssh` needs a login
shell so `GITHUB_TOKEN` is present for bootstrap:

```bash
# Get the forwarded URL for the app's port (example: 3000).
gh codespace ports forward 3000:3000 --codespace "$CS" &   # or use the Ports panel
gh codespace ssh -c "$CS" -- bash -lc 'cd /workspaces/<repo> && <start-app-command>'
```

For the **before** shots, point Playwright at the same app running on the base
branch (a second Codespace on `main`, or the deployed baseline). Capturing before
and after against the same viewport and route is what makes the comparison honest.

## Step 3: capture before/after with Playwright

Drive the browser MCP tools against the forwarded URL. Keep the viewport and route
identical across before and after:

1. `browser_resize` to a fixed size (for example 1280x800) so both shots match.
2. `browser_navigate` to the route under test.
3. `browser_snapshot` to confirm the page rendered the state you intend to show.
4. `browser_take_screenshot` and save into the artifacts directory with the
   `before-*` / `after-*` names above.

Repeat for each meaningful view. Look at each screenshot before moving on, following
the "use vision for visual work" rule; do not assume the capture is correct.

### Preferred: a recorded before/after video walkthrough

A before/after video is the preferred demo deliverable, so record one whenever the
change has a visual surface. The browser MCP captures stills, so record the
walkthrough with a Playwright script with `record_video_dir` set to the artifacts
directory, either locally against the forwarded URL or inside the Codespace.
Capture the before state (base branch) and the after state (feature branch) in the
same flow, or record one pass per branch and keep both:

```python
# playwright install chromium  (once)
from playwright.sync_api import sync_playwright

URL = "http://localhost:3000/<route>"
OUT = "<artifacts-dir>"  # from: pr-marker artifacts-dir demo

with sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context(
        viewport={"width": 1280, "height": 800},
        record_video_dir=OUT,
    )
    page = context.new_page()
    page.goto(URL)
    # ... drive the flow you want to demo ...
    context.close()  # video is flushed to OUT on context close
    browser.close()
```

## Step 4: write the demo marker

Assemble a short summary that references the captured artifacts, then write it
through `pr-marker` so it lands at the exact path the gate checks:

```bash
python3 ~/.copilot/skills/record-demo/scaffold.py init > /tmp/demo-marker.md
# edit /tmp/demo-marker.md: fill the before/after table and video note
pr-marker write demo /tmp/demo-marker.md
```

The marker is also a draft of the before/after block for the PR description, so it
does double duty. Reuse the same table and image references when you write the PR
body.

### No visual surface

```bash
python3 ~/.copilot/skills/record-demo/scaffold.py na \
  "Pure config change with no user-visible surface." \
  --alt "Before/after of the rendered config table is included in the PR body."
```

That writes a valid `N/A` demo marker (with the required alternative-aid line) and
satisfies the gate.

## Step 5: verify the capture

```bash
python3 ~/.copilot/skills/record-demo/scaffold.py check
```

`check` confirms the demo marker exists and, for a visual demo, that the artifacts
directory holds at least one non-empty image. It also warns when a visual demo has
no before/after video, since a recording is the preferred deliverable. For an `N/A`
marker it confirms the justification records an alternative visual aid. Fix any gap
it reports before moving on to the PR description.

## After this skill

Continue the workflow: write the PR description (reusing the before/after block),
run the multi-model review of the PR description and write the `pr-review` marker,
then let the human review before `ZACK_CONFIRMED_PR_CREATE=1 gh pr create ...`.
