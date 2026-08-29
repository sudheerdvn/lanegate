"""Trusted Git and repository/config discovery helpers."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

from lanegate.config import CONFIG_FILENAME, ConfigError


def _trusted_git_executable() -> str:
    """Return a protected Git executable found through absolute PATH entries.

    We cannot hard-code ``/usr/bin/git``: supported installations commonly
    place Git in a protected nonstandard prefix (for example an enterprise
    toolchain under ``/opt``).  PATH is therefore only an *index* into
    candidate locations, never a trust decision.  On POSIX the resolved
    executable and every containing directory must be owned by someone other
    than the LaneGate process and not writable by group or other users.  (The
    owner is normally root; comparing against the effective user also works
    inside user namespaces which map host-root files to an overflow uid.)  A
    worktree-controlled ``PATH`` entry, an empty entry (the current directory),
    or a relative entry cannot pass that test.  Resolve before returning so a
    mutable PATH directory cannot swap a symlink between validation and
    execution.

    On Windows, candidate locations come only from machine-wide installer
    registry entries (plus the conventional machine install).  In
    particular, neither the caller's PATH nor its current directory is ever
    searched.  This supports an administrator-installed custom prefix such
    as ``D:\\Tools\\Git`` without treating an agent-controlled per-user
    installation as authoritative.
    """
    if os.name == "nt":
        candidates = _windows_git_candidates()
    else:
        candidates = tuple(
            Path(entry) / "git"
            for entry in os.environ.get("PATH", "").split(os.pathsep)
            if entry and Path(entry).is_absolute()
        )

    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            metadata = resolved.stat()
        except OSError:
            continue
        if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
            continue
        if os.name != "nt" and not _is_protected_executable(resolved):
            continue
        return str(resolved)
    raise ConfigError("unable to determine a trusted Git control checkout")


def _windows_git_candidates() -> tuple[Path, ...]:
    """Return Git paths registered by the machine-wide Windows installer.

    ``HKLM`` is intentionally the only registry hive consulted: HKCU and
    environment variables are writable by the account running a ticket, so
    they cannot establish a trusted executable.  Git for Windows records
    either an App Paths executable or an installation directory in these
    locations.  The conventional Program Files path is retained for older
    installers that did not create a registry entry.
    """
    candidates: list[Path] = []
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        winreg = None  # type: ignore[assignment]

    if winreg is not None:
        views = [0]
        for flag_name in ("KEY_WOW64_64KEY", "KEY_WOW64_32KEY"):
            flag = getattr(winreg, flag_name, 0)
            if flag and flag not in views:
                views.append(flag)

        def machine_value(key_name: str, value_name: str | None) -> str | None:
            for view in views:
                try:
                    with winreg.OpenKey(  # type: ignore[attr-defined]
                        winreg.HKEY_LOCAL_MACHINE,  # type: ignore[attr-defined]
                        key_name,
                        0,
                        winreg.KEY_READ | view,  # type: ignore[attr-defined]
                    ) as key:
                        value, _ = winreg.QueryValueEx(key, value_name)  # type: ignore[attr-defined,arg-type]
                except OSError:
                    continue
                if isinstance(value, str) and value:
                    return value
            return None

        app_path = machine_value(
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\git.exe", None
        )
        if app_path:
            candidates.append(Path(app_path))
        for value_name in ("InstallPath", "InstallPath64", "Path"):
            install_path = machine_value(r"SOFTWARE\GitForWindows", value_name)
            if install_path:
                install = Path(install_path)
                candidates.extend((install / "cmd" / "git.exe", install / "bin" / "git.exe"))

    candidates.extend(
        (
            Path(r"C:\\Program Files\\Git\\cmd\\git.exe"),
            Path(r"C:\\Program Files\\Git\\bin\\git.exe"),
        )
    )
    # A registry entry can appear in both registry views.  Preserve order so
    # the installed path wins over the conventional fallback.
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).casefold()
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return tuple(unique)


def _is_protected_executable(path: Path) -> bool:
    """Whether *path* and every containing directory resist agent mutation."""
    effective_uid = os.geteuid()  # type: ignore[attr-defined]
    for ancestor in (path, *path.parents):
        try:
            metadata = ancestor.stat()
        except OSError:
            return False
        # Root has no meaningful ownership boundary from its effective uid:
        # root-owned system binaries are the expected trusted installation.
        # The non-writable mode requirement still prevents group/other users
        # from replacing any component in the executable path.
        if (
            (effective_uid != 0 and metadata.st_uid == effective_uid)
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            return False
    return True


def _control_checkout_root(start: Path) -> Path | None:
    """Return the shared control checkout root for *start*, if it is in Git.

    Linked worktrees have their own checkout root but share a common Git
    directory with the control checkout.  Lifecycle commands must use the
    latter's configuration: a worktree is controlled by an agent and may
    contain an uncommitted, locally planted config file.
    """
    git = _trusted_git_executable()
    # This probe never contacts a remote, and explicitly disallows an
    # interactive credential prompt should a local Git wrapper/config attempt
    # to cause one.
    # Git environment variables can redirect even an absolute Git executable
    # to an attacker-selected repository (for example GIT_DIR) or make it
    # stop walking at a worktree boundary (GIT_CEILING_DIRECTORIES).  Strip
    # all of them rather than trusting the executor's environment.
    probe_env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    probe_env["GIT_TERMINAL_PROMPT"] = "0"
    # The non-repository result below is Git's stable C-locale diagnostic.
    # Pin it so standalone discovery does not depend on the caller's locale.
    probe_env["LC_ALL"] = "C"
    try:
        result = subprocess.run(
            [git, "-C", str(start), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=False,
            env=probe_env,
        )
    except OSError as exc:
        # Failing open here would make a worktree-local config authoritative.
        # In particular, an agent could shadow ``git`` on PATH and arrange for
        # just this probe to fail before invoking a lifecycle command.
        raise ConfigError("unable to determine a trusted Git control checkout") from exc
    if result.returncode != 0:
        # Standalone directories retain walk-up discovery.  A real Git binary
        # reports this specific condition when ``start`` is outside a
        # repository; every other failure is ambiguous and must fail closed.
        stderr = result.stderr.lower()
        if "not a git repository" in stderr:
            return None
        raise ConfigError("unable to determine a trusted Git control checkout")

    # --path-format=absolute was added after Git 2.25, so do not require it
    # for the control-plane boundary.  Only the final record terminator is
    # removable: a newline anywhere else would make the path ambiguous.
    common_dir_text = result.stdout.removesuffix("\n")
    if not common_dir_text or "\n" in common_dir_text or "\r" in common_dir_text:
        raise ConfigError("unable to determine a trusted Git control checkout")
    common_dir = Path(common_dir_text)
    if not common_dir.is_absolute():
        common_dir = start / common_dir
    common_dir = common_dir.resolve()

    # The normal form is <control checkout>/.git, including linked worktrees.
    # Submodules and separate Git directories instead use a common directory
    # such as .git/modules/<name>.  Their trusted primary worktree is recorded
    # in that common directory's core.worktree setting.
    if common_dir.name == ".git":
        return common_dir.parent

    try:
        primary_result = subprocess.run(
            [git, f"--git-dir={common_dir}", "config", "--get", "core.worktree"],
            capture_output=True,
            text=True,
            check=False,
            env=probe_env,
        )
    except OSError as exc:
        raise ConfigError("unable to determine a trusted Git control checkout") from exc
    if primary_result.returncode != 0:
        raise ConfigError("unable to determine a trusted Git control checkout")

    primary_text = primary_result.stdout.removesuffix("\n")
    if not primary_text or "\n" in primary_text or "\r" in primary_text:
        raise ConfigError("unable to determine a trusted Git control checkout")
    primary = Path(primary_text)
    return (primary if primary.is_absolute() else common_dir / primary).resolve()


def is_linked_worktree(start: Path) -> bool | None:
    """Whether *start* is inside a linked worktree rather than its control checkout.

    A linked worktree has its own ``--git-dir`` (``<control>/.git/worktrees/<name>``)
    distinct from the shared ``--git-common-dir`` (``<control>/.git``); the control
    checkout itself reports the same path for both. This distinction is independent
    of filesystem layout — unlike a path-containment check against the control
    checkout's root, it is not fooled by a worktree nested *underneath* the control
    checkout (e.g. a ``worktrees_dir`` configured inside the repo itself), since a
    nested worktree still has its own distinct git-dir.

    Returns ``None`` when *start* is not inside a Git repository at all (nothing to
    classify). Uses the same trusted, ownership-verified Git executable and
    ``GIT_*``-stripped environment as ``_control_checkout_root`` — see that
    function's docstring for why an untrusted probe cannot be used here.
    """
    git = _trusted_git_executable()
    probe_env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    probe_env["GIT_TERMINAL_PROMPT"] = "0"
    probe_env["LC_ALL"] = "C"
    try:
        result = subprocess.run(
            [git, "-C", str(start), "rev-parse", "--git-dir", "--git-common-dir"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env=probe_env,
        )
    except OSError as exc:
        raise ConfigError("unable to determine whether this is a linked worktree") from exc
    if result.returncode != 0:
        stderr = result.stderr.lower()
        if "not a git repository" in stderr:
            return None
        raise ConfigError("unable to determine whether this is a linked worktree")

    lines = result.stdout.splitlines()
    if len(lines) != 2:
        raise ConfigError("unable to determine whether this is a linked worktree")
    git_dir, common_dir = (Path(line) for line in lines)
    if not git_dir.is_absolute():
        git_dir = start / git_dir
    if not common_dir.is_absolute():
        common_dir = start / common_dir
    return git_dir.resolve() != common_dir.resolve()


def find_config(start: Path | None = None) -> Path | None:
    """Find the trusted project config, or None when no config exists.

    In Git repositories this considers only the shared control checkout's
    top-level config, never a config planted in a linked worktree or subdir.
    The historical walk-up behavior is retained for non-Git directories so
    ``lanegate init`` and standalone config discovery remain usable.
    """
    here = (start or Path.cwd()).resolve()
    control_root = _control_checkout_root(here)
    if control_root is not None:
        candidate = control_root / CONFIG_FILENAME
        return candidate if candidate.exists() else None
    for directory in [here, *here.parents]:
        candidate = directory / CONFIG_FILENAME
        if candidate.exists():
            return candidate
    return None


def find_repo_root(start: Path | None = None) -> Path:
    """Find the trusted control root; fall back to config discovery or cwd."""
    here = (start or Path.cwd()).resolve()
    control_root = _control_checkout_root(here)
    if control_root is not None:
        return control_root
    config_path = find_config(here)
    if config_path:
        return config_path.parent
    return here


