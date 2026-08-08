"""Bridging blocking work onto the GTK main loop.

Every SMB call blocks, and GTK is single-threaded: touching a widget from a
worker thread corrupts the toolkit's state. So work runs on a plain daemon
thread and results come back through `GLib.idle_add`, which queues the
callback for the main loop.

This is the only place threading is set up for the GUI.
"""

import threading

from gi.repository import GLib


def run_async(work, on_done=None, on_error=None):
    """Run `work()` off the main loop, delivering the result back on it.

    `on_done(result)` and `on_error(exception)` are invoked on the GTK main
    loop, so they may touch widgets freely. Returns the started thread.
    """

    def target():
        try:
            result = work()
        except Exception as exc:                      # noqa: BLE001 — reported to the UI
            if on_error is not None:
                GLib.idle_add(on_error, exc)
            return
        if on_done is not None:
            GLib.idle_add(on_done, result)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def on_main(func, *args):
    """Schedule `func(*args)` on the GTK main loop from any thread."""
    # Returning False stops idle_add from repeating the callback.
    GLib.idle_add(lambda: (func(*args), False)[1])
