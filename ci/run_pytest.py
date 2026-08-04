"""Run pytest in CI while making pytest's return code explicit.

Windows-specific hardening
--------------------------
On GitHub's Windows runner a stray console control event (CTRL_C / CTRL_BREAK)
is occasionally delivered to the whole console process group mid-run. When that
happens the *same* event is seen by two processes at once:

  * python — raises KeyboardInterrupt, which pytest turns into
    ``ExitCode.INTERRUPTED`` (2) even though every test already passed; and
  * cmd.exe — prints the blocking ``Terminate batch job (Y/N)?`` prompt, which
    has no stdin in CI and wedges the step until the runner force-kills it.

This is environmental, not a test bug (every test that spawns a real child or
sends a real signal is Unix-only and skipped on Windows). A previous fix removed
one local trigger, but the class of event recurs, so we defend at the wrapper:
install a console control handler that *consumes* CTRL_C / CTRL_BREAK (returns
TRUE) so the event propagates to neither pytest nor cmd, and ignore the matching
Python signals as a belt-and-suspenders fallback. Genuine test failures still
surface through pytest's normal non-zero exit codes.
"""

from __future__ import annotations

import faulthandler
import os
import signal
import sys


def _silence_console_interrupts() -> None:
    """Stop a spurious CTRL_C/CTRL_BREAK from interrupting the test run.

    No-op on non-Windows. Best-effort: any failure here must never abort CI.
    """
    if os.name != "nt":
        # SIGINT is the only portable lever off Windows; harmless to ignore for
        # the duration of a non-interactive CI run.
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        except (ValueError, OSError):
            pass
        return

    # Ignore the Python-level signals first, in case the console handler below
    # cannot be installed for some reason.
    for signame in ("SIGINT", "SIGBREAK"):
        sig = getattr(signal, signame, None)
        if sig is not None:
            try:
                signal.signal(sig, signal.SIG_IGN)
            except (ValueError, OSError):
                pass

    # Install a native console control handler that swallows CTRL_C (0) and
    # CTRL_BREAK (1). Returning TRUE marks the event handled so it does not reach
    # the default handler (which would terminate us) or cmd.exe's prompt.
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        CTRL_C_EVENT = 0
        CTRL_BREAK_EVENT = 1

        def _handler(ctrl_type: int) -> bool:
            # Swallow Ctrl+C / Ctrl+Break; let everything else fall through to
            # the default handler so shutdown/close still works normally.
            return ctrl_type in (CTRL_C_EVENT, CTRL_BREAK_EVENT)

        # Keep a reference so the callback is not garbage-collected.
        global _CONSOLE_HANDLER
        _CONSOLE_HANDLER = handler_type(_handler)
        kernel32.SetConsoleCtrlHandler(_CONSOLE_HANDLER, True)
    except Exception:
        # Never let hardening break the run.
        pass


_CONSOLE_HANDLER = None  # holds the live ctypes callback on Windows


def main() -> int:
    faulthandler.enable()
    _silence_console_interrupts()
    import pytest

    code = pytest.main()
    exit_code = int(code)
    print(f"[ci] pytest.main returned {exit_code} ({code!r})", flush=True)
    return exit_code


if __name__ == "__main__":
    result = main()
    sys.stdout.flush()
    sys.stderr.flush()
    if os.name == "nt":
        # GitHub's Windows runner can report exit 1 during interpreter shutdown
        # even after pytest.main() returns OK. Preserve pytest's actual result.
        os._exit(result)
    raise SystemExit(result)
