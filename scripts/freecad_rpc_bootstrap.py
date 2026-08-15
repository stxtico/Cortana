"""cortana's FreeCAD RPC bridge (PROMPTS.md A26) - run this ONCE inside an
already-open FreeCAD's own Python console (View > Panels > Python console),
not from this project's own Python environment. Paste the whole file's
contents in, or File > Open Macro... and run it as a macro (View > Macros,
or the Macro toolbar). It has to run INSIDE FreeCAD's own embedded
interpreter - that's the entire point: this is what lets an external
process (tools/_freecad.py, in this repo's own venv) send Python that
executes in the SAME live process as the GUI you're looking at, so a loaded
part actually appears in the open 3D view instead of in some disconnected
headless session.

Transport decision, stated plainly (PROMPTS.md A26 asked for this
explicitly): FreeCAD exposes three real paths for external control.
1. A console command FILE - FreeCAD doesn't watch a directory and
   auto-execute new macros dropped into it; nothing about "write a file"
   makes an ALREADY-OPEN GUI instance run it without a second, separate
   trigger (a human clicking Execute, or driving that menu via UIA - which
   reopens exactly the GUI-automation fragility PLAN.md's "GUI question"
   section built this whole feature to avoid).
2. `import FreeCAD` from an EXTERNAL process (this project's own venv, the
   way scripts/_cad_common.py's headless generation already could in
   principle) - creates its own, separate, disconnected in-memory FreeCAD
   session. Never touches whatever the user actually has open. Wrong tool
   for "renders live where I can rotate it."
3. A socket/RPC server, STARTED FROM INSIDE the already-running GUI
   process (this file). The only one of the three that actually satisfies
   "an already-running instance executes code and shows the result live" -
   which is the entire stated point of this feature, not an implementation
   detail to optimize around.

Built here rather than depending on a third-party FreeCAD RPC addon:
unverifiable without installing FreeCAD to check, unclear current
maintenance status, and this project's own established pattern (A23's
media_control writing its own temp .ps1 script rather than depending on
someone else's automation bridge) is to own small, fully-understood
transport code end to end. This file is ~60 lines of stdlib
(xmlrpc.server + threading + Qt's own QTimer, all already present in
FreeCAD's bundled Python - no pip install needed inside FreeCAD itself).

What this can't do, stated plainly:
- It does NOT survive closing FreeCAD - re-run this after every fresh
  launch. There's no persistence; it's a live bridge into a live process,
  not a service.
- It listens on localhost ONLY (never 0.0.0.0) - nothing outside this
  machine can reach it. Still: arbitrary Python execution inside a GUI
  process you're looking at is real capability, worth knowing is active.
- FreeCAD's GUI (Qt) is not thread-safe from arbitrary background
  threads - calling FreeCADGui/3D-view functions directly from the
  XML-RPC server's own listener thread risks crashing FreeCAD. This
  script marshals every run_code() call onto FreeCAD's own main/GUI
  thread via a QTimer polling loop (50ms) rather than executing inline
  on the RPC thread - a real fix for a real constraint, not decoration.
- Genuinely untested against a live FreeCAD instance as of this build
  (FreeCAD isn't installed on the machine this was written on) - the
  underlying XML-RPC transport and the calling client
  (tools/_freecad.py) ARE tested, against a stand-in server. This file's
  own FreeCAD-specific calls (Part.insert, FreeCADGui.activeView(), the
  exact QTimer/PySide plumbing) are a best-effort first draft, not a
  verified one. Report back what breaks.
"""

import io
import queue
import threading
import traceback
from xmlrpc.server import SimpleXMLRPCServer

# PySide's module name changed across FreeCAD major versions (PySide2 for
# FreeCAD 0.20/0.21, PySide6 for FreeCAD 1.0+) - try newest first rather
# than hardcoding one and breaking on the other.
try:
    from PySide6 import QtCore
except ImportError:
    try:
        from PySide2 import QtCore
    except ImportError:
        from PySide import QtCore

HOST = "localhost"
PORT = 9875  # must match [tools.freecad].rpc_port in config/cortana.toml

_task_queue = queue.Queue()
_namespace = {"FreeCAD": FreeCAD, "App": FreeCAD, "FreeCADGui": FreeCADGui, "Gui": FreeCADGui, "Part": __import__("Part")}


class _MainThreadExecutor(QtCore.QObject):
    """Polls _task_queue on a QTimer tick - QTimer callbacks run on the
    thread that created the QObject, which is the GUI/main thread when this
    is instantiated from FreeCAD's own console. That's what actually makes
    run_code() below safe to call from the XML-RPC server's separate
    listener thread."""

    def __init__(self):
        super().__init__()
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self._poll)
        self.timer.start(50)

    def _poll(self):
        try:
            code, result_holder, done_event = _task_queue.get_nowait()
        except queue.Empty:
            return
        buf = io.StringIO()
        try:
            import contextlib
            with contextlib.redirect_stdout(buf):
                exec(code, _namespace)
            result_holder["ok"] = True
            result_holder["stdout"] = buf.getvalue()
            result_holder["error"] = ""
        except Exception:
            result_holder["ok"] = False
            result_holder["stdout"] = buf.getvalue()
            result_holder["error"] = traceback.format_exc()
        done_event.set()


_executor = _MainThreadExecutor()


def ping():
    return "pong"


def run_code(code):
    """Blocks the RPC-handling thread until the main-thread executor above
    actually runs the code (or this timeout fires) - a real synchronous
    round trip, not fire-and-forget, so the caller genuinely knows whether
    it worked before reporting anything."""
    result_holder = {}
    done_event = threading.Event()
    _task_queue.put((code, result_holder, done_event))
    if not done_event.wait(timeout=30):
        return {"ok": False, "stdout": "", "error": "timed out waiting for FreeCAD's main thread to run this"}
    return result_holder


_server = SimpleXMLRPCServer((HOST, PORT), allow_none=True, logRequests=False)
_server.register_function(ping, "ping")
_server.register_function(run_code, "run_code")
_server_thread = threading.Thread(target=_server.serve_forever, daemon=True)
_server_thread.start()
print(f"cortana FreeCAD RPC bridge listening on {HOST}:{PORT} - leave this FreeCAD instance open.")
