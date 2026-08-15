"""Shared XML-RPC client for freecad.py - not itself an agent-callable tool,
same "helper module, not a tool module" shape as tools/_fs.py and
tools/_outlook.py. Talks to scripts/freecad_rpc_bootstrap.py - see that
file's docstring for the transport decision (a socket/RPC bridge started
from inside FreeCAD's own already-running process, not a console command
file or an external `import FreeCAD` process - only the RPC bridge lets an
already-open GUI instance execute code and show the result live, which is
the actual point of this tool).

is_available() calls the bridge's ping() method and checks the real
response, not just that something answers on the port at all - the same
"genuinely reachable, not just importable" discipline A24's OCR fix and
A25's Playwright CDP check both already established (a naive "is the port
open" check can't distinguish the real bridge from an unrelated service
that happens to be listening there, or a FreeCAD that's running but never
had the bootstrap script pasted in - open GUI, closed door).

Uses socket.setdefaulttimeout() around the call, not
asyncio.wait_for(asyncio.to_thread(...)) alone - xmlrpc.client's
ServerProxy has no timeout parameter of its own, and wrapping a blocking
call in wait_for only stops AWAITING it on cancellation, it doesn't
actually interrupt the underlying blocked socket call, which would leak a
hung thread on a wedged connection. Setting the real socket-level timeout
bounds the actual connect/read, not just this coroutine's patience for it.
"""

import asyncio
import socket
import tomllib
import xmlrpc.client
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "cortana.toml"

_PING_TIMEOUT_S = 1.5


def _config() -> dict:
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f).get("tools", {}).get("freecad", {})


def endpoint() -> str:
    cfg = _config()
    host = cfg.get("rpc_host", "localhost")
    port = cfg.get("rpc_port", 9875)
    return f"http://{host}:{port}"


def _ping() -> bool:
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(_PING_TIMEOUT_S)
    try:
        proxy = xmlrpc.client.ServerProxy(endpoint(), allow_none=True)
        return proxy.ping() == "pong"
    except Exception:
        return False
    finally:
        socket.setdefaulttimeout(old_timeout)


async def is_available() -> bool:
    return await asyncio.to_thread(_ping)


def run_code(code: str) -> dict:
    """Synchronous - only ever called from execute() paths already gated
    behind is_available() returning True, same pattern as
    tools/_outlook.py's get_namespace(). Blocks until
    scripts/freecad_rpc_bootstrap.py's run_code() actually finishes on
    FreeCAD's own main thread (or its own internal timeout fires) - a real
    round trip, not fire-and-forget, so the caller genuinely knows the
    outcome before reporting anything. Returns {"ok": bool, "stdout": str,
    "error": str} - the exact shape the bootstrap script's run_code()
    returns, passed straight through, not reinterpreted here."""
    proxy = xmlrpc.client.ServerProxy(endpoint(), allow_none=True)
    return proxy.run_code(code)
