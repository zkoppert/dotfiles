---
name: playwright
description: Use before any Playwright browser task, including navigation, screenshots, demos, UI validation, or authenticated browser work. Requires cleanup of agent-created tabs while preserving user tabs and the shared browser.
---

# Use Playwright without leaving tabs behind

## Choose the browser connection

- Use the configured shared Playwright MCP service for local browser tasks.
- Use its existing Chrome extension connection instead of launching a separate browser.
- Follow specialized skills when a task requires a Codespace browser or another isolated environment.
- Never copy browser credentials or extension tokens into repositories, logs, or messages.

## Track tab ownership

1. List existing tabs before opening or navigating any page.
2. Preserve those existing tabs, including their URLs and state.
3. Open a new task tab rather than navigating a user tab.
4. Track each tab you create, including popups and task-created connection tabs.
5. Keep ownership separate from tab indexes, which can change after tabs close.
6. Never assume every visible tab belongs to this task or agent.

## Close task tabs before finishing

- Close tabs you created as soon as you no longer need them.
- Run cleanup after success, errors, cancellations, and failed validation when browser tools remain available.
- Use `try`/`finally` for scripted browser work.
- Save required screenshots, recordings, and other evidence before closing task tabs.
- Keep a task tab open only when the user explicitly requests it.
- Refresh the tab list before each index-based close.
- Confirm tab ownership before closing it.
- Preserve user tabs and tabs owned by other agents.
- Close task-created connection tabs last, after browser work finishes, if doing so cannot interrupt another task.
- Never close the shared browser, browser context, or Chrome process to clean up tabs.
- Avoid broad close-all commands and browser shutdown tools on the shared connection.
- If ownership is uncertain, leave the tab open and report the cleanup limitation.
- If cleanup fails, report which task tabs remain instead of claiming cleanup succeeded.

## Verify cleanup

List remaining tabs after cleanup when the connection remains available.
Confirm that task tabs are gone and existing user tabs remain.
Do not reopen a connection solely to verify cleanup if reconnection creates another tab.
