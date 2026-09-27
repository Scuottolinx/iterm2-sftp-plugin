#!/usr/bin/env python3
"""iTerm2 AutoLaunch entry point for SFTP Browser.

iTerm2 starts this file with its API cookie in the environment; we simply hand
over to the plugin's own virtualenv (created by install.sh) so dependencies
(aiohttp, psutil) never touch iTerm2's Python runtime.
"""
import os
import sys

HOME = os.path.expanduser("~/Library/Application Support/iTerm2/SFTPBrowser")
PY = os.path.join(HOME, "venv", "bin", "python")
MAIN = os.path.join(HOME, "app", "main.py")

if not os.path.exists(PY):
    sys.exit("SFTP Browser non installato: esegui install.sh")
os.execv(PY, [PY, MAIN])
