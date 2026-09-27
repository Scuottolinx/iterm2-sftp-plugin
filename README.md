# SFTP Browser for iTerm2

🇮🇹 [Italiano](README.it.md)

A **MobaXterm-style SFTP panel** inside the iTerm2 Toolbelt. It reuses the **SSH
connection of the active session**, so you never type a password or a 2FA code again.

- File list with sortable columns (name, size, date, owner, group, permissions) and a folder tree
- Drag & drop upload from Finder (folders too), recursive download
- **Edit & sync**: double-click a file, edit it locally, every save is uploaded back
- Rename, delete, new file/folder, chmod, copy path, `cd` the terminal to a folder
- **Follow the terminal folder**: the panel goes where you `cd`, no server-side install needed
- **Press R to reconnect** when an SSH connection drops (like MobaXterm), in every tab
- Transfer queue with progress and cancel, light/dark theme
- `iterm-attach`: open an iTerm2 tab on an SSH connection that was opened elsewhere (e.g. Terminal.app)

> The user interface is currently in Italian.

## Requirements

- macOS with **iTerm2 3.5+**
- **Python 3.9+** (the one from Xcode Command Line Tools is fine, Homebrew too)
- OpenSSH (the system `ssh`)

## Install

```bash
git clone https://github.com/Scuottolinx/iterm2-sftp-plugin.git
cd iterm2-sftp-plugin
./install.sh
```

The installer is per-user (no `sudo`): it creates a private virtualenv in
`~/Library/Application Support/iTerm2/SFTPBrowser`, adds a launcher to iTerm2's
`Scripts/AutoLaunch` and links `iterm-attach` into your `PATH`.

Then:

1. **iTerm2 ▸ Settings ▸ General ▸ Magic ▸ Enable Python API**
2. **Restart iTerm2** (or run *Scripts ▸ AutoLaunch ▸ sftp_browser.py*)
3. **View ▸ Show Toolbelt** (⌘⇧B), then tick **View ▸ Toolbelt ▸ SFTP**
4. Enable OpenSSH connection sharing in `~/.ssh/config` and reconnect your sessions:

   ```
   Host *
       ControlMaster auto
       ControlPath ~/.ssh/cm-%C
       ControlPersist 10m
   ```

   You can exclude hosts where you don't want it, e.g. `Host * !github.com`.

That's it: open `ssh myserver` in iTerm2 and the panel shows the remote files.

### Update / uninstall

```bash
git pull && ./install.sh      # update, then restart the script from iTerm2 ▸ Scripts
./uninstall.sh                # remove everything
```

## How it works

```
iTerm2 ──(Python API)──► sftp_browser (AutoLaunch script)
  │  active session, tty, "path" variable             │
  │                                                   ├─ finds the `ssh` process on the tty
  │                                                   ├─ `ssh -G` → host, port, ControlPath
  │                                                   ├─ `ssh -O check` → is there a master?
  │                                                   └─ `ssh -o ControlMaster=no -s host sftp`
  │                                                        └─ async SFTP v3 client (sftp.py)
  └─ Toolbelt ▸ SFTP (web view) ◄──HTTP/WebSocket── local server 127.0.0.1:47823 (token)
```

A running `ssh` client doesn't expose its connection to other processes. The clean way
to reuse it is **OpenSSH multiplexing** (ControlMaster): the panel opens a new `sftp`
channel on the master's socket, which is already authenticated.

Without ControlMaster the panel says so and offers to open a **separate** connection,
which only works with keys or an agent (it cannot ask for a password).

**Follow the terminal folder** uses iTerm2
[Shell Integration](https://iterm2.com/documentation-shell-integration.html) when the
server has it. Otherwise, after screen updates (at most once per second), it runs a tiny
read-only `sh` script over the shared connection (`ps`, `readlink /proc/<pid>/cwd`) to read
the current directory of your shell. It works on Linux (even very old ones) and BSD/macOS with `lsof`.

## Press R to reconnect

When the `ssh` of a tab exits because the connection dropped (broken pipe, "closed by
remote host", timeout, connection refused…), the terminal shows:

```
 ✖ Connessione a server persa  Premi R per riconnetterti (un altro tasto per ignorare)
```

Press **R** to run the same `ssh` command again; any other key goes to the shell as usual.
Nothing happens after a normal `exit`/`logout`. It works in every tab, even with the
Toolbelt closed, and can be turned off in the panel settings (⚙︎).

## `iterm-attach`: use in iTerm2 a connection opened elsewhere

If another program opened an SSH session with the regular `ssh` (for example in
Terminal.app) and ControlMaster is on, you can open an iTerm2 tab on the same connection,
without authenticating again:

```bash
iterm-attach          # choose (Enter = first connection not yet open in iTerm2)
iterm-attach web01    # the connection whose name contains "web01"
iterm-attach -a       # open all connections not yet in iTerm2
iterm-attach -l       # list: where each one is open (Terminal / iTerm2)
```

The new tab is a separate shell on the same connection, not the same shell.

## SSH proxies that record sessions (PAM)

Some privileged-access proxies **drop the whole connection if a second channel is opened**
on it. The panel and `iterm-attach` detect them (one-time UUID usernames, or the hosts you
list in the settings) and never open anything on those connections. Tip: exclude such
proxies from ControlMaster (`Host * !pam-proxy.example.com`).

## Settings (⚙︎ in the panel)

- download folder (default `~/Downloads`) or "always ask"
- editor command: `open -t`, `open -a "Visual Studio Code"`, `open -a BBEdit`…
- double-click action: edit & sync / open with the default app / download
- "press R to reconnect" on/off, PAM proxy hosts never to touch

Keyboard: ↑↓ Enter · ⌫ parent folder · ⌘⌫ delete · F2 rename · F5 refresh · ⌘A select all ·
⌘[ ⌘] back/forward · type to jump to a name.

## Troubleshooting

- **SFTP doesn't appear in the Toolbelt** → View ▸ Toolbelt ▸ tick SFTP; check that the Python API is enabled.
- **"nessun ControlMaster"** → add the `~/.ssh/config` block above and open the SSH session again.
- Logs: `~/Library/Application Support/iTerm2/SFTPBrowser/plugin.log`,
  or iTerm2 ▸ Scripts ▸ Manage ▸ Console.

## Known limitations

- Only connections opened **after** enabling ControlMaster can be reused.
- Files are accessed as the SSH login user: after `sudo su` in the terminal, SFTP is still the login user.
- `ssh` running inside tmux/screen is not detected.
- ControlPath must be shorter than ~104 characters (Unix socket limit): `~/.ssh/cm-%C` is fine.
- Edit & sync overwrites the remote file on every save (like MobaXterm).

## Security

The local server listens on `127.0.0.1` only, requires a random token (stored in
`~/Library/Application Support/iTerm2/SFTPBrowser/token`, mode 600) and rejects any `Host`
other than `127.0.0.1`/`localhost` (DNS-rebinding protection), so web pages in your browser
cannot drive it.

## Development

```bash
python3 tests/test_sftp.py          # SFTP client against /usr/libexec/sftp-server
python3 tests/test_reconnect.py     # dropped connection vs normal logout (needs psutil)
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python app/main.py --demo # UI without iTerm2, browsing this Mac
```

## License

[MIT](LICENSE) © 2026 Antonio Scuotto.

Dependencies are installed from PyPI and are not bundled: iterm2 (GPLv2), aiohttp
(Apache-2.0), psutil (BSD-3-Clause).
