#!/bin/zsh
# Build the menu bar monitor, install it to ~/Applications and start it at login.
set -e
cd "$(dirname "$0")"
root="$(cd .. && pwd)"
app="$HOME/Applications/心响.app"
app_port="$("$root/.venv/bin/python" -c 'import sys; from pathlib import Path; from agent_matrix.config import load; p=Path(sys.argv[1]); print(load(p).app_port if p.exists() else 8765)' "$root/config.yaml")"
build="$(mktemp -d)/心响.app"
mkdir -p "$build/Contents/MacOS" "$build/Contents/Resources"
cp AppIcon.icns "$build/Contents/Resources/AppIcon.icns"  # redraw with: python make_icon.py
swiftc -O -parse-as-library -target arm64-apple-macos14 AgentMatrixMonitor.swift ClientLinks.swift -o "$build/Contents/MacOS/AgentMatrixMonitor"
cat > "$build/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>com.agent-matrix.monitor</string>
  <key>CFBundleName</key><string>心响</string>
  <key>CFBundleDisplayName</key><string>心响</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/></dict>
  <key>CFBundleExecutable</key><string>AgentMatrixMonitor</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>AgentMatrixRoot</key><string>$root</string>
  <key>AgentMatrixAppURL</key><string>http://127.0.0.1:$app_port/</string>
</dict></plist>
PLIST
codesign --force --sign - "$build" >/dev/null
pkill -x AgentMatrixMonitor 2>/dev/null || true
mkdir -p "$HOME/Applications"
rm -rf "$app" "$HOME/Applications/事项中心监控.app" && mv "$build" "$app"
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
touch "$app"  # let Finder and the Dock pick up the icon
open -a "$app" || { sleep 2; open -a "$app"; }
echo "installed $app"
