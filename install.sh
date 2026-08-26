#!/bin/bash
# Dotfiles install script - runs automatically in GitHub Codespaces
# and can be run manually on any machine.

set -e

DOTFILES_DIR="$(cd "$(dirname "$0")" && pwd)"

# Symlink copilot instructions for Copilot CLI
if [ -f "$DOTFILES_DIR/.github/copilot-instructions.md" ]; then
  mkdir -p "$HOME/.copilot"
  ln -sf "$DOTFILES_DIR/.github/copilot-instructions.md" "$HOME/.copilot/copilot-instructions.md"
  echo "✓ Linked copilot-instructions.md → ~/.copilot/copilot-instructions.md"
fi

# Symlink user-scope Copilot CLI skills so they auto-load on every session.
if [ -d "$DOTFILES_DIR/.copilot/skills" ]; then
  mkdir -p "$HOME/.copilot/skills"
  for skill_path in "$DOTFILES_DIR/.copilot/skills/"*/; do
    [ -d "$skill_path" ] || continue
    skill_name="$(basename "$skill_path")"
    target="$HOME/.copilot/skills/$skill_name"
    if [ -L "$target" ] || [ ! -e "$target" ]; then
      ln -sfn "$skill_path" "$target"
      echo "✓ Linked skill $skill_name → $target"
    else
      echo "⚠ $target exists and is not a symlink - skipping"
    fi
  done
fi

# Install private catalog tools only when the source is supplied outside this
# public repository.
CATALOG_REPO="${COPILOT_SKILL_CATALOG_REPO:-}"
CATALOG_SKILLS="validate-pr-with-codespace session-portability cleanup-worktrees remediate-accessibility-audit"
if [ -z "$CATALOG_REPO" ]; then
  echo "⚠ COPILOT_SKILL_CATALOG_REPO is not set - skipping private catalog tools"
else
  if command -v gh >/dev/null 2>&1; then
    for skill_name in $CATALOG_SKILLS; do
      skill_target="$HOME/.copilot/skills/$skill_name/SKILL.md"
      if [ -f "$skill_target" ]; then
        echo "✓ Copilot skill $skill_name is already installed"
      elif gh skill install "$CATALOG_REPO" "skills/$skill_name" --agent github-copilot --scope user </dev/null; then
        echo "✓ Installed Copilot skill $skill_name"
      else
        echo "⚠ Failed to install Copilot skill $skill_name - continuing dotfiles setup"
      fi
    done
  else
    echo "⚠ gh is missing - skipping private catalog skills"
  fi

  if command -v copilot >/dev/null 2>&1; then
    if copilot plugin list 2>/dev/null | grep -Eq '(^|[[:space:]])gho11y([[:space:](]|$)'; then
      echo "✓ Copilot plugin gho11y is already installed"
    elif copilot plugin install "$CATALOG_REPO:plugins/gho11y" </dev/null; then
      echo "✓ Installed Copilot plugin gho11y"
    else
      echo "⚠ Failed to install Copilot plugin gho11y - continuing dotfiles setup"
    fi
  else
    echo "⚠ copilot is missing - skipping Copilot plugin gho11y"
  fi
fi

# Install gh guard wrapper as a PATH shim at ~/.local/bin/gh.
# Guards both `gh pr ready` and `gh pr create` against accidental/unreviewed runs.
if [ -x "$DOTFILES_DIR/bin/gh-guard" ]; then
  mkdir -p "$HOME/.local/bin"
  GH_TARGET="$HOME/.local/bin/gh"
  if [ -L "$GH_TARGET" ] || [ ! -e "$GH_TARGET" ]; then
    ln -sfn "$DOTFILES_DIR/bin/gh-guard" "$GH_TARGET"
    echo "✓ Linked gh wrapper → ~/.local/bin/gh (intercepts 'gh pr ready' and 'gh pr create')"
  else
    echo "⚠ $GH_TARGET exists and is not a symlink - skipping gh wrapper install"
    echo "  Move or remove the existing file and re-run install.sh to enable the guard."
  fi

  # Ensure ~/.local/bin is on PATH ahead of /opt/homebrew/bin so the wrapper wins
  # over the real gh binary. Cover both zsh (macOS default) and bash (Codespaces,
  # Linux). brew shellenv (in .zprofile) prepends /opt/homebrew/bin, so we add
  # our own prepend AFTER brew runs.
  PATH_LINE='export PATH="$HOME/.local/bin:$PATH"  # dotfiles: gh wrapper'
  for shell_rc in "$HOME/.zprofile" "$HOME/.profile" "$HOME/.bashrc"; do
    rc_short="${shell_rc/#$HOME/~}"
    if [ -f "$shell_rc" ] && grep -q "dotfiles: gh wrapper" "$shell_rc"; then
      echo "✓ PATH for gh wrapper already in $rc_short"
    else
      printf '\n%s\n' "$PATH_LINE" >> "$shell_rc"
      echo "✓ Appended PATH for gh wrapper to $rc_short (open a new shell to apply)"
    fi
  done
fi

# Add the local-only review environment shortcut on macOS.
if [ "$(uname)" = "Darwin" ]; then
  DEPLOY_ALIAS="alias deploy='gh review-lab deploy'  # dotfiles: review lab"
  for shell_rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.bash_profile"; do
    rc_short="${shell_rc/#$HOME/~}"
    if [ -f "$shell_rc" ] && grep -q "dotfiles: review lab" "$shell_rc"; then
      echo "✓ Review lab alias already in $rc_short"
    elif [ -f "$shell_rc" ] && grep -Eq '^[[:space:]]*(alias[[:space:]]+((['\''"]deploy['\''"][[:space:]]*=)|(['\''"]?deploy=))|deploy[[:space:]]*\(\)|function[[:space:]]+deploy([[:space:]]|\(|$))' "$shell_rc"; then
      echo "⚠ $rc_short already defines deploy - skipping review lab alias"
    else
      printf '\n%s\n' "$DEPLOY_ALIAS" >> "$shell_rc"
      echo "✓ Added review lab alias to $rc_short"
    fi
  done
fi

# Install pr-marker helper as a PATH shim at ~/.local/bin/pr-marker.
# Writes the per-branch plan/code/demo/PR-description/tests markers that the
# gh-guard `gh pr create` gate checks, keeping the path encoding in one place.
if [ -x "$DOTFILES_DIR/bin/pr-marker" ]; then
  mkdir -p "$HOME/.local/bin"
  PR_MARKER_TARGET="$HOME/.local/bin/pr-marker"
  if [ -L "$PR_MARKER_TARGET" ] || [ ! -e "$PR_MARKER_TARGET" ]; then
    ln -sfn "$DOTFILES_DIR/bin/pr-marker" "$PR_MARKER_TARGET"
    echo "✓ Linked pr-marker → ~/.local/bin/pr-marker"
  else
    echo "⚠ $PR_MARKER_TARGET exists and is not a symlink - skipping"
  fi
fi

# Install and reload the babysit-prs launchd agent so plist argument changes
# take effect immediately.
BABYSIT_PLIST="$DOTFILES_DIR/LaunchAgents/com.zkoppert.babysit-prs.plist"
BABYSIT_SCRIPT="$HOME/repos/babysit-prs/babysit_prs.py"
EXPECTED_DOTFILES_DIR="$HOME/repos/dotfiles"
if [ -f "$BABYSIT_PLIST" ] && [ "$(uname)" = "Darwin" ]; then
  if [ "$DOTFILES_DIR" != "$EXPECTED_DOTFILES_DIR" ] || [ ! -f "$BABYSIT_SCRIPT" ]; then
    echo "⚠ Skipping babysit-prs launchd agent: expected dotfiles at $EXPECTED_DOTFILES_DIR and companion script at $BABYSIT_SCRIPT"
  elif [ ! -x "$DOTFILES_DIR/bin/babysit-prs" ]; then
    echo "⚠ Skipping babysit-prs launchd agent: $DOTFILES_DIR/bin/babysit-prs is not executable"
  else
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
    BABYSIT_PLIST_TARGET="$HOME/Library/LaunchAgents/com.zkoppert.babysit-prs.plist"
    if [ -L "$BABYSIT_PLIST_TARGET" ] || [ ! -e "$BABYSIT_PLIST_TARGET" ]; then
      launchctl unload "$BABYSIT_PLIST_TARGET" >/dev/null 2>&1 || true
      ln -sfn "$BABYSIT_PLIST" "$BABYSIT_PLIST_TARGET"
      if launchctl load "$BABYSIT_PLIST_TARGET" 2>/dev/null; then
        echo "✓ Loaded launchd agent com.zkoppert.babysit-prs"
      else
        echo "⚠ launchctl load failed for $BABYSIT_PLIST_TARGET - check ~/Library/Logs/babysit-prs.log"
      fi
    else
      echo "⚠ $BABYSIT_PLIST_TARGET exists and is not a symlink - skipping"
    fi
  fi
fi

# Install notification-triage launchd agent (macOS only).
# The wrapper itself goes in ~/.local/bin so it stays on PATH for ad-hoc runs,
# and the plist gets symlinked into ~/Library/LaunchAgents so launchctl can
# pick it up on a cron-like schedule (every 2h, 08:00-18:00, Mon-Fri).
if [ "$(uname)" = "Darwin" ] && ! command -v terminal-notifier >/dev/null 2>&1; then
  echo "⚠ terminal-notifier is missing - run 'brew install terminal-notifier' to enable clickable triage alerts"
fi

TRIAGE_WRAPPER="$DOTFILES_DIR/bin/notification-triage"
TRIAGE_PLIST="$DOTFILES_DIR/LaunchAgents/com.zkoppert.notification-triage.plist"
if [ -x "$TRIAGE_WRAPPER" ] && [ "$(uname)" = "Darwin" ]; then
  mkdir -p "$HOME/.local/bin"
  TRIAGE_BIN_TARGET="$HOME/.local/bin/notification-triage"
  if [ -L "$TRIAGE_BIN_TARGET" ] || [ ! -e "$TRIAGE_BIN_TARGET" ]; then
    ln -sfn "$TRIAGE_WRAPPER" "$TRIAGE_BIN_TARGET"
    echo "✓ Linked notification-triage → ~/.local/bin/notification-triage"
  else
    echo "⚠ $TRIAGE_BIN_TARGET exists and is not a symlink - skipping"
  fi

  if [ -f "$TRIAGE_PLIST" ]; then
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
    PLIST_TARGET="$HOME/Library/LaunchAgents/com.zkoppert.notification-triage.plist"
    if [ -L "$PLIST_TARGET" ] || [ ! -e "$PLIST_TARGET" ]; then
      # launchctl bootstrap (modern) or load -w (legacy) both work fine here;
      # unload first so re-runs are consistent (no error if it isn't loaded).
      launchctl unload "$PLIST_TARGET" >/dev/null 2>&1 || true
      ln -sfn "$TRIAGE_PLIST" "$PLIST_TARGET"
      if launchctl load "$PLIST_TARGET" 2>/dev/null; then
        echo "✓ Loaded launchd agent com.zkoppert.notification-triage"
      else
        echo "⚠ launchctl load failed for $PLIST_TARGET - check 'launchctl error' and ~/Library/Logs/notification-triage.log"
      fi
    else
      echo "⚠ $PLIST_TARGET exists and is not a symlink - skipping (delete it manually if you want the dotfiles version)"
    fi
  fi
fi

# Install the hourly accessibility issue picker. Its private repository and label
# configuration stays in a user-owned file outside this public repository.
ACCESSIBILITY_WRAPPER="$DOTFILES_DIR/bin/accessibility-issue-picker"
ACCESSIBILITY_RESUME_WRAPPER="$DOTFILES_DIR/bin/resume-accessibility-session"
ACCESSIBILITY_PLIST="$DOTFILES_DIR/LaunchAgents/com.zkoppert.accessibility-issue-picker.plist"
ACCESSIBILITY_CONFIG="$HOME/.config/accessibility-issue-picker.env"
if [ -x "$ACCESSIBILITY_WRAPPER" ] &&
  [ -x "$ACCESSIBILITY_RESUME_WRAPPER" ] &&
  [ -f "$ACCESSIBILITY_PLIST" ] &&
  [ "$(uname)" = "Darwin" ] &&
  [ "$DOTFILES_DIR" = "$HOME/repos/dotfiles" ]; then
  mkdir -p "$HOME/.local/bin" "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
  for accessibility_command in accessibility-issue-picker resume-accessibility-session; do
    source_path="$DOTFILES_DIR/bin/$accessibility_command"
    target_path="$HOME/.local/bin/$accessibility_command"
    if [ -L "$target_path" ] || [ ! -e "$target_path" ]; then
      ln -sfn "$source_path" "$target_path"
      echo "✓ Linked $accessibility_command → $target_path"
    else
      echo "⚠ $target_path exists and is not a symlink - skipping"
    fi
  done

  ACCESSIBILITY_PLIST_TARGET="$HOME/Library/LaunchAgents/com.zkoppert.accessibility-issue-picker.plist"
  ACCESSIBILITY_CONFIG_ERROR=""
  ACCESSIBILITY_VALIDATION_ERROR=""
  if [ ! -f "$ACCESSIBILITY_CONFIG" ]; then
    ACCESSIBILITY_CONFIG_ERROR="create $ACCESSIBILITY_CONFIG first"
  elif ! ACCESSIBILITY_VALIDATION_ERROR="$(
    ACCESSIBILITY_PICKER_CONFIG="$ACCESSIBILITY_CONFIG" \
      "$ACCESSIBILITY_WRAPPER" --validate-schedule 2>&1
  )"; then
    while IFS= read -r validation_line; do
      case "$validation_line" in
        "accessibility-issue-picker: missing config file:"*)
          ACCESSIBILITY_CONFIG_ERROR="accessibility picker config file is missing"
          ;;
        "accessibility-issue-picker: config file must have mode 0600:"*)
          ACCESSIBILITY_CONFIG_ERROR="accessibility picker config file must have mode 0600"
          ;;
        "accessibility-issue-picker: failed to load config file:"*)
          ACCESSIBILITY_CONFIG_ERROR="accessibility picker config file failed to load"
          ;;
        "accessibility-issue-picker: Missing accessibility picker configuration:"*)
          ACCESSIBILITY_CONFIG_ERROR="accessibility picker configuration is incomplete"
          ;;
        "accessibility-issue-picker: Repository configuration must use OWNER/REPOSITORY")
          ACCESSIBILITY_CONFIG_ERROR="accessibility picker repositories must use OWNER/REPOSITORY"
          ;;
        "accessibility-issue-picker: Remediation workdir does not exist:"*)
          ACCESSIBILITY_CONFIG_ERROR="accessibility remediation workdir does not exist"
          ;;
        "accessibility-issue-picker: Remediation workdir contains no Git checkout:"*)
          ACCESSIBILITY_CONFIG_ERROR="accessibility remediation workdir contains no Git checkout"
          ;;
        "accessibility-issue-picker: Cannot inspect remediation workdir "*)
          ACCESSIBILITY_CONFIG_ERROR="accessibility remediation workdir cannot be inspected"
          ;;
      esac
      [ -n "$ACCESSIBILITY_CONFIG_ERROR" ] && break
    done <<< "$ACCESSIBILITY_VALIDATION_ERROR"
    ACCESSIBILITY_CONFIG_ERROR="${ACCESSIBILITY_CONFIG_ERROR:-accessibility picker schedule validation failed}"
  fi
  if [ -n "$ACCESSIBILITY_CONFIG_ERROR" ]; then
    if [ -L "$ACCESSIBILITY_PLIST_TARGET" ]; then
      launchctl unload "$ACCESSIBILITY_PLIST_TARGET" >/dev/null 2>&1 || true
      rm "$ACCESSIBILITY_PLIST_TARGET"
      echo "✓ Unloaded accessibility issue picker because its configuration is unavailable"
    fi
    echo "⚠ Skipping accessibility issue picker launchd agent: $ACCESSIBILITY_CONFIG_ERROR"
  else
    if [ -L "$ACCESSIBILITY_PLIST_TARGET" ] || [ ! -e "$ACCESSIBILITY_PLIST_TARGET" ]; then
      launchctl unload "$ACCESSIBILITY_PLIST_TARGET" >/dev/null 2>&1 || true
      ln -sfn "$ACCESSIBILITY_PLIST" "$ACCESSIBILITY_PLIST_TARGET"
      if launchctl load "$ACCESSIBILITY_PLIST_TARGET" 2>/dev/null; then
        echo "✓ Loaded launchd agent com.zkoppert.accessibility-issue-picker"
      else
        echo "⚠ launchctl load failed for $ACCESSIBILITY_PLIST_TARGET - check ~/Library/Logs/accessibility-issue-picker.log"
      fi
    else
      echo "⚠ $ACCESSIBILITY_PLIST_TARGET exists and is not a symlink - skipping"
    fi
  fi
elif [ -f "$ACCESSIBILITY_PLIST" ] &&
  [ "$(uname)" = "Darwin" ] &&
  [ "$DOTFILES_DIR" != "$HOME/repos/dotfiles" ]; then
  echo "⚠ Skipping accessibility issue picker launchd agent from nonstandard checkout $DOTFILES_DIR"
fi

# Install the Friday NUX first-responder handoff generator.
NUX_HANDOFF_WRAPPER="$DOTFILES_DIR/bin/nux-fr-handoff"
NUX_HANDOFF_INSTALLER="$DOTFILES_DIR/.copilot/skills/nux-fr-handoff/install.sh"
if [ -x "$NUX_HANDOFF_WRAPPER" ]; then
  mkdir -p "$HOME/.local/bin"
  NUX_HANDOFF_BIN_TARGET="$HOME/.local/bin/nux-fr-handoff"
  if [ -L "$NUX_HANDOFF_BIN_TARGET" ] || [ ! -e "$NUX_HANDOFF_BIN_TARGET" ]; then
    ln -sfn "$NUX_HANDOFF_WRAPPER" "$NUX_HANDOFF_BIN_TARGET"
    echo "✓ Linked nux-fr-handoff → ~/.local/bin/nux-fr-handoff"
  else
    echo "⚠ $NUX_HANDOFF_BIN_TARGET exists and is not a symlink - skipping"
  fi
fi

if [ -x "$NUX_HANDOFF_INSTALLER" ] && [ "$(uname)" = "Darwin" ]; then
  if ! "$NUX_HANDOFF_INSTALLER"; then
    echo "⚠ NUX FR handoff scheduling was skipped; run $NUX_HANDOFF_INSTALLER after installing its prerequisites"
  fi
fi

echo "Dotfiles install complete."
