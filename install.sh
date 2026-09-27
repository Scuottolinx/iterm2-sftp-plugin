#!/bin/bash
# Installs SFTP Browser for iTerm2 (per-user, no sudo).
set -euo pipefail
cd "$(dirname "$0")"

DEST="$HOME/Library/Application Support/iTerm2/SFTPBrowser"
AUTOLAUNCH="$HOME/Library/Application Support/iTerm2/Scripts/AutoLaunch"
PYTHON="${PYTHON:-python3}"

if ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
  echo "Serve Python >= 3.9 (python3 di Xcode CLT o Homebrew)." >&2; exit 1
fi

echo "→ Ambiente Python in $DEST/venv"
mkdir -p "$DEST"
[ -x "$DEST/venv/bin/python" ] || "$PYTHON" -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install -q --upgrade pip
"$DEST/venv/bin/pip" install -q -r requirements.txt

echo "→ Copio il plugin"
rm -rf "$DEST/app"
cp -R app "$DEST/app"
find "$DEST/app" -name __pycache__ -prune -exec rm -rf {} +

echo "→ Comando iterm-attach"
mkdir -p "$DEST/bin"
cp bin/iterm-attach "$DEST/bin/"
chmod +x "$DEST/bin/iterm-attach"
BIN=""
for d in /opt/homebrew/bin /usr/local/bin "$HOME/.local/bin"; do
  case ":$PATH:" in *":$d:"*) [ -w "$d" ] && { BIN="$d"; break; } ;; esac
done
if [ -n "$BIN" ]; then
  ln -sf "$DEST/bin/iterm-attach" "$BIN/iterm-attach"
  echo "   in $BIN"
else
  echo "   Aggiungi al PATH: $DEST/bin"
fi

echo "→ Launcher in AutoLaunch"
mkdir -p "$AUTOLAUNCH"
cp launcher/sftp_browser.py "$AUTOLAUNCH/sftp_browser.py"

cat <<MSG

Installato. Ultimi passi:
  1. iTerm2 ▸ Settings ▸ General ▸ Magic ▸ attiva "Enable Python API"
  2. Riavvia iTerm2 (oppure Scripts ▸ AutoLaunch ▸ sftp_browser.py)
  3. View ▸ Show Toolbelt, poi Toolbelt ▸ SFTP
MSG

if ! grep -qiE "^\s*ControlMaster\s+(auto|autoask|yes)" "$HOME/.ssh/config" 2>/dev/null; then
cat <<MSG

Per riusare la connessione SSH senza autenticarti di nuovo aggiungi a ~/.ssh/config:

  Host *
      ControlMaster auto
      ControlPath ~/.ssh/cm-%C
      ControlPersist 10m

MSG
fi
