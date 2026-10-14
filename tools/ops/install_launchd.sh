#!/bin/bash
# Install (or reinstall) the Easytop bot as a macOS LaunchAgent: starts at login, restarts if it dies,
# keeps the Mac awake on AC power while it runs (caffeinate -s), logs to logs/bot.log (content-free).
# Usage: tools/ops/install_launchd.sh [session-file]   (default spike_data/cookies_session2.json)
# Remove:  launchctl bootout gui/$(id -u)/com.easytop.bot && rm ~/Library/LaunchAgents/com.easytop.bot.plist
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
SESSION="${1:-spike_data/cookies_session2.json}"
UV="$(command -v uv)"
LABEL="com.easytop.bot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$REPO/logs" "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string>
    <string>-s</string>
    <string>$UV</string>
    <string>run</string>
    <string>--env-file</string>
    <string>.env</string>
    <string>python</string>
    <string>-m</string>
    <string>easytop.bot</string>
    <string>--session</string>
    <string>$SESSION</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>EASYTOP_LOG_FILE</key><string>$REPO/logs/bot.log</string>
    <key>PATH</key><string>$(dirname "$UV"):/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$REPO/logs/bot.out</string>
  <key>StandardErrorPath</key><string>$REPO/logs/bot.out</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL; log: $REPO/logs/bot.log"
