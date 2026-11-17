#!/bin/zsh
# Install (or restart) the always-on Feishu listener as a macOS launch agent.
# Generates the plist for this checkout, so nothing machine-specific is committed.
# Uninstall: launchctl bootout gui/$UID/com.agent-matrix.listen && rm ~/Library/LaunchAgents/com.agent-matrix.listen.plist
set -e
label="com.agent-matrix.listen"
root="$(cd "$(dirname "$0")/.." && pwd)"
lark="$(command -v lark-cli)" || { echo "lark-cli not found on PATH"; exit 1; }
node_dir="$(dirname "$(command -v node)")"
envpath="$(dirname "$lark"):$node_dir:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
mkdir -p "$root/logs" "$HOME/Library/LaunchAgents"
plist="$HOME/Library/LaunchAgents/$label.plist"
cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array><string>$root/.venv/bin/python</string><string>-u</string><string>-m</string><string>agent_matrix.listen</string></array>
  <key>WorkingDirectory</key><string>$root</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$envpath</string><key>LARKSUITE_CLI_NO_UPDATE_NOTIFIER</key><string>1</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$root/logs/listen.log</string>
  <key>StandardErrorPath</key><string>$root/logs/listen.log</string>
</dict>
</plist>
PLIST
plutil -lint "$plist" >/dev/null
if launchctl print "gui/$UID/$label" >/dev/null 2>&1; then
  launchctl bootout "gui/$UID/$label" || true
  for i in {1..20}; do launchctl print "gui/$UID/$label" >/dev/null 2>&1 || break; sleep 1; done
fi
launchctl bootstrap "gui/$UID" "$plist"
echo "installed $label → $root"
