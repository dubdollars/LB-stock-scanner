"""
Platform helpers — sleep prevention for long-running scans.

When the laptop sleeps mid-scan, Python pauses and yfinance loses its network
connections — the scan effectively stops and the user has to restart from
scratch. Windows offers SetThreadExecutionState to prevent this, but three
subtle things need to be right:

  1. Include ES_DISPLAY_REQUIRED, not just ES_SYSTEM_REQUIRED. On modern
     Windows 10/11 laptops with Modern Standby ("S0 Low Power Idle"),
     letting the DISPLAY sleep can cause the system to enter a low-power
     state that pauses Python execution — even though we technically told
     Windows "don't sleep the system." Requiring the display to stay on
     keeps the machine in the S0 active state.

  2. Re-assert the flag periodically. Some Windows configurations reset
     the "keep awake" hint after the process/thread goes idle from
     Windows' point of view. A background daemon that reasserts every
     30 seconds is belt-and-suspenders insurance.

  3. Set the flag from the SAME thread that's doing the work, OR set it
     with ES_CONTINUOUS which makes it process-wide. We use ES_CONTINUOUS
     so Streamlit's threading model doesn't matter.

User-reported bug 2026-07-22: scans still stalled overnight even with the
original single-shot ES_SYSTEM_REQUIRED. Added ES_DISPLAY_REQUIRED, the
background reasserter, and ES_AWAYMODE_REQUIRED (which explicitly opts
out of Modern Standby on supported hardware).

No-op on non-Windows platforms — the wrappers degrade gracefully.
"""
from __future__ import annotations

import sys
import threading
import time

# Module-level state so we can start/stop the background reasserter thread
# across function calls.
_reasserter_thread: threading.Thread | None = None
_reasserter_stop = threading.Event()


# Windows SetThreadExecutionState flags. Documented at
# https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate
_ES_CONTINUOUS       = 0x80000000  # keep the flag active until cleared
_ES_SYSTEM_REQUIRED  = 0x00000001  # keep system awake
_ES_DISPLAY_REQUIRED = 0x00000002  # keep display awake (prevents Modern Standby)
_ES_AWAYMODE_REQUIRED = 0x00000040  # media-center-style away-mode (some laptops)


def _set_awake_flag() -> None:
    """One-shot: tell Windows to stay awake. Called by keep_awake AND the
    reasserter thread."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        # Combine ALL relevant flags. If ES_AWAYMODE_REQUIRED isn't supported
        # on the current hardware, Windows silently ignores it (per the docs).
        ctypes.windll.kernel32.SetThreadExecutionState(
            _ES_CONTINUOUS
            | _ES_SYSTEM_REQUIRED
            | _ES_DISPLAY_REQUIRED
            | _ES_AWAYMODE_REQUIRED
        )
    except Exception:
        pass  # best-effort — fall back to OS defaults


def _reasserter_loop() -> None:
    """Background daemon that re-calls SetThreadExecutionState every 30 seconds.

    Rationale: some Windows configurations "forget" the keep-awake hint when
    the process goes idle from Windows' point of view (e.g., yfinance is
    waiting on a socket). Periodically reasserting the flag guarantees the
    hint stays fresh. Costs almost nothing (~one syscall per 30s).
    """
    while not _reasserter_stop.is_set():
        _set_awake_flag()
        # Wait 30s but break early if stop was requested
        _reasserter_stop.wait(30)


def keep_awake() -> None:
    """Tell the OS not to sleep while a scan is running. Windows-only; no-op elsewhere.

    Sets the keep-awake flag AND starts a background daemon that reasserts it
    every 30 seconds. Idempotent — calling multiple times reuses the same
    daemon. Always pair with allow_sleep() in a try/finally so normal power
    management resumes when the scan finishes.
    """
    global _reasserter_thread

    if sys.platform != "win32":
        return

    # Set the flag immediately so we're protected from the first second.
    _set_awake_flag()

    # Start the background reasserter if it's not already running.
    if _reasserter_thread is None or not _reasserter_thread.is_alive():
        _reasserter_stop.clear()
        _reasserter_thread = threading.Thread(
            target=_reasserter_loop,
            daemon=True,   # dies with the process; never blocks shutdown
            name="LB-keep-awake",
        )
        _reasserter_thread.start()


def allow_sleep() -> None:
    """Restore normal power management after the scan finishes.

    Stops the background reasserter and clears the keep-awake flag so the
    OS can sleep normally again.
    """
    global _reasserter_thread

    if sys.platform != "win32":
        return

    # Signal the reasserter to stop and wait for it to exit (short timeout).
    _reasserter_stop.set()
    if _reasserter_thread is not None:
        _reasserter_thread.join(timeout=1.0)
        _reasserter_thread = None

    try:
        import ctypes
        # Clear all flags — just ES_CONTINUOUS alone releases the hints.
        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
    except Exception:
        pass
