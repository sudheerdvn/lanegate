"""
deploy.py — secure hook execution for lanegate promotion/deployment pipeline.

All hooks are invoked as explicit argv lists via subprocess.run(shell=False).
String hooks are rejected at config load time (see config.validate_hook).

An allowlist of permitted hook executables is enforced. The default allowlist
covers common deployment tools; projects may extend it via .lanegate.yml under
``hook_allowlist``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from lanegate.config import ConfigError

# ---------------------------------------------------------------------------
# Default executable allowlist
# ---------------------------------------------------------------------------

#: Executables that are permitted as the first element of a hook argv list.
#: Paths (relative or absolute) to scripts with a recognised extension are
#: also allowed without being named here — see ``_is_allowed``.
_DEFAULT_ALLOWLIST: frozenset[str] = frozenset(
    {
        # shell-script wrappers (invoked directly, not via /bin/sh)
        "bash",
        "sh",
        "zsh",
        # Python
        "python",
        "python3",
        # Node / JS tooling
        "node",
        "npm",
        "npx",
        "yarn",
        "pnpm",
        # Build / task runners
        "make",
        "just",
        # Common CI/CD tools
        "docker",
        "kubectl",
        "helm",
        # Git
        "git",
        # Misc utilities often used in post-deploy hooks
        "curl",
        "wget",
        "rsync",
        "scp",
        "ssh",
        # Notification helpers
        "slack",
        "notify-send",
    }
)

#: Script file extensions that are unconditionally allowed as the first argv
#: element (e.g. ``./scripts/deploy.sh``, ``scripts/notify.py``).
_ALLOWED_EXTENSIONS: frozenset[str] = frozenset(
    {".sh", ".bash", ".zsh", ".py", ".rb", ".pl", ".js", ".ts", ".bat", ".ps1", ".cmd"}
)


def _is_allowed(executable: str, extra_allowlist: frozenset[str] | None = None) -> bool:
    """Return True when *executable* is in the combined allowlist.

    Allows:
    - bare names that are in ``_DEFAULT_ALLOWLIST`` (or *extra_allowlist*)
    - paths whose final component is in the allowlist
    - any path whose suffix is in ``_ALLOWED_EXTENSIONS``
    """
    combined = _DEFAULT_ALLOWLIST | (extra_allowlist or frozenset())
    # Bare name or last path component in allowlist
    path = Path(executable)
    if path.name in combined or executable in combined:
        return True
    # Script by extension
    if path.suffix.lower() in _ALLOWED_EXTENSIONS:
        return True
    return False


def run_hook(
    hook_argv: list[str],
    repo_root: Path,
    label: str,
    *,
    extra_allowlist: frozenset[str] | None = None,
) -> None:
    """Execute *hook_argv* safely without a shell.

    Parameters
    ----------
    hook_argv:
        The hook command as a validated list of strings.  Must be non-empty.
    repo_root:
        Working directory for the subprocess.
    label:
        Human-readable name used in error messages (e.g. ``"pre_promote"``).
    extra_allowlist:
        Additional executable names to permit beyond the built-in default.

    Raises
    ------
    ConfigError
        If the first element of *hook_argv* is not in the allowlist.
    subprocess.CalledProcessError
        If the hook exits with a non-zero status.
    """
    if not hook_argv:
        raise ConfigError(f"Hook '{label}' is an empty list — nothing to execute.")

    executable = hook_argv[0]
    if not _is_allowed(executable, extra_allowlist):
        raise ConfigError(
            f"Hook '{label}' executable '{executable}' is not in the permitted allowlist. "
            "Add it to hook_allowlist in .lanegate.yml or use a wrapper script."
        )

    subprocess.run(
        hook_argv,
        shell=False,
        check=True,
        cwd=repo_root,
    )
