"""Find the `ssh` client running in a terminal and derive an SFTP command that
reuses its connection through OpenSSH multiplexing (ControlMaster)."""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

import psutil

# From ssh.c getopt string (OpenSSH 9.x/10.x)
_WITH_ARG = set("BbcDEeFIiJLlmOoPpQRSWw")
_FLAGS = set("1246AaCfGgKkMNnqsTtVvXxYy")
# Options worth carrying over to the SFTP channel (identity, config, port, jump...)
_KEEP_WITH_ARG = set("BbcFiJlmoPpS")
_KEEP_FLAGS = set("46C")
# -o keys that would make a second channel misbehave
_DROP_O = {
    "localforward", "remoteforward", "dynamicforward", "remotecommand", "requesttty",
    "sessiontype", "forkafterauthentication", "stdinnull", "controlmaster", "permitlocalcommand",
    "localcommand", "tunnel", "tunneldevice", "forwardx11", "forwardx11trusted", "forwardagent",
    "exitonforwardfailure", "controlpersist",
}

# PAM / session-recording SSH proxies drop the whole connection
# when a second channel is opened on it. Their users are one-time session UUIDs.
_SESSION_TOKEN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
PAM_HOSTS: set = set()  # extra hostnames from the settings ("pam_hosts")


def is_pam_proxy(user: str, hostname: str, destination: str = "") -> bool:
    if _SESSION_TOKEN.match(user or "") or _SESSION_TOKEN.match(destination.split("@")[0]):
        return True
    return (hostname or "").lower() in PAM_HOSTS


SAFE_OPTS = [
    "-o", "ControlMaster=no",       # use an existing master, never become one
    "-o", "ClearAllForwardings=yes",
    "-o", "RequestTTY=no",
    "-o", "RemoteCommand=none",
    "-o", "PermitLocalCommand=no",
    "-o", "ForwardX11=no",
    "-o", "ForwardAgent=no",
    "-o", "BatchMode=yes",           # never prompt: we have no terminal
    "-o", "ConnectTimeout=15",
]


@dataclass
class SSHTarget:
    pid: int
    argv: List[str]
    destination: str
    options: List[str] = field(default_factory=list)
    user: str = ""
    hostname: str = ""
    port: str = "22"
    control_path: str = ""
    multiplexed: bool = False
    pam: bool = False  # never open extra channels on this connection

    @property
    def key(self) -> tuple:
        return tuple(self.options) + (self.destination,)

    @property
    def label(self) -> str:
        host = self.hostname or self.destination
        s = f"{self.user}@{host}" if self.user else host
        return s if self.port in ("", "22") else f"{s}:{self.port}"

    def base_cmd(self) -> List[str]:
        return ["ssh", *self.options, *SAFE_OPTS]

    def sftp_argv(self) -> List[str]:
        return [*self.base_cmd(), "-s", self.destination, "sftp"]

    def to_json(self) -> dict:
        return {
            "label": self.label,
            "destination": self.destination,
            "user": self.user,
            "hostname": self.hostname,
            "port": self.port,
            "multiplexed": self.multiplexed,
            "pam": self.pam,
            "control_path": self.control_path,
            "command": " ".join(self.argv),
        }


# Runs on the server through the multiplexed connection. Our shell and the user's
# interactive shell are children of the same per-connection sshd, so we can find
# the terminal's tty and read the cwd of its foreground process. POSIX sh only,
# `ps -o x= -o y=` (not `x=,y=`) for old procps (RHEL 5).
CWD_PROBE = r"""
# sshd processes serving this connection = our sshd ancestors, minus the listener daemon
anc=" "
p=$PPID
while [ -n "$p" ] && [ "$p" -gt 1 ] 2>/dev/null; do
  pp=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')
  case "$(ps -o comm= -p "$p" 2>/dev/null)" in
    sshd*) [ "$pp" = 1 ] && break; anc="$anc$p " ;;
  esac
  p=$pp
done
[ "$anc" != " " ] || exit 3
# interactive ttys opened by those sshd processes -> cwd of the foreground process
ps -e -o ppid= -o tty= 2>/dev/null | while read pp t; do
  case "$anc" in *" $pp "*) ;; *) continue ;; esac
  case "$t" in "?"|"??"|"") continue ;; esac
  echo "$t"
done | sort -u | while read t; do
  f=$(ps -t "$t" -o pid= -o stat= 2>/dev/null | awk '$2 ~ /\+/ {x=$1} END {print x}')
  [ -n "$f" ] || continue
  d=$(readlink "/proc/$f/cwd" 2>/dev/null)
  [ -n "$d" ] || d=$(lsof -a -p "$f" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')
  [ -n "$d" ] || continue
  m=$(stat -c %Y "/dev/$t" 2>/dev/null || stat -f %m "/dev/$t" 2>/dev/null || echo 0)
  echo "$m $d"
done | sort -rn | head -n 1 | cut -d' ' -f2-
"""


async def probe_remote_cwd(target: "SSHTarget", timeout: float = 10) -> Optional[str]:
    """Current directory of the shell in the terminal, without shell integration."""
    proc = await asyncio.create_subprocess_exec(
        *target.base_cmd(), "-T", target.destination, "sh -s",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(CWD_PROBE.encode()), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return None
    path = out.decode("utf-8", "replace").strip()
    return path if proc.returncode == 0 and path.startswith("/") else None


def parse_ssh_argv(argv: List[str]) -> Optional[tuple]:
    """Returns (destination, kept_options) or None if this ssh is not an interactive session."""
    kept: List[str] = []
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--":
            i += 1
            break
        if not a.startswith("-") or a == "-":
            break
        j = 1
        while j < len(a):
            c = a[j]
            if c in _WITH_ARG:
                val = a[j + 1:]
                if not val:
                    i += 1
                    if i >= len(argv):
                        return None
                    val = argv[i]
                if c in "GOQVW":
                    return None
                if c == "o":
                    k = val.replace("=", " ", 1).split(None, 1)[0].lower()
                    if k in _DROP_O:
                        break
                if c in _KEEP_WITH_ARG:
                    kept += [f"-{c}", val]
                break
            if c in _FLAGS:
                if c in "GV":
                    return None
                if c in _KEEP_FLAGS:
                    kept.append(f"-{c}")
            j += 1
        i += 1
    if i >= len(argv):
        return None
    return argv[i], kept


def _is_ssh(p: psutil.Process) -> bool:
    try:
        return os.path.basename(p.name()) == "ssh"
    except (psutil.Error, OSError):
        return False


def find_ssh_on_tty(tty: str) -> Optional[psutil.Process]:
    """The outermost, most recent `ssh` whose controlling terminal is `tty`."""
    if not tty:
        return None
    candidates = []
    for p in psutil.process_iter(["name", "terminal", "create_time"]):
        try:
            if p.info["terminal"] != tty or os.path.basename(p.info["name"] or "") != "ssh":
                continue
            par = p.parent()
            if par is not None and _is_ssh(par):
                continue  # ProxyJump / ProxyCommand helper
            candidates.append(p)
        except (psutil.Error, OSError):
            continue
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.info["create_time"] or 0)


async def _run(cmd: List[str], timeout: float = 10) -> tuple:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "", "timeout"
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def resolve_tty(tty: str) -> Optional[SSHTarget]:
    p = await asyncio.get_event_loop().run_in_executor(None, find_ssh_on_tty, tty)
    if p is None:
        return None
    try:
        argv = p.cmdline()
    except (psutil.Error, OSError):
        return None
    parsed = parse_ssh_argv(argv)
    if not parsed:
        return None
    dest, opts = parsed
    t = SSHTarget(pid=p.pid, argv=argv, destination=dest, options=opts)

    rc, out, _ = await _run(["ssh", *opts, "-G", dest])
    if rc == 0:
        cfg = {}
        for line in out.splitlines():
            k, _, v = line.partition(" ")
            cfg.setdefault(k.lower(), v)
        t.user = cfg.get("user", "")
        t.hostname = cfg.get("hostname", "")
        t.port = cfg.get("port", "22")
        cp = cfg.get("controlpath", "")
        t.control_path = "" if cp in ("", "none") else os.path.expanduser(cp)

    if is_pam_proxy(t.user, t.hostname, dest):
        t.pam = True  # not even a check: nothing may touch this connection
        return t
    if t.control_path:
        rc, _, _ = await _run([*t.base_cmd(), "-O", "check", dest], timeout=5)
        t.multiplexed = rc == 0
    return t
