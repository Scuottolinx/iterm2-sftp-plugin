"""python3 tests/test_reconnect.py  (needs psutil, e.g. the plugin venv)"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from reconnect import connection_dropped  # noqa: E402

DROPS = [
    ["root@srv:~# ", "client_loop: send disconnect: Broken pipe", "user@mac ~ % "],
    ["Connection to 10.0.0.1 closed by remote host.", "Connection to 10.0.0.1 closed.", "% "],
    ["Timeout, server 10.0.0.1 not responding.", "% "],
    ["ssh: connect to host 10.0.0.1 port 22: Connection refused", "% "],
    ["ssh: connect to host srv port 22: Operation timed out", "% "],
    ["packet_write_wait: Connection to 1.2.3.4 port 22: Broken pipe", "% "],
    ["Read from remote host srv: Connection reset by peer", "Connection to srv closed.", "% "],
]
CLEAN = [
    ["root@srv:~# exit", "logout", "Connection to srv closed.", "user@mac ~ % "],
    ["[root@srv ~]# logout", "Connection to 203.0.113.10 closed.", "% "],
    ["Permission denied (publickey).", "% "],
    ["", "% "],
    ["srv% exit", "Connection to srv closed.", "% "],
    # an old drop far above a clean exit must not count
    ["Broken pipe", "a", "b", "c", "d", "e", "f", "g", "h", "logout", "Connection to x closed.", "% "],
]
for lines in DROPS:
    assert connection_dropped(lines), lines
for lines in CLEAN:
    assert not connection_dropped(lines), lines
print("ALL OK", len(DROPS) + len(CLEAN))
