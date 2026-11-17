#!/bin/zsh
# Build the menu bar monitor, install it to ~/Applications and start it at login.
set -e
cd "$(dirname "$0")"
root="$(cd .. && pwd)"
app="$HOME/Applications/事项中心监控.app"
build="$(mktemp -d)/事项中心监控.app"
mkdir -p "$build/Contents/MacOS"
swiftc -O -parse-as-library -target arm64-apple-macos14 AgentMatrixMonitor.swift -o "$build/Contents/MacOS/AgentMatrixMonitor"
cat > "$build/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>com.agent-matrix.monitor</string>
  <key>CFBundleName</key><string>事项中心监控</string>
  <key>CFBundleExecutable</key><string>AgentMatrixMonitor</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>AgentMatrixRoot</key><string>$root</string>
</dict></plist>
PLIST
codesign --force --sign - "$build" >/dev/null
pkill -x AgentMatrixMonitor 2>/dev/null || true
mkdir -p "$HOME/Applications"
rm -rf "$app" && mv "$build" "$app"
agent="$HOME/Library/LaunchAgents/com.agent-matrix.monitor.plist"
cat > "$agent" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.agent-matrix.monitor</string>
  <key>ProgramArguments</key><array><string>/usr/bin/open</string><string>-a</string><string>$app</string></array>
  <key>RunAtLoad</key><true/>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
</dict></plist>
PLIST
launchctl bootout "gui/$UID/com.agent-matrix.monitor" 2>/dev/null || true
launchctl bootstrap "gui/$UID" "$agent" 2>/dev/null || true
open -a "$app"
echo "installed $app"
