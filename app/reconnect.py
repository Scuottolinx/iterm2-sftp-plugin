"""MobaXterm-style "press R to reconnect" for every iTerm2 session.

When the `ssh` running in a session exits because the connection dropped (not
after a normal `exit`/`logout`), a message is printed in the terminal and the
next keystroke decides: R re-runs the same ssh command, anything else goes to
the shell as usual. Works for every tab, whether the Toolbelt is visible or not.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shlex
from typing import Callable, Dict, List, Optional

from sshresolve import find_ssh_on_tty

log = logging.getLogger("sftp-browser.reconnect")

# Messages printed by OpenSSH when the connection is lost or cannot be made.
_DROP = re.compile("|".join([
    r"closed by remote host",
    r"client_loop: send disconnect",
    r"broken pipe",
    r"connection reset",
    r"timeout, server .* not responding",
    r"connection timed out",
    r"operation timed out",
    r"connection refused",
    r"no route to host",
    r"network is unreachable",
    r"network is down",
    r"packet_write_wait",
    r"read from remote host .*:",
    r"kex_exchange_identification",
    r"connection closed by \S+ port \d+",
    r"could not resolve hostname",
]), re.IGNORECASE)

# `exit` typed at the prompt or the "logout" printed by the remote shell. (OpenSSH
# prints "Connection to host closed." both after a logout and after a drop.)
_CLEAN = re.compile(r"(^|[\s#$%>])(exit|logout)\s*$", re.IGNORECASE)


def connection_dropped(lines: List[str]) -> bool:
    """Look at the last lines of the terminal right after ssh exited."""
    tail = [l.strip() for l in lines if l.strip()][-8:]
    for line in reversed(tail):
        if _DROP.search(line):
            return True
        if _CLEAN.search(line):
            return False
    return False


def banner(host: str) -> bytes:
    msg = (f"\r\n\x1b[0;1;37;41m ✖ Connessione a {host} persa \x1b[0m "
           f"\x1b[1mPremi \x1b[1;33mR\x1b[0;1m per riconnetterti\x1b[0m"
           f"\x1b[2m (un altro tasto per ignorare)\x1b[0m\r\n")
    return msg.encode("utf-8")


class Reconnector:
    def __init__(self, connection, app, enabled: Callable[[], bool]) -> None:
        self.conn = connection
        self.app = app
        self.enabled = enabled
        self.last_argv: Dict[str, List[str]] = {}   # session id -> ssh argv
        self.armed: Dict[str, asyncio.Future] = {}

    async def start(self) -> None:
        import iterm2
        await iterm2.EachSessionOnceMonitor.async_foreach_session_create_task(self.app, self._watch)

    async def _remember_ssh(self, session) -> None:
        tty = await session.async_get_variable("tty")
        loop = asyncio.get_event_loop()
        for _ in range(5):  # jobName changes slightly before exec completes
            p = await loop.run_in_executor(None, find_ssh_on_tty, tty)
            if p is not None:
                try:
                    argv = p.cmdline()
                except Exception:  # noqa: BLE001
                    argv = None
                if argv and "-W" not in argv:
                    self.last_argv[session.session_id] = argv
                    return
            await asyncio.sleep(0.4)

    async def _watch(self, session_id: str) -> None:
        import iterm2
        session = self.app.get_session_by_id(session_id)
        if session is None:
            return
        prev = await session.async_get_variable("jobName") or ""
        if prev == "ssh":
            await self._remember_ssh(session)
        try:
            async with iterm2.VariableMonitor(self.conn, iterm2.VariableScopes.SESSION, "jobName", session_id) as mon:
                while True:
                    job = await mon.async_get() or ""
                    if job == prev:
                        continue
                    if job == "ssh":
                        self._disarm(session_id)
                        await self._remember_ssh(session)
                    elif prev == "ssh":
                        await self._ssh_exited(session)
                    prev = job
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - session closed
            log.debug("reconnect watcher for %s ended: %s", session_id, e)
        finally:
            self._disarm(session_id)
            self.last_argv.pop(session_id, None)

    async def _ssh_exited(self, session) -> None:
        argv = self.last_argv.get(session.session_id)
        if not argv or not self.enabled():
            return
        await asyncio.sleep(0.3)  # let the last lines reach the screen
        try:
            contents = await session.async_get_screen_contents()
        except Exception:  # noqa: BLE001
            return
        lines = [contents.line(i).string for i in range(contents.number_of_lines)]
        if not connection_dropped(lines):
            return
        host = _host_from_argv(argv)
        from sshresolve import is_pam_proxy
        if is_pam_proxy("", "", host):
            return  # one-time session token: re-running the same command cannot work
        log.info("connection to %s dropped in session %s", host, session.session_id)
        await session.async_inject(banner(host))
        self._disarm(session.session_id)
        self.armed[session.session_id] = asyncio.ensure_future(self._wait_key(session, argv))

    def _disarm(self, session_id: str) -> None:
        fut = self.armed.pop(session_id, None)
        if fut and not fut.done():
            fut.cancel()

    async def _wait_key(self, session, argv: List[str]) -> None:
        import iterm2
        pat = iterm2.KeystrokePattern()
        pat.characters = ["r", "R"]
        pat.forbidden_modifiers = [iterm2.Modifier.COMMAND, iterm2.Modifier.CONTROL, iterm2.Modifier.OPTION]
        sid = session.session_id
        try:
            # R is swallowed while armed; every other key reaches the shell untouched.
            async with iterm2.KeystrokeMonitor(self.conn, session=sid) as mon, \
                    iterm2.KeystrokeFilter(self.conn, [pat], session=sid):
                while True:
                    ks = await mon.async_get()
                    if ks.action not in (iterm2.Keystroke.Action.KEY_DOWN, iterm2.Keystroke.Action.NA):
                        continue
                    mods = set(ks.modifiers or [])
                    if ks.characters in ("r", "R") and not mods & {
                            iterm2.Modifier.COMMAND, iterm2.Modifier.CONTROL, iterm2.Modifier.OPTION}:
                        await session.async_inject(b"\x1b[2m\xe2\x86\xbb riconnessione...\x1b[0m\r\n")
                        await session.async_send_text(shlex.join(argv) + "\n")
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001
            log.warning("reconnect prompt failed: %s", e)
        finally:
            if self.armed.get(sid) is asyncio.current_task():
                self.armed.pop(sid, None)


def _host_from_argv(argv: List[str]) -> str:
    from sshresolve import parse_ssh_argv
    parsed = parse_ssh_argv(argv)
    return parsed[0] if parsed else "server"
