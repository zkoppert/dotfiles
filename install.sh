#!/bin/bash
# Dotfiles install script — runs automatically in GitHub Codespaces
# and can be run manually on any machine.

set -e

DOTFILES_DIR="$(cd "$(dirname "$0")" && pwd)"

# Symlink copilot instructions for VS Code and CLI
if [ -f "$DOTFILES_DIR/.github/copilot-instructions.md" ]; then
  mkdir -p "$HOME/.github"
  ln -sf "$DOTFILES_DIR/.github/copilot-instructions.md" "$HOME/.github/copilot-instructions.md"
  echo "✓ Linked copilot-instructions.md → ~/.github/copilot-instructions.md"

  mkdir -p "$HOME/.copilot"
  ln -sf "$DOTFILES_DIR/.github/copilot-instructions.md" "$HOME/.copilot/copilot-instructions.md"
  echo "✓ Linked copilot-instructions.md → ~/.copilot/copilot-instructions.md"
fi

echo "Dotfiles install complete."
