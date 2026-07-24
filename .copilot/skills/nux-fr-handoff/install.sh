#!/bin/bash
set -euo pipefail

DRY_RUN=false
if [ "${1:-}" = "--dry-run" ]; then
  DRY_RUN=true
elif [ "$#" -gt 0 ]; then
  echo "Usage: $0 [--dry-run]" >&2
  exit 2
fi

SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_TARGET="$HOME/.copilot/skills/nux-fr-handoff"
BIN_TARGET="$HOME/.local/bin/nux-fr-handoff"
LABEL="com.${USER}.nux-fr-handoff"
PLIST_TARGET="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_PATH="$HOME/Library/Logs/nux-fr-handoff.log"

run() {
  if $DRY_RUN; then
    printf '+'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

for command in copilot gh python3; do
  if ! command -v "$command" >/dev/null 2>&1; then
    echo "Missing required command: $command" >&2
    exit 1
  fi
done

if ! python3 -c 'import yaml' >/dev/null 2>&1; then
  echo "PyYAML is required. Install it with: python3 -m pip install --user PyYAML" >&2
  exit 1
fi

if ! command -v terminal-notifier >/dev/null 2>&1; then
  echo "terminal-notifier is required for the clickable review notification." >&2
  echo "Install it with: brew install terminal-notifier" >&2
  exit 1
fi

run mkdir -p \
  "$HOME/.copilot/skills" \
  "$HOME/.local/bin" \
  "$HOME/Library/LaunchAgents" \
  "$HOME/Library/Logs"
run ln -sfn "$SOURCE_DIR" "$SKILL_TARGET"

if $DRY_RUN; then
  echo "+ write $BIN_TARGET"
  echo "+ write $PLIST_TARGET"
else
  rm -f "$BIN_TARGET"
  cat >"$BIN_TARGET" <<'EOF'
#!/bin/bash
set -euo pipefail
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
exec /usr/bin/env python3 \
  "$HOME/.copilot/skills/nux-fr-handoff/nux_fr_handoff.py" \
  "$@"
EOF
  chmod +x "$BIN_TARGET"

  rm -f "$PLIST_TARGET"
  cat >"$PLIST_TARGET" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-lc</string>
        <string>exec "\$HOME/.local/bin/nux-fr-handoff"</string>
    </array>
    <key>RunAtLoad</key>
    <false/>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key><integer>5</integer>
        <key>Hour</key><integer>12</integer>
        <key>Minute</key><integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>${LOG_PATH}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_PATH}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>${HOME}</string>
    </dict>
</dict>
</plist>
EOF

  plutil -lint "$PLIST_TARGET"
  launchctl unload "$PLIST_TARGET" >/dev/null 2>&1 || true
  launchctl load "$PLIST_TARGET"
fi

echo "Installed nux-fr-handoff."
echo "Preview with: nux-fr-handoff --dry-run --verbose"
