"""Minimal asyncio SFTP v3 client that talks over any subprocess stdin/stdout.

It is meant to run on top of `ssh -s <host> sftp`, which lets OpenSSH reuse an
existing ControlMaster connection, so no new authentication is required.
"""
from __future__ import annotations

import asyncio
import os
import stat as statmod
import struct
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional

# Packet types
INIT, VERSION, OPEN, CLOSE, READ, WRITE, LSTAT, FSTAT, SETSTAT = 1, 2, 3, 4, 5, 6, 7, 8, 9
OPENDIR, READDIR, REMOVE, MKDIR, RMDIR, REALPATH, STAT, RENAME, READLINK = 11, 12, 13, 14, 15, 16, 17, 18, 19
STATUS, HANDLE, DATA, NAME, ATTRS = 101, 102, 103, 104, 105
EXTENDED, EXTENDED_REPLY = 200, 201

# Attribute flags
A_SIZE, A_UIDGID, A_PERMISSIONS, A_ACMODTIME, A_EXTENDED = 0x1, 0x2, 0x4, 0x8, 0x80000000

# Open flags
F_READ, F_WRITE, F_APPEND, F_CREAT, F_TRUNC, F_EXCL = 0x1, 0x2, 0x4, 0x8, 0x10, 0x20

S_OK, S_EOF, S_NO_SUCH_FILE, S_PERMISSION_DENIED = 0, 1, 2, 3

CHUNK = 32 * 1024
PIPELINE = 16

STATUS_TEXT = {
    1: "Fine del file",
    2: "File o cartella inesistente",
    3: "Permesso negato",
    4: "Operazione fallita",
    5: "Messaggio non valido",
    6: "Nessuna connessione",
    7: "Connessione persa",
    8: "Operazione non supportata",
}


class Cancelled(Exception):
    """Raised when a transfer's `cancelled()` callback returns True."""


class SFTPError(Exception):
    def __init__(self, code: int, message: str = ""):
        self.code = code
        text = message or STATUS_TEXT.get(code, f"Errore SFTP {code}")
        super().__init__(text)


@dataclass
class Attrs:
    size: Optional[int] = None
    uid: Optional[int] = None
    gid: Optional[int] = None
    permissions: Optional[int] = None
    atime: Optional[int] = None
    mtime: Optional[int] = None

    @property
    def is_dir(self) -> bool:
        return self.permissions is not None and statmod.S_ISDIR(self.permissions)

    @property
    def is_link(self) -> bool:
        return self.permissions is not None and statmod.S_ISLNK(self.permissions)

    def pack(self) -> bytes:
        flags = 0
        body = b""
        if self.size is not None:
            flags |= A_SIZE
            body += struct.pack(">Q", self.size)
        if self.uid is not None and self.gid is not None:
            flags |= A_UIDGID
            body += struct.pack(">II", self.uid, self.gid)
        if self.permissions is not None:
            flags |= A_PERMISSIONS
            body += struct.pack(">I", self.permissions)
        if self.atime is not None and self.mtime is not None:
            flags |= A_ACMODTIME
            body += struct.pack(">II", self.atime, self.mtime)
        return struct.pack(">I", flags) + body


@dataclass
class Entry:
    name: str
    longname: str
    attrs: Attrs
    link_target_is_dir: bool = False
    owner: str = ""
    group: str = ""

    def to_json(self) -> dict:
        a = self.attrs
        return {
            "name": self.name,
            "size": a.size,
            "mtime": a.mtime,
            "perm": a.permissions,
            "mode": mode_string(a.permissions),
            "uid": a.uid,
            "gid": a.gid,
            "owner": self.owner or (str(a.uid) if a.uid is not None else ""),
            "group": self.group or (str(a.gid) if a.gid is not None else ""),
            "dir": a.is_dir or self.link_target_is_dir,
            "link": a.is_link,
        }


def mode_string(perm: Optional[int]) -> str:
    if perm is None:
        return ""
    return statmod.filemode(perm)


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def byte(self) -> int:
        v = self.data[self.pos]
        self.pos += 1
        return v

    def u32(self) -> int:
        (v,) = struct.unpack_from(">I", self.data, self.pos)
        self.pos += 4
        return v

    def u64(self) -> int:
        (v,) = struct.unpack_from(">Q", self.data, self.pos)
        self.pos += 8
        return v

    def string(self) -> bytes:
        n = self.u32()
        v = self.data[self.pos:self.pos + n]
        self.pos += n
        return v

    def text(self) -> str:
        return self.string().decode("utf-8", "replace")

    def attrs(self) -> Attrs:
        flags = self.u32()
        a = Attrs()
        if flags & A_SIZE:
            a.size = self.u64()
        if flags & A_UIDGID:
            a.uid, a.gid = self.u32(), self.u32()
        if flags & A_PERMISSIONS:
            a.permissions = self.u32()
        if flags & A_ACMODTIME:
            a.atime, a.mtime = self.u32(), self.u32()
        if flags & A_EXTENDED:
            for _ in range(self.u32()):
                self.string()
                self.string()
        return a


def _s(v) -> bytes:
    if isinstance(v, str):
        v = v.encode("utf-8")
    return struct.pack(">I", len(v)) + v


def parse_owner_group(longname: str) -> tuple:
    # "drwxr-xr-x    5 user     group        4096 Jan  1 12:00 name"
    parts = longname.split(None, 4)
    if len(parts) >= 4:
        return parts[2], parts[3]
    return "", ""


class SFTPClient:
    """One SFTP session over a subprocess. Requests are multiplexed by id."""

    def __init__(self, argv: List[str]):
        self.argv = argv
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._next_id = 1
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._stderr = b""
        self._write_lock = asyncio.Lock()
        self.extensions: Dict[str, str] = {}
        self.closed = False
        self.home = "/"

    # ---------- lifecycle ----------

    async def connect(self, timeout: float = 20.0) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # never grab the terminal / prompt for passwords
        )
        self._stderr_task = asyncio.ensure_future(self._drain_stderr())
        try:
            await self._send_raw(struct.pack(">BI", INIT, 3))
            ptype, payload = await asyncio.wait_for(self._read_packet(), timeout)
        except (asyncio.IncompleteReadError, ConnectionError, BrokenPipeError, asyncio.TimeoutError):
            await self._kill()
            try:
                await asyncio.wait_for(self._stderr_task, 1)
            except Exception:
                pass
            raise ConnectionError(self._stderr_text() or "Il server non ha avviato il sottosistema SFTP")
        if ptype != VERSION:
            await self._kill()
            raise ConnectionError("Risposta SFTP inattesa")
        r = _Reader(payload)
        r.u32()  # version
        while r.pos < len(r.data):
            name = r.text()
            self.extensions[name] = r.text()
        self._reader_task = asyncio.ensure_future(self._reader_loop())
        try:
            self.home = await self.realpath(".")
        except SFTPError:
            self.home = "/"

    async def close(self) -> None:
        await self._kill()

    async def _kill(self) -> None:
        self.closed = True
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                await asyncio.wait_for(self.proc.wait(), 2)
            except asyncio.TimeoutError:
                self.proc.kill()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("Connessione SFTP chiusa"))
        self._pending.clear()

    async def _drain_stderr(self) -> None:
        while True:
            chunk = await self.proc.stderr.read(4096)
            if not chunk:
                return
            self._stderr = (self._stderr + chunk)[-4096:]

    def _stderr_text(self) -> str:
        return self._stderr.decode("utf-8", "replace").strip()

    # ---------- transport ----------

    async def _send_raw(self, payload: bytes) -> None:
        async with self._write_lock:
            self.proc.stdin.write(struct.pack(">I", len(payload)) + payload)
            await self.proc.stdin.drain()

    async def _read_packet(self) -> tuple:
        hdr = await self.proc.stdout.readexactly(4)
        (length,) = struct.unpack(">I", hdr)
        body = await self.proc.stdout.readexactly(length)
        return body[0], body[1:]

    async def _reader_loop(self) -> None:
        try:
            while True:
                ptype, payload = await self._read_packet()
                (rid,) = struct.unpack_from(">I", payload, 0)
                fut = self._pending.pop(rid, None)
                if fut and not fut.done():
                    fut.set_result((ptype, payload[4:]))
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.closed = True
            err = ConnectionError(self._stderr_text() or "Connessione SFTP persa")
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(err)
            self._pending.clear()

    def _request_nowait(self, ptype: int, body: bytes) -> asyncio.Future:
        if self.closed:
            raise ConnectionError("Connessione SFTP chiusa")
        rid = self._next_id
        self._next_id = (self._next_id + 1) & 0xFFFFFFFF
        fut = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        asyncio.ensure_future(self._send_or_fail(struct.pack(">BI", ptype, rid) + body, rid))
        return fut

    async def _send_or_fail(self, payload: bytes, rid: int) -> None:
        try:
            await self._send_raw(payload)
        except Exception as e:  # pipe closed
            fut = self._pending.pop(rid, None)
            if fut and not fut.done():
                fut.set_exception(ConnectionError(str(e)))

    async def _request(self, ptype: int, body: bytes) -> tuple:
        return await self._request_nowait(ptype, body)

    @staticmethod
    def _check_status(ptype: int, payload: bytes, allow_eof: bool = False) -> int:
        if ptype != STATUS:
            raise SFTPError(4, "Risposta inattesa")
        r = _Reader(payload)
        code = r.u32()
        msg = r.text() if r.pos < len(r.data) else ""
        if code == S_OK or (allow_eof and code == S_EOF):
            return code
        raise SFTPError(code, STATUS_TEXT.get(code) or msg)

    async def _expect(self, want: int, ptype: int, payload: bytes):
        if ptype == want:
            return _Reader(payload)
        self._check_status(ptype, payload)
        raise SFTPError(4, "Risposta inattesa")

    # ---------- operations ----------

    async def realpath(self, path: str) -> str:
        r = await self._expect(NAME, *await self._request(REALPATH, _s(path)))
        r.u32()
        return r.text()

    async def stat(self, path: str) -> Attrs:
        r = await self._expect(ATTRS, *await self._request(STAT, _s(path)))
        return r.attrs()

    async def lstat(self, path: str) -> Attrs:
        r = await self._expect(ATTRS, *await self._request(LSTAT, _s(path)))
        return r.attrs()

    async def exists(self, path: str) -> bool:
        try:
            await self.lstat(path)
            return True
        except SFTPError as e:
            if e.code == S_NO_SUCH_FILE:
                return False
            raise

    async def listdir(self, path: str) -> List[Entry]:
        r = await self._expect(HANDLE, *await self._request(OPENDIR, _s(path)))
        handle = r.string()
        entries: List[Entry] = []
        try:
            while True:
                ptype, payload = await self._request(READDIR, _s(handle))
                if ptype == STATUS:
                    self._check_status(ptype, payload, allow_eof=True)
                    break
                r = await self._expect(NAME, ptype, payload)
                for _ in range(r.u32()):
                    name, longname, attrs = r.text(), r.text(), r.attrs()
                    if name in (".", ".."):
                        continue
                    owner, group = parse_owner_group(longname)
                    entries.append(Entry(name, longname, attrs, owner=owner, group=group))
        finally:
            await self._close_handle(handle)
        # Resolve symlinks to directories so they can be opened
        links = [e for e in entries if e.attrs.is_link]
        if links:
            futs = [self._request_nowait(STAT, _s(join(path, e.name))) for e in links]
            for e, fut in zip(links, futs):
                try:
                    ptype, payload = await fut
                    if ptype == ATTRS:
                        e.link_target_is_dir = _Reader(payload).attrs().is_dir
                except (SFTPError, ConnectionError):
                    pass
        return entries

    async def _close_handle(self, handle: bytes) -> None:
        try:
            self._check_status(*await self._request(CLOSE, _s(handle)))
        except (SFTPError, ConnectionError):
            pass

    async def mkdir(self, path: str, mode: int = 0o755) -> None:
        self._check_status(*await self._request(MKDIR, _s(path) + Attrs(permissions=mode).pack()))

    async def rmdir(self, path: str) -> None:
        self._check_status(*await self._request(RMDIR, _s(path)))

    async def remove(self, path: str) -> None:
        self._check_status(*await self._request(REMOVE, _s(path)))

    async def rename(self, old: str, new: str) -> None:
        if "posix-rename@openssh.com" in self.extensions:
            body = _s("posix-rename@openssh.com") + _s(old) + _s(new)
            self._check_status(*await self._request(EXTENDED, body))
        else:
            self._check_status(*await self._request(RENAME, _s(old) + _s(new)))

    async def chmod(self, path: str, mode: int) -> None:
        self._check_status(*await self._request(SETSTAT, _s(path) + Attrs(permissions=mode).pack()))

    async def setmtime(self, path: str, atime: int, mtime: int) -> None:
        self._check_status(*await self._request(SETSTAT, _s(path) + Attrs(atime=atime, mtime=mtime).pack()))

    async def touch(self, path: str) -> None:
        r = await self._expect(HANDLE, *await self._request(
            OPEN, _s(path) + struct.pack(">I", F_WRITE | F_CREAT | F_EXCL) + Attrs(permissions=0o644).pack()))
        await self._close_handle(r.string())

    async def rmtree(self, path: str) -> None:
        a = await self.lstat(path)
        if not a.is_dir:
            await self.remove(path)
            return
        for e in await self.listdir(path):
            await self.rmtree(join(path, e.name))
        await self.rmdir(path)

    async def walk_size(self, path: str) -> int:
        a = await self.lstat(path)
        if not a.is_dir:
            return a.size or 0
        total = 0
        for e in await self.listdir(path):
            if e.attrs.is_dir:
                total += await self.walk_size(join(path, e.name))
            elif not e.attrs.is_link:
                total += e.attrs.size or 0
        return total

    # ---------- transfers ----------

    async def download(self, remote: str, local: str,
                       progress: Optional[Callable[[int], None]] = None,
                       cancelled: Callable[[], bool] = lambda: False) -> None:
        """Pipelined download of one file (up to PIPELINE reads in flight)."""
        attrs = await self.stat(remote)
        r = await self._expect(HANDLE, *await self._request(
            OPEN, _s(remote) + struct.pack(">I", F_READ) + Attrs().pack()))
        handle = r.string()
        tmp = local + ".part"
        queue: List[tuple] = []

        def issue(off: int, length: int, front: bool = False):
            item = (off, length, self._request_nowait(READ, _s(handle) + struct.pack(">QI", off, length)))
            queue.insert(0, item) if front else queue.append(item)

        try:
            with open(tmp, "wb") as f:
                limit = attrs.size  # None -> read until EOF
                offset = 0
                eof = False
                while True:
                    while not eof and len(queue) < PIPELINE and (limit is None or offset < limit):
                        issue(offset, CHUNK)
                        offset += CHUNK
                    if not queue:
                        break
                    off, length, fut = queue.pop(0)
                    ptype, payload = await fut
                    if ptype == STATUS:
                        self._check_status(ptype, payload, allow_eof=True)
                        eof = True
                        continue
                    data = _Reader(payload).string()
                    f.seek(off)
                    f.write(data)
                    if progress:
                        progress(len(data))
                    if 0 < len(data) < length:
                        issue(off + len(data), length - len(data), front=True)
                    if cancelled():
                        raise Cancelled()
            if attrs.mtime:
                os.utime(tmp, (attrs.atime or attrs.mtime, attrs.mtime))
            os.replace(tmp, local)
        except BaseException:
            for _, _, fut in queue:
                fut.cancel()
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        finally:
            await self._close_handle(handle)

    async def upload_stream(self, remote: str,
                            chunks: Callable[[], Awaitable[bytes]],
                            progress: Optional[Callable[[int], None]] = None,
                            cancelled: Callable[[], bool] = lambda: False,
                            mode: int = 0o644) -> None:
        """Pipelined upload from an async chunk source (returns b'' at the end)."""
        r = await self._expect(HANDLE, *await self._request(
            OPEN, _s(remote) + struct.pack(">I", F_WRITE | F_CREAT | F_TRUNC) + Attrs(permissions=mode).pack()))
        handle = r.string()
        inflight: List[tuple] = []

        async def wait_one():
            n, fut = inflight.pop(0)
            self._check_status(*await fut)
            if progress:
                progress(n)

        try:
            offset = 0
            buf = b""
            while True:
                data = await chunks()
                buf += data
                while len(buf) >= CHUNK or (not data and buf):
                    piece, buf = buf[:CHUNK], buf[CHUNK:]
                    if len(inflight) >= PIPELINE:
                        await wait_one()
                    inflight.append((len(piece), self._request_nowait(
                        WRITE, _s(handle) + struct.pack(">Q", offset) + _s(piece))))
                    offset += len(piece)
                if cancelled():
                    raise Cancelled()
                if not data:
                    break
            while inflight:
                await wait_one()
        except BaseException:
            for _, fut in inflight:
                fut.cancel()
            raise
        finally:
            await self._close_handle(handle)

    async def upload_file(self, local: str, remote: str, progress=None, cancelled=lambda: False) -> None:
        st = os.stat(local)
        with open(local, "rb") as f:
            async def chunks():
                return f.read(CHUNK * 4)
            await self.upload_stream(remote, chunks, progress, cancelled, mode=st.st_mode & 0o777)
        try:
            await self.setmtime(remote, int(st.st_atime), int(st.st_mtime))
        except SFTPError:
            pass


def join(base: str, name: str) -> str:
    if not base or base == "/":
        return "/" + name.lstrip("/")
    return base.rstrip("/") + "/" + name


def parent(path: str) -> str:
    path = path.rstrip("/")
    if not path:
        return "/"
    p = path.rsplit("/", 1)[0]
    return p or "/"
