"""SFTP Browser for iTerm2 — a MobaXterm-style file panel in the Toolbelt that
reuses the SSH connection of the active session.

Run by the AutoLaunch launcher, or manually:
    python main.py            # talk to iTerm2
    python main.py --demo     # no iTerm2, browse this Mac through /usr/libexec/sftp-server
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import os
import secrets
import shlex
import shutil
import socket
import sys
import tempfile
import time
import uuid
from typing import Callable, Dict, List, Optional

from aiohttp import WSMsgType, web

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sftp import CHUNK, Cancelled, SFTPClient, SFTPError, join, parent  # noqa: E402
from reconnect import Reconnector  # noqa: E402
import sshresolve  # noqa: E402
from sshresolve import SSHTarget, probe_remote_cwd, resolve_tty  # noqa: E402

APP_DIR = os.path.expanduser(os.environ.get("SFTP_BROWSER_HOME", "~/Library/Application Support/iTerm2/SFTPBrowser"))
CACHE_DIR = os.path.expanduser(os.environ.get("SFTP_BROWSER_CACHE", "~/Library/Caches/SFTPBrowser"))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PREFERRED_PORT = 47823
TOOL_ID = "com.github.iterm2-sftp-browser"

log = logging.getLogger("sftp-browser")

CONTROLMASTER_SNIPPET = """Host *
    ControlMaster auto
    ControlPath ~/.ssh/cm-%C
    ControlPersist 10m
"""

DEFAULTS = {
    "download_dir": "~/Downloads",
    "ask_download_dir": False,
    "editor": "open -t",
    "show_hidden": False,
    "follow": True,
    "dblclick": "edit",  # edit | open | download
    "reconnect": True,   # "press R to reconnect" when an ssh connection drops
    "pam_hosts": "",     # comma-separated PAM proxy hosts never to share (UUID users are detected)
}


# ---------------------------------------------------------------- settings --

class Settings:
    def __init__(self) -> None:
        self.path = os.path.join(APP_DIR, "settings.json")
        self.data = dict(DEFAULTS)
        try:
            with open(self.path) as f:
                self.data.update({k: v for k, v in json.load(f).items() if k in DEFAULTS})
        except (OSError, ValueError):
            pass
        self.apply()

    def __getitem__(self, key: str):
        return self.data[key]

    def update(self, values: dict) -> None:
        for k, v in values.items():
            if k in DEFAULTS and type(v) is type(DEFAULTS[k]):
                self.data[k] = v
        os.makedirs(APP_DIR, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)
        self.apply()

    def apply(self) -> None:
        sshresolve.PAM_HOSTS = {h.strip().lower() for h in self.data["pam_hosts"].split(",") if h.strip()}

    @property
    def download_dir(self) -> str:
        return os.path.expanduser(self.data["download_dir"] or "~/Downloads")


def load_token() -> str:
    os.makedirs(APP_DIR, exist_ok=True)
    path = os.path.join(APP_DIR, "token")
    try:
        with open(path) as f:
            tok = f.read().strip()
            if len(tok) >= 32:
                return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    return tok


# --------------------------------------------------------------- transfers --

class Transfer:
    def __init__(self, kind: str, name: str, remote: str = "", local: str = "") -> None:
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind  # "down" | "up"
        self.name = name
        self.remote = remote
        self.local = local
        self.total = 0
        self.done = 0
        self.state = "queued"
        self.error = ""
        self.started: Optional[float] = None
        self.finished: Optional[float] = None
        self.cancel = False
        self.task: Optional[asyncio.Future] = None

    def progress(self, n: int) -> None:
        self.done += n

    def cancelled(self) -> bool:
        return self.cancel

    def to_json(self) -> dict:
        end = self.finished or time.time()
        speed = self.done / (end - self.started) if self.started and end > self.started else 0
        return {
            "id": self.id, "kind": self.kind, "name": self.name, "remote": self.remote,
            "local": self.local, "total": self.total, "done": self.done, "state": self.state,
            "error": self.error, "speed": speed,
        }


class TransferManager:
    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.items: Dict[str, Transfer] = {}
        self.sem = asyncio.Semaphore(3)
        self.dirty = False
        asyncio.ensure_future(self._ticker())

    def list(self) -> List[dict]:
        return [t.to_json() for t in list(self.items.values())[-200:]]

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(0.3)
            running = any(t.state == "running" for t in self.items.values())
            if self.dirty or running:
                self.dirty = False
                await self.hub.broadcast({"type": "transfers", "items": self.list()})

    def enqueue(self, t: Transfer, fn: Callable) -> Transfer:
        self.items[t.id] = t
        self.dirty = True
        t.task = asyncio.ensure_future(self._run(t, fn, queued=True))
        return t

    async def run_inline(self, t: Transfer, fn: Callable) -> None:
        self.items[t.id] = t
        await self._run(t, fn, queued=False)

    async def _run(self, t: Transfer, fn: Callable, queued: bool) -> None:
        if queued:
            await self.sem.acquire()
        try:
            if t.cancel:
                t.state = "cancelled"
                return
            t.state = "running"
            t.started = time.time()
            self.dirty = True
            await fn(t)
            t.state = "done"
        except (Cancelled, asyncio.CancelledError):
            t.state = "cancelled"
        except Exception as e:  # noqa: BLE001 - reported to the UI
            t.state = "error"
            t.error = str(e) or e.__class__.__name__
            log.warning("transfer %s failed: %s", t.name, t.error)
        finally:
            if queued:
                self.sem.release()
            t.finished = time.time()
            self.dirty = True

    def cancel(self, tid: str) -> None:
        t = self.items.get(tid)
        if not t:
            return
        t.cancel = True
        if t.state == "queued" and t.task:
            t.task.cancel()

    def clear(self) -> None:
        for k in [k for k, t in self.items.items() if t.state in ("done", "error", "cancelled")]:
            del self.items[k]
        self.dirty = True


def unique_local_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    if os.path.isdir(path):
        root, ext = path, ""
    i = 1
    while os.path.exists(f"{root} ({i}){ext}"):
        i += 1
    return f"{root} ({i}){ext}"


async def remote_makedirs(c: SFTPClient, path: str) -> None:
    if path in ("", "/") or await c.exists(path):
        return
    await remote_makedirs(c, parent(path))
    await c.mkdir(path)


async def download_tree(c: SFTPClient, remote: str, local: str, t: Transfer) -> None:
    os.makedirs(local, exist_ok=True)
    for e in await c.listdir(remote):
        r, l = join(remote, e.name), os.path.join(local, e.name)
        if e.attrs.is_dir:
            await download_tree(c, r, l, t)
        elif e.attrs.is_link and e.link_target_is_dir:
            continue  # do not follow directory links (loops)
        else:
            await c.download(r, l, t.progress, t.cancelled)


async def upload_tree(c: SFTPClient, local: str, remote: str, t: Transfer) -> None:
    if not await c.exists(remote):
        await c.mkdir(remote)
    for name in sorted(os.listdir(local)):
        p = os.path.join(local, name)
        if os.path.islink(p) and os.path.isdir(p):
            continue
        if os.path.isdir(p):
            await upload_tree(c, p, join(remote, name), t)
        elif os.path.isfile(p):
            await c.upload_file(p, join(remote, name), t.progress, t.cancelled)


def local_tree_size(path: str) -> int:
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


# ------------------------------------------------------------------- edits --

class Edit:
    def __init__(self, remote: str, local: str, target: SSHTarget) -> None:
        self.id = uuid.uuid4().hex[:10]
        self.remote = remote
        self.local = local
        self.target = target
        self.mtime = os.stat(local).st_mtime_ns
        self.state = "watching"
        self.saved_at: Optional[float] = None
        self.error = ""

    def to_json(self) -> dict:
        return {"id": self.id, "remote": self.remote, "name": os.path.basename(self.remote),
                "host": self.target.label, "state": self.state, "saved_at": self.saved_at,
                "error": self.error}


class EditManager:
    """Download to a temp file, open it in an editor, upload again on every save."""

    def __init__(self, hub: "Hub") -> None:
        self.hub = hub
        self.items: Dict[str, Edit] = {}
        asyncio.ensure_future(self._watch())

    def list(self) -> List[dict]:
        return [e.to_json() for e in self.items.values()]

    async def open(self, c: SFTPClient, target: SSHTarget, remote: str) -> Edit:
        for e in self.items.values():  # already open: just bring the editor up again
            if e.remote == remote and e.target.key == target.key:
                await self._launch(e.local)
                return e
        base = os.path.join(CACHE_DIR, "edit")
        os.makedirs(base, exist_ok=True)
        d = tempfile.mkdtemp(dir=base)
        local = os.path.join(d, os.path.basename(remote) or "file")
        await c.download(remote, local)
        e = Edit(remote, local, target)
        self.items[e.id] = e
        await self._launch(local)
        await self._emit()
        return e

    async def _launch(self, local: str) -> None:
        cmd = shlex.split(self.hub.settings["editor"] or "open -t") + [local]
        try:
            await asyncio.create_subprocess_exec(*cmd, stdin=asyncio.subprocess.DEVNULL,
                                                 start_new_session=True)
        except OSError as e:
            raise web.HTTPBadRequest(text=f"Impossibile avviare l'editor '{cmd[0]}': {e}")

    def stop(self, eid: str) -> None:
        e = self.items.pop(eid, None)
        if e:
            shutil.rmtree(os.path.dirname(e.local), ignore_errors=True)

    async def _emit(self) -> None:
        await self.hub.broadcast({"type": "edits", "items": self.list()})

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(1)
            for e in list(self.items.values()):
                try:
                    st = os.stat(e.local)
                except OSError:
                    continue
                if st.st_mtime_ns == e.mtime:
                    continue
                await asyncio.sleep(0.3)  # let the editor finish writing
                try:
                    st = os.stat(e.local)
                except OSError:
                    continue
                e.mtime = st.st_mtime_ns
                e.state = "uploading"
                await self._emit()
                try:
                    c = await self.hub.client_for(e.target)
                    await c.upload_file(e.local, e.remote)
                    e.state, e.error, e.saved_at = "watching", "", time.time()
                    await self.hub.toast("ok", f"Salvato sul server: {e.remote}")
                    await self.hub.broadcast({"type": "changed", "path": parent(e.remote)})
                except Exception as ex:  # noqa: BLE001
                    e.state, e.error = "error", str(ex)
                    await self.hub.toast("error", f"Upload di {e.remote} fallito: {ex}")
                await self._emit()


# --------------------------------------------------------------------- hub --

class Hub:
    def __init__(self, settings: Settings, local_hostname: str) -> None:
        self.settings = settings
        self.local_hostname = local_hostname
        self.sockets: set = set()
        self.session_id: Optional[str] = None
        self.session_name = ""
        self.target: Optional[SSHTarget] = None
        self.status = "idle"   # idle | nossh | nomux | connecting | ready | error
        self.error = ""
        self.pinned = False
        self.terminal_cwd: Optional[str] = None
        self.clients: Dict[tuple, SFTPClient] = {}
        self.allow_direct: set = set()
        self.connect_lock = asyncio.Lock()
        self.bridge = None
        self.url = ""
        self.transfers = TransferManager(self)
        self.edits = EditManager(self)

    # -- broadcasting

    def state(self) -> dict:
        t = self.target
        c = self.clients.get(t.key) if t else None
        return {
            "type": "state",
            "session_id": self.session_id,
            "session_name": self.session_name,
            "target": t.to_json() if t else None,
            "status": self.status,
            "error": self.error,
            "pinned": self.pinned,
            "home": c.home if c and not c.closed else None,
            "terminal_cwd": self.terminal_cwd,
            "settings": self.settings.data,
            "snippet": CONTROLMASTER_SNIPPET,
            "demo": isinstance(self.bridge, DemoBridge),
        }

    async def broadcast(self, msg: dict) -> None:
        for ws in list(self.sockets):
            try:
                await ws.send_json(msg)
            except Exception:  # noqa: BLE001
                self.sockets.discard(ws)

    async def toast(self, level: str, text: str) -> None:
        await self.broadcast({"type": "toast", "level": level, "text": text})

    async def push_state(self) -> None:
        await self.broadcast(self.state())

    # -- session / connection

    async def set_session(self, session_id: str, name: str, target: Optional[SSHTarget],
                          cwd: Optional[str]) -> None:
        self.session_id, self.session_name, self.terminal_cwd = session_id, name, cwd
        self.target = target
        self.error = ""
        if target is None:
            self.status = "nossh"
        elif target.pam:
            self.status = "pam"
        elif target.key in self.clients and not self.clients[target.key].closed:
            self.status = "ready"
        elif target.multiplexed or target.key in self.allow_direct:
            self.status = "connecting"
        else:
            self.status = "nomux"
        await self.push_state()
        if self.status == "connecting":
            try:
                await self.client()
            except Exception:  # noqa: BLE001 - reported through state
                pass

    async def set_terminal_cwd(self, cwd: Optional[str]) -> None:
        if cwd != self.terminal_cwd:
            self.terminal_cwd = cwd
            await self.broadcast({"type": "cwd", "path": cwd})

    async def client(self) -> SFTPClient:
        if self.target is None:
            raise web.HTTPConflict(text="La sessione attiva non è una connessione SSH")
        if self.target.pam:
            raise web.HTTPConflict(text="Connessione tramite proxy PAM: non condivisibile")
        if not (self.target.multiplexed or self.target.key in self.allow_direct):
            raise web.HTTPConflict(text="Nessun ControlMaster attivo per questa sessione")
        return await self.client_for(self.target, update_state=True)

    async def client_for(self, t: SSHTarget, update_state: bool = False) -> SFTPClient:
        async with self.connect_lock:
            c = self.clients.get(t.key)
            if c and not c.closed:
                return c
            if update_state:
                self.status, self.error = "connecting", ""
                await self.push_state()
            c = SFTPClient(t.sftp_argv())
            try:
                await c.connect()
            except Exception as e:  # noqa: BLE001
                if update_state and self.target is t:
                    self.status, self.error = "error", str(e)
                    await self.push_state()
                raise web.HTTPBadGateway(text=f"Connessione SFTP fallita: {e}")
            log.info("SFTP ready on %s (%s)", t.label, "mux" if t.multiplexed else "direct")
            self.clients[t.key] = c
            if self.target is t:
                self.status = "ready"
                await self.push_state()
            return c


# ----------------------------------------------------------------- bridges --

class DemoTarget(SSHTarget):
    def sftp_argv(self) -> List[str]:
        return ["/usr/libexec/sftp-server"]


class DemoBridge:
    """Stand-in for iTerm2: browses this Mac through the local sftp-server."""

    def __init__(self, hub: Hub) -> None:
        self.hub = hub

    async def start(self) -> None:
        t = DemoTarget(pid=0, argv=["demo"], destination="localhost", user=os.environ.get("USER", ""),
                       hostname="localhost (demo)", multiplexed=True)
        await self.hub.set_session("demo", "Demo", t, os.environ.get("SFTP_DEMO_DIR") or os.path.expanduser("~"))

    async def refresh(self) -> None:
        await self.start()

    async def send_cd(self, path: str) -> None:
        log.info("[demo] cd %s", path)

    async def set_pinned(self, pinned: bool) -> None:
        self.hub.pinned = pinned
        await self.hub.push_state()


class ITermBridge:
    def __init__(self, hub: Hub, connection, app) -> None:
        import iterm2  # noqa: F401
        self.hub = hub
        self.conn = connection
        self.app = app
        self.session_id: Optional[str] = None
        self.watchers: List[asyncio.Future] = []
        self.refresh_task: Optional[asyncio.Future] = None
        self.recheck_task: Optional[asyncio.Future] = None
        self.si_cwd: Optional[str] = None      # from iTerm2 shell integration
        self.probe_cwd: Optional[str] = None   # from CWD_PROBE over the mux
        self.probe_task: Optional[asyncio.Future] = None
        self.probe_again = False
        self.last_probe = 0.0

    async def start(self) -> None:
        import iterm2
        await iterm2.tool.async_register_web_view_tool(self.conn, "SFTP", TOOL_ID, False, self.hub.url)
        asyncio.ensure_future(self._focus_loop())
        await self._activate_current()

    def _current_session(self):
        w = self.app.current_terminal_window
        if w and w.current_tab:
            return w.current_tab.current_session
        return None

    async def _activate_current(self) -> None:
        s = self._current_session()
        if s and s.session_id != self.session_id:
            await self.activate(s.session_id)

    async def _focus_loop(self) -> None:
        import iterm2
        async with iterm2.FocusMonitor(self.conn) as mon:
            while True:
                await mon.async_get_next_update()
                if not self.hub.pinned:
                    try:
                        await self._activate_current()
                    except Exception as e:  # noqa: BLE001
                        log.warning("focus update failed: %s", e)

    async def activate(self, session_id: str) -> None:
        for w in self.watchers:
            w.cancel()
        self.session_id = session_id
        self.si_cwd = self.probe_cwd = None
        self.watchers = [asyncio.ensure_future(self._watch(session_id, v))
                         for v in ("jobName", "path", "hostname")]
        self.watchers.append(asyncio.ensure_future(self._watch_screen(session_id)))
        await self.refresh()

    async def _remote_cwd(self, s) -> Optional[str]:
        path = await s.async_get_variable("path")
        host = (await s.async_get_variable("hostname") or "").split(".")[0]
        if path and host and host.lower() != self.hub.local_hostname.lower():
            return path
        return None

    async def refresh(self) -> None:
        s = self.app.get_session_by_id(self.session_id) if self.session_id else None
        if s is None:
            await self.hub.set_session(None, "", None, None)
            return
        tty = await s.async_get_variable("tty")
        name = await s.async_get_variable("name") or ""
        target = await resolve_tty(tty)
        self.si_cwd = await self._remote_cwd(s)
        if target is None or (self.hub.target and target.key != self.hub.target.key):
            self.probe_cwd = None
        await self.hub.set_session(s.session_id, name, target, self.si_cwd or self.probe_cwd)
        if target and not target.multiplexed:
            self._schedule_recheck()
        self.kick_probe()

    # -- follow the terminal folder without shell integration

    def kick_probe(self) -> None:
        """Ask the server for the terminal's cwd (debounced, never concurrent)."""
        if self.probe_task and not self.probe_task.done():
            self.probe_again = True
            return
        self.probe_task = asyncio.ensure_future(self._probe(self.session_id))

    async def _probe(self, sid: Optional[str]) -> None:
        await asyncio.sleep(max(0.35, 1.0 - (time.time() - self.last_probe)))
        while True:
            self.probe_again = False
            t = self.hub.target
            if (sid != self.session_id or not self.hub.settings["follow"] or self.si_cwd
                    or t is None or not t.multiplexed or isinstance(t, DemoTarget)):
                return
            self.last_probe = time.time()
            try:
                cwd = await probe_remote_cwd(t)
            except Exception as e:  # noqa: BLE001
                log.warning("cwd probe failed: %s", e)
                cwd = None
            if cwd and sid == self.session_id and self.hub.target is t and not self.si_cwd:
                self.probe_cwd = cwd
                await self.hub.set_terminal_cwd(cwd)
            if not self.probe_again:
                return
            await asyncio.sleep(1.0)

    async def _watch_screen(self, session_id: str) -> None:
        """Every screen update (e.g. after `cd` + Enter) triggers a cwd probe."""
        s = self.app.get_session_by_id(session_id)
        if s is None:
            return
        try:
            async with s.get_screen_streamer(want_contents=False) as streamer:
                while True:
                    await streamer.async_get()
                    if self.session_id != session_id:
                        return
                    self.kick_probe()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("screen streamer ended: %s", e)

    def _schedule_recheck(self) -> None:
        """Right after `ssh` starts the master socket does not exist yet: look again for a while."""
        if self.recheck_task and not self.recheck_task.done():
            return

        async def loop(sid):
            for delay in (1, 2, 3, 5, 8, 13):
                await asyncio.sleep(delay)
                if self.session_id != sid or self.hub.status not in ("nomux",):
                    return
                await self.refresh()
        self.recheck_task = asyncio.ensure_future(loop(self.session_id))

    def _debounced_refresh(self) -> None:
        if self.refresh_task and not self.refresh_task.done():
            self.refresh_task.cancel()

        async def later():
            await asyncio.sleep(0.8)
            await self.refresh()
        self.refresh_task = asyncio.ensure_future(later())

    async def _watch(self, session_id: str, var: str) -> None:
        import iterm2
        try:
            async with iterm2.VariableMonitor(self.conn, iterm2.VariableScopes.SESSION, var, session_id) as mon:
                while True:
                    await mon.async_get()
                    if self.session_id != session_id:
                        return
                    if var == "jobName":
                        self._debounced_refresh()
                    else:
                        s = self.app.get_session_by_id(session_id)
                        if s:
                            self.si_cwd = await self._remote_cwd(s)
                            await self.hub.set_terminal_cwd(self.si_cwd or self.probe_cwd)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("variable monitor %s ended: %s", var, e)

    async def send_cd(self, path: str) -> None:
        s = self.app.get_session_by_id(self.session_id) if self.session_id else None
        if s is None:
            raise web.HTTPConflict(text="Nessuna sessione attiva")
        await s.async_send_text("cd " + shlex.quote(path) + "\n")

    async def set_pinned(self, pinned: bool) -> None:
        self.hub.pinned = pinned
        if not pinned:
            await self._activate_current()
        await self.hub.push_state()


# -------------------------------------------------------------- pickers ----

async def osascript(script: str) -> Optional[str]:
    proc = await asyncio.create_subprocess_exec(
        "osascript", "-e", script, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        if b"-128" in err:  # user cancelled
            return None
        raise web.HTTPInternalServerError(text=err.decode("utf-8", "replace").strip())
    return out.decode("utf-8", "replace")


async def pick_folder(prompt: str) -> Optional[str]:
    out = await osascript(f'activate\nPOSIX path of (choose folder with prompt {json.dumps(prompt)})')
    return out.strip() if out else None


async def pick_files(prompt: str) -> List[str]:
    out = await osascript(
        f'activate\nset fs to choose file with prompt {json.dumps(prompt)} with multiple selections allowed\n'
        'set out to ""\nrepeat with f in fs\nset out to out & POSIX path of f & linefeed\nend repeat\nreturn out')
    return [l for l in (out or "").splitlines() if l]


async def pbcopy(text: str) -> None:
    proc = await asyncio.create_subprocess_exec("pbcopy", stdin=asyncio.subprocess.PIPE)
    await proc.communicate(text.encode("utf-8"))


# ------------------------------------------------------------------ web ----

def make_app(hub: Hub, token: str, port: int) -> web.Application:
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    @web.middleware
    async def guard(request: web.Request, handler):
        if request.host not in allowed_hosts:  # DNS rebinding
            raise web.HTTPForbidden(text="bad host")
        supplied = request.headers.get("X-Token") or request.query.get("t", "")
        if not secrets.compare_digest(supplied, token):
            raise web.HTTPForbidden(text="bad token")
        try:
            return await handler(request)
        except SFTPError as e:
            raise web.HTTPBadRequest(text=str(e))
        except ConnectionError as e:
            if hub.target:
                hub.clients.pop(hub.target.key, None)
            raise web.HTTPBadGateway(text=f"Connessione persa: {e}")

    routes = web.RouteTableDef()

    async def body(request) -> dict:
        try:
            return await request.json()
        except ValueError:
            raise web.HTTPBadRequest(text="JSON non valido")

    def ok(**kw):
        return web.json_response({"ok": True, **kw})

    async def changed(*dirs: str):
        """Tell every open view (Toolbelt, browser window) to reload these folders."""
        for d in set(dirs):
            await hub.broadcast({"type": "changed", "path": d})
        return ok()

    @routes.get("/")
    async def index(request):
        return web.FileResponse(os.path.join(STATIC_DIR, "index.html"),
                                headers={"Cache-Control": "no-store"})

    @routes.get("/ws")
    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        hub.sockets.add(ws)
        await ws.send_json(hub.state())
        await ws.send_json({"type": "transfers", "items": hub.transfers.list()})
        await ws.send_json({"type": "edits", "items": hub.edits.list()})
        try:
            async for msg in ws:
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            hub.sockets.discard(ws)
        return ws

    @routes.get("/api/ls")
    async def ls(request):
        c = await hub.client()
        path = request.query.get("path") or c.home
        real = await c.realpath(path)
        entries = await c.listdir(real)
        return web.json_response({"path": real, "entries": [e.to_json() for e in entries]})

    @routes.post("/api/mkdir")
    async def mkdir(request):
        path = (await body(request))["path"]
        await (await hub.client()).mkdir(path)
        return await changed(parent(path))

    @routes.post("/api/touch")
    async def touch(request):
        path = (await body(request))["path"]
        await (await hub.client()).touch(path)
        return await changed(parent(path))

    @routes.post("/api/rename")
    async def rename(request):
        b = await body(request)
        await (await hub.client()).rename(b["from"], b["to"])
        return await changed(parent(b["from"]), parent(b["to"]))

    @routes.post("/api/delete")
    async def delete(request):
        c = await hub.client()
        paths = (await body(request))["paths"]
        for p in paths:
            if p in ("", "/"):
                raise web.HTTPBadRequest(text="Non cancello la root")
            await c.rmtree(p)
        return await changed(*{parent(p) for p in paths})

    @routes.post("/api/chmod")
    async def chmod(request):
        c = await hub.client()
        b = await body(request)
        mode = int(b["mode"]) & 0o7777

        async def apply(p: str) -> None:
            a = await c.lstat(p)
            if a.is_link:
                return
            await c.chmod(p, mode if not a.is_dir or not b.get("files_only") else a.permissions & 0o7777)
            if a.is_dir and b.get("recursive"):
                for e in await c.listdir(p):
                    await apply(join(p, e.name))

        for p in b["paths"]:
            await apply(p)
        return await changed(*{parent(p) for p in b["paths"]})

    @routes.post("/api/download")
    async def download(request):
        c = await hub.client()
        b = await body(request)
        dest = hub.settings.download_dir
        if b.get("ask") or hub.settings["ask_download_dir"]:
            dest = await pick_folder("Scarica in…")
            if not dest:
                return ok(cancelled=True)
        os.makedirs(dest, exist_ok=True)
        for remote in b["paths"]:
            t = Transfer("down", os.path.basename(remote.rstrip("/")) or remote, remote=remote)

            async def run(t: Transfer, remote=remote) -> None:
                a = await c.stat(remote)
                t.local = unique_local_path(os.path.join(dest, t.name))
                if a.is_dir:
                    t.total = await c.walk_size(remote)
                    await download_tree(c, remote, t.local, t)
                else:
                    t.total = a.size or 0
                    await c.download(remote, t.local, t.progress, t.cancelled)
            hub.transfers.enqueue(t, run)
        return ok()

    @routes.post("/api/upload_pick")
    async def upload_pick(request):
        c = await hub.client()
        b = await body(request)
        remote_dir = b["dir"]
        if b.get("folder"):
            f = await pick_folder("Cartella da caricare")
            locals_ = [f.rstrip("/")] if f else []
        else:
            locals_ = await pick_files("File da caricare")
        for local in locals_:
            name = os.path.basename(local)
            t = Transfer("up", name, remote=join(remote_dir, name), local=local)

            async def run(t: Transfer, local=local) -> None:
                t.total = await asyncio.get_event_loop().run_in_executor(None, local_tree_size, local)
                if os.path.isdir(local):
                    await upload_tree(c, local, t.remote, t)
                else:
                    await c.upload_file(local, t.remote, t.progress, t.cancelled)
                await hub.broadcast({"type": "changed", "path": remote_dir})
            hub.transfers.enqueue(t, run)
        return ok(count=len(locals_))

    @routes.put("/api/upload")
    async def upload_http(request):
        """Body is the raw file (drag & drop from Finder). `rel` may contain sub folders."""
        c = await hub.client()
        remote_dir = request.query["dir"]
        rel = request.query["rel"].strip("/")
        parts = [p for p in rel.split("/") if p]
        if not parts or any(p in (".", "..") for p in parts):
            raise web.HTTPBadRequest(text="Percorso non valido")
        remote = join(remote_dir, "/".join(parts))
        t = Transfer("up", rel, remote=remote)
        t.total = request.content_length or 0

        async def run(t: Transfer) -> None:
            if len(parts) > 1:
                await remote_makedirs(c, parent(remote))

            async def chunks() -> bytes:
                return await request.content.read(CHUNK * 4)
            await c.upload_stream(remote, chunks, t.progress, t.cancelled)
            mtime = request.query.get("mtime")
            if mtime:
                try:
                    await c.setmtime(remote, int(time.time()), int(float(mtime)))
                except SFTPError:
                    pass
        await hub.transfers.run_inline(t, run)
        if t.state == "done":
            await hub.broadcast({"type": "changed", "path": parent(remote)})
            return ok()
        return web.json_response({"ok": False, "error": t.error or t.state}, status=500)

    @routes.post("/api/edit")
    async def edit(request):
        c = await hub.client()
        await hub.edits.open(c, hub.target, (await body(request))["path"])
        return ok()

    @routes.post("/api/edit_stop")
    async def edit_stop(request):
        hub.edits.stop((await body(request))["id"])
        await hub.broadcast({"type": "edits", "items": hub.edits.list()})
        return ok()

    @routes.post("/api/cancel")
    async def cancel(request):
        hub.transfers.cancel((await body(request))["id"])
        return ok()

    @routes.post("/api/transfers_clear")
    async def transfers_clear(request):
        hub.transfers.clear()
        return ok()

    @routes.post("/api/reveal")
    async def reveal(request):
        b = await body(request)
        t = hub.transfers.items.get(b.get("id", ""))
        path = t.local if t else ""
        if not path or not os.path.exists(path):
            path = hub.settings.download_dir
        await asyncio.create_subprocess_exec("open", "-R", path)
        return ok()

    @routes.post("/api/open_local")
    async def open_local(request):
        """Download to a temp dir and open with the default macOS app (no sync back)."""
        c = await hub.client()
        remote = (await body(request))["path"]
        base = os.path.join(CACHE_DIR, "open")
        os.makedirs(base, exist_ok=True)
        d = tempfile.mkdtemp(dir=base)
        local = os.path.join(d, os.path.basename(remote))
        await c.download(remote, local)
        await asyncio.create_subprocess_exec("open", local)
        return ok()

    @routes.post("/api/pick_folder")
    async def pick_folder_route(request):
        return ok(path=await pick_folder("Scegli una cartella"))

    @routes.post("/api/cd")
    async def cd(request):
        await hub.bridge.send_cd((await body(request))["path"])
        return ok()

    @routes.post("/api/copy")
    async def copy(request):
        await pbcopy((await body(request))["text"])
        return ok()

    @routes.post("/api/settings")
    async def settings(request):
        hub.settings.update(await body(request))
        await hub.push_state()
        if hasattr(hub.bridge, "kick_probe"):
            hub.bridge.kick_probe()
        return ok()

    @routes.post("/api/pin")
    async def pin(request):
        await hub.bridge.set_pinned(bool((await body(request)).get("pinned")))
        return ok()

    @routes.post("/api/connect_direct")
    async def connect_direct(request):
        if hub.target and not hub.target.pam:
            hub.allow_direct.add(hub.target.key)
            await hub.client()
        return ok()

    @routes.post("/api/reconnect")
    async def reconnect(request):
        if hub.target:
            old = hub.clients.pop(hub.target.key, None)
            if old:
                await old.close()
        await hub.bridge.refresh()
        return ok()

    @routes.post("/api/open_browser")
    async def open_browser(request):
        await asyncio.create_subprocess_exec("open", hub.url)
        return ok()

    app = web.Application(middlewares=[guard], client_max_size=1024 ** 4)
    app.add_routes(routes)
    return app


async def start_server(hub: Hub, token: str) -> web.AppRunner:
    port = PREFERRED_PORT
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
    runner = web.AppRunner(make_app(hub, token, port), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    hub.url = f"http://127.0.0.1:{port}/?t={token}"
    log.info("SFTP Browser listening on http://127.0.0.1:%d", port)
    return runner


def local_hostname() -> str:
    return socket.gethostname().split(".")[0]


async def run_demo() -> None:
    hub = Hub(Settings(), local_hostname())
    hub.bridge = DemoBridge(hub)
    await start_server(hub, load_token())
    print(hub.url, flush=True)
    await hub.bridge.start()
    await asyncio.Event().wait()


def run_iterm() -> None:
    import iterm2

    async def main(connection):
        app = await iterm2.async_get_app(connection)
        hub = Hub(Settings(), local_hostname())
        await start_server(hub, load_token())
        hub.bridge = ITermBridge(hub, connection, app)
        await hub.bridge.start()
        reconnector = Reconnector(connection, app, lambda: hub.settings["reconnect"])
        asyncio.ensure_future(reconnector.start())  # runs forever, one watcher per session
        log.info("Registered Toolbelt tool 'SFTP' (Toolbelt ▸ SFTP)")

    iterm2.run_forever(main)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="run without iTerm2 against the local sftp-server")
    args = ap.parse_args()
    os.makedirs(APP_DIR, exist_ok=True)
    handlers = [logging.StreamHandler(),  # iTerm2 ▸ Scripts ▸ Manage ▸ Console
                logging.handlers.RotatingFileHandler(os.path.join(APP_DIR, "plugin.log"),
                                                     maxBytes=512 * 1024, backupCount=1)]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)
    if args.demo:
        asyncio.run(run_demo())
    else:
        run_iterm()
