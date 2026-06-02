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

# Install gh pr ready guard as a PATH shim at ~/.local/bin/gh.
# Guards `gh pr ready` against accidental/unconfirmed runs.
if [ -x "$DOTFILES_DIR/bin/gh-pr-ready-guard" ]; then
  mkdir -p "$HOME/.local/bin"
  GH_TARGET="$HOME/.local/bin/gh"
  if [ -L "$GH_TARGET" ] || [ ! -e "$GH_TARGET" ]; then
    ln -sfn "$DOTFILES_DIR/bin/gh-pr-ready-guard" "$GH_TARGET"
    echo "✓ Linked gh wrapper → ~/.local/bin/gh (intercepts 'gh pr ready')"
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

# Install notification-triage launchd agent (macOS only).
# The wrapper itself goes in ~/.local/bin so it stays on PATH for ad-hoc runs,
# and the plist gets symlinked into ~/Library/LaunchAgents so launchctl can
# pick it up on a cron-like schedule (every 2h, 08:00-18:00, Mon-Fri).
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

echo "Dotfiles install complete."
