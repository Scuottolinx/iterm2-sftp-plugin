#!/bin/bash
set -euo pipefail
rm -f "$HOME/Library/Application Support/iTerm2/Scripts/AutoLaunch/sftp_browser.py"
for d in /opt/homebrew/bin /usr/local/bin "$HOME/.local/bin"; do
  [ -L "$d/iterm-attach" ] && rm -f "$d/iterm-attach"
done
rm -rf "$HOME/Library/Application Support/iTerm2/SFTPBrowser" "$HOME/Library/Caches/SFTPBrowser"
echo "SFTP Browser rimosso. Riavvia iTerm2."
