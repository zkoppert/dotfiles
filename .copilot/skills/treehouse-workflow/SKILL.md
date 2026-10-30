---
name: treehouse-workflow
description: Use Treehouse for isolated git work whenever Pi starts implementation, edits code, runs tests that can mutate a checkout, or performs parallel work in a repository. Also use when choosing, leasing, inspecting, returning, pruning, or recovering worktrees. Never create raw git worktrees when Treehouse can manage the repository.
compatibility: Requires the treehouse CLI at ~/.local/bin/treehouse and Git repositories with a usable base branch.
---

# Use Treehouse for Pi repository work

Treehouse is the default owner of worktree lifecycle for Pi.

## Required behavior

- New interactive Pi invocations in a Git repository are normally relocated by `~/.local/bin/pi` into a durable Treehouse lease.
- Before changing files, confirm the working directory is a Treehouse-managed path with `treehouse status --json`.
- Never run `git worktree add`, manually clone a repository for isolation, or invent an unmanaged worktree path when Treehouse can manage the repository.
- Do not edit the main checkout merely because it is available. Preserve its dirty state and work in the acquired Treehouse slot.
- If Pi was intentionally started with `PI_TREEHOUSE_BYPASS=1`, respect that explicit exception and state that changes are happening outside Treehouse.
- Session-resume and Pi administrative commands bypass automatic acquisition so their cwd/session lookup remains correct. Do not mistake that bypass for permission to start new implementation in the main checkout.

## Inspect the current slot

Run:

```bash
treehouse status --json
```

A managed current directory must exactly match a returned `path`. Treat `damaged`, `unverified`, recovered/quarantined, or foreign worktrees as unsafe until inspected.

## Acquire explicitly when needed

For delegated or durable work that is not already in a managed slot:

```bash
treehouse get --lease --lease-holder "pi:<purpose>" --json
```

Use the returned `path`, `lease_id`, and `lease_holder` verbatim. Keep the lease while a Pi session or related process may resume there.

Use `--no-fetch` only after independently fetching and verifying the required refs. Use `--base <branch>` only when the task names a non-default base and that branch exists. Do not guess.

## Return safely

Stop all worktree processes first. Return the exact acquisition with identity guards:

```bash
treehouse return \
  --if-lease-id "$lease_id" \
  --if-lease-holder "$lease_holder" \
  "$path"
```

Never add `--force` automatically. If Treehouse reports dirty or unlanded work, preserve the lease and report the path. Do not destroy, prune, reset, or clean it merely to free a slot.

## Recovery and cleanup

- Use `treehouse enter <name>` to inspect an existing slot without changing its ownership.
- `treehouse prune` and `treehouse destroy` are dry runs by default; review their evidence before any `--yes` operation.
- Never use broad destructive include flags without explicit user approval.
- Recovered, leased, in-use, dirty, unmerged, and damaged slots are evidence to preserve, not cleanup targets.
