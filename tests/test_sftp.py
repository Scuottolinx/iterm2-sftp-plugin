"""Exercise the SFTP client against the local OpenSSH sftp-server (no network needed).

    python3 tests/test_sftp.py
"""
import asyncio
import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from sftp import SFTPClient, SFTPError, join  # noqa: E402

SERVER = "/usr/libexec/sftp-server"


def md5(p):
    return hashlib.md5(open(p, "rb").read()).hexdigest()


async def main():
    work = tempfile.mkdtemp(prefix="sftptest-")
    c = SFTPClient([SERVER])
    await c.connect()
    print("home:", c.home, "ext:", sorted(c.extensions))

    remote = join(work, "remote")
    await c.mkdir(remote)
    await c.mkdir(join(remote, "sub"))
    await c.touch(join(remote, "empty.txt"))
    os.symlink(join(remote, "sub"), join(remote, "link-to-sub"))

    # sizes around chunk boundaries + a big one
    local_dir = join(work, "local")
    os.mkdir(local_dir)
    for size in (0, 1, 32767, 32768, 32769, 5 * 1024 * 1024 + 123):
        src = join(local_dir, f"f{size}")
        with open(src, "wb") as f:
            f.write(os.urandom(size))
        seen = []
        await c.upload_file(src, join(remote, f"f{size}"), progress=seen.append)
        assert sum(seen) == size, (size, sum(seen))
        dst = join(work, f"back{size}")
        got = []
        await c.download(join(remote, f"f{size}"), dst, progress=got.append)
        assert sum(got) == size and md5(src) == md5(dst), size
        assert int(os.stat(dst).st_mtime) == int(os.stat(src).st_mtime)
    print("upload/download ok")

    entries = {e.name: e.to_json() for e in await c.listdir(remote)}
    assert entries["sub"]["dir"] and entries["link-to-sub"]["dir"] and entries["link-to-sub"]["link"]
    assert entries["f32768"]["size"] == 32768 and entries["f32768"]["owner"]
    print("listdir ok:", sorted(entries))

    await c.rename(join(remote, "empty.txt"), join(remote, "renamed.txt"))
    await c.chmod(join(remote, "renamed.txt"), 0o600)
    assert (await c.stat(join(remote, "renamed.txt"))).permissions & 0o777 == 0o600
    try:
        await c.stat(join(remote, "nope"))
        raise AssertionError("expected error")
    except SFTPError as e:
        assert e.code == 2
    print("walk_size:", await c.walk_size(remote))

    # concurrent requests on the same channel
    res = await asyncio.gather(*[c.listdir(remote) for _ in range(20)])
    assert all(len(r) == len(res[0]) for r in res)

    await c.rmtree(remote)
    assert not await c.exists(remote)
    await c.close()
    print("ALL OK")


asyncio.run(main())
