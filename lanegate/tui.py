"""Launcher for the Go TUI.

Python remains the product entry point and control plane: it owns repository
discovery, API startup, local port selection, and user-facing launch errors.
The Go TUI renders and navigates board, ticket detail, and blocked queue screens
over the Python-owned JSON API contracts.
"""

from __future__ import annotations

import http.client
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

# How long to wait for the background `lanegate api` subprocess to start
# accepting connections before giving up and reporting a launch error.
_API_READY_TIMEOUT_S = 10.0
_API_READY_POLL_INTERVAL_S = 0.05


class TuiLaunchError(RuntimeError):
    def __init__(self, message: str, exit_code: int = 2) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def cmd_tui(
    repo_root: Path,
    *,
    fixture: Path | None = None,
    fixture_dir: Path | None = None,
    api_url: str | None = None,
    no_api_start: bool = False,
    port: int | None = None,
) -> None:
    """Launch the Go TUI against fixtures or a live Python API."""

    api_proc: subprocess.Popen | None = None
    try:
        argv, cwd = _go_tui_command()

        # Handle fixture mode
        if fixture_dir is not None:
            argv.extend(["--fixture-dir", str(_resolve_path(repo_root, fixture_dir))])
        elif fixture is not None:
            argv.extend(["--fixture", str(_resolve_path(repo_root, fixture))])
        elif api_url is not None:
            _validate_loopback_api_url(api_url)
            argv.extend(["--api-url", api_url])
        elif no_api_start:
            raise TuiLaunchError("--no-api-start requires --api-url, --fixture, or --fixture-dir")
        else:
            # Default: start our own API on a loopback port in the background.
            selected_port = _select_loopback_port(port)
            api_url = f"http://127.0.0.1:{selected_port}"
            print(
                f"lanegate tui is starting the API on 127.0.0.1:{selected_port}",
                file=sys.stderr,
            )
            api_proc = subprocess.Popen(
                [sys.executable, "-m", "lanegate.cli", "api", "--port", str(selected_port)],
                cwd=str(repo_root),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _wait_for_api_ready(api_proc, selected_port)
            argv.extend(["--api-url", api_url])

        subprocess.run(argv, cwd=cwd, check=True)
    except TuiLaunchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(exc.exit_code)
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: Go TUI exited with status {exc.returncode}", file=sys.stderr)
        sys.exit(exc.returncode)
    except FileNotFoundError as exc:
        print(f"ERROR: failed to start Go TUI: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        if api_proc is not None and api_proc.poll() is None:
            api_proc.terminate()
            try:
                api_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                api_proc.kill()
                api_proc.wait()


def _wait_for_api_ready(api_proc: subprocess.Popen, port: int) -> None:
    """Block until the background API subprocess answers on `port`, it exits
    early, or `_API_READY_TIMEOUT_S` elapses — whichever comes first."""
    deadline = time.monotonic() + _API_READY_TIMEOUT_S
    while time.monotonic() < deadline:
        if api_proc.poll() is not None:
            raise TuiLaunchError(
                f"the background API subprocess exited (code {api_proc.returncode}) before it "
                "became ready",
                exit_code=1,
            )
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        try:
            conn.request("GET", "/api/status")
            conn.getresponse()
            return
        except OSError:
            time.sleep(_API_READY_POLL_INTERVAL_S)
        finally:
            conn.close()

    api_proc.terminate()
    raise TuiLaunchError(
        f"timed out waiting for the background API to become ready on 127.0.0.1:{port}",
        exit_code=1,
    )




def _resolve_path(repo_root: Path, path: Path) -> Path:
    if path.is_absolute():
        return path
    cwd_path = (Path.cwd() / path).resolve()
    if cwd_path.exists():
        return cwd_path
    return (repo_root / path).resolve()


def _go_tui_command() -> tuple[list[str], Path | None]:
    # Try environment variable override first
    env_bin = os.environ.get("LANEGATE_TUI_BIN")
    if env_bin:
        binary = Path(env_bin).expanduser()
        if not binary.is_file():
            raise TuiLaunchError(f"LANEGATE_TUI_BIN does not point to a file: {binary}")
        return [str(binary)], None

    # Try to find binary on PATH
    path_binary = shutil.which("lanegate-tui")
    if path_binary:
        return [path_binary], None

    # Fall back to dev installation: use go run from tui/ module
    source_dir = Path(__file__).resolve().parent.parent / "tui"
    if (source_dir / "cmd" / "lanegate-tui" / "main.go").is_file():
        go = shutil.which("go")
        if go is None:
            raise TuiLaunchError(
                "Go TUI source is bundled, but the go toolchain is not on PATH; "
                "set LANEGATE_TUI_BIN to a built lanegate-tui binary",
                exit_code=1,
            )
        return [go, "run", "./cmd/lanegate-tui"], source_dir

    raise TuiLaunchError(
        "Go TUI binary or source not found; set LANEGATE_TUI_BIN or ensure tui/ module is present",
        exit_code=1,
    )


def _validate_loopback_api_url(api_url: str) -> None:
    parsed = urlparse(api_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise TuiLaunchError(f"--api-url must be an http(s) URL: {api_url}")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise TuiLaunchError("--api-url must target a loopback host for this local spike")


def _select_loopback_port(preferred: int | None) -> int:
    if preferred is not None and not (1 <= preferred <= 65535):
        raise TuiLaunchError(f"--port must be between 1 and 65535: {preferred}")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", preferred or 0))
        except OSError as exc:
            raise TuiLaunchError(f"could not reserve 127.0.0.1:{preferred}: {exc}") from exc
        return int(sock.getsockname()[1])
