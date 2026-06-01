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

echo "Dotfiles install complete."
