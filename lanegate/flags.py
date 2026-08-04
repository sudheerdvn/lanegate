"""
flags.py — per-environment feature flag management.

Key design constraints:
- Path resolved at CALL TIME (not import time) so HOME-swap per-env isolation works.
- Simple JSON format: {name: bool}.
- Atomic write via temp-file rename under a stable lock file.
- Resolves per-env flag_file from config; rejects unknown env names (FLG-01).
- Lock is held across the full read-modify-write cycle (FLG-02).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import portalocker

from lanegate import APP_NAME

LOCK_SUFFIX = ".lock"


class FlagError(ValueError):
    """Raised for invalid flag operations (e.g. unknown environment name)."""


def _known_env_names(cfg: dict) -> list[str]:
    """Return the list of registered environment names from config."""
    return [env["name"] for env in cfg.get("environments", []) if "name" in env]


def _resolve_flag_path(cfg: dict, env_name: str | None) -> Path:
    """
    Return the flag file path for the given environment (or the default if env_name is None).
    Path is expanded at call time so HOME-swap isolation works.

    Raises FlagError if env_name is not None and not found in the known environments list,
    or if the environment lacks a configured flag_file.
    """
    if env_name is not None:
        known = _known_env_names(cfg)
        if env_name not in known:
            raise FlagError(
                f"unknown environment {env_name!r}; "
                f"valid environments: {known or ['(none configured)']}"
            )
        for env in cfg.get("environments", []):
            if env["name"] == env_name:
                if not env.get("flag_file"):
                    raise FlagError(
                        f"environment {env_name!r} has no flag_file configured; "
                        f"add 'flag_file' to the environment in .lanegate.yml"
                    )
                return Path(os.path.expandvars(os.path.expanduser(env["flag_file"])))
    # Fall back to top-level flag_file (only if env_name is None)
    default = cfg.get("flag_file", f"~/.{APP_NAME}/feature_flags.json")
    return Path(os.path.expandvars(os.path.expanduser(default)))


def _load_raw(flag_path: Path) -> dict[str, bool]:
    if not flag_path.exists():
        return {}
    try:
        with flag_path.open() as f:
            data = json.load(f)
        return {k: bool(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _update_flags(flag_path: Path, updates: dict[str, bool]) -> None:
    """
    Atomically apply *updates* to the flag file at *flag_path*.

    The stable lock file (flag_path + LOCK_SUFFIX) is held across the entire
    read-modify-write cycle so concurrent callers cannot overwrite each other.
    """
    flag_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = flag_path.with_suffix(LOCK_SUFFIX)

    with portalocker.Lock(str(lock_path), "a", timeout=None):
        # Read the current state *inside* the lock to capture any concurrent writes.
        current = _load_raw(flag_path)
        current.update(updates)
        tmp_path = flag_path.with_suffix(".tmp")
        with tmp_path.open("w") as tmp_fh:
            json.dump(current, tmp_fh, indent=2, sort_keys=True)
            tmp_fh.flush()
            os.fsync(tmp_fh.fileno())
        tmp_path.replace(flag_path)


def get_all(cfg: dict, env_name: str | None = None) -> dict[str, bool]:
    return _load_raw(_resolve_flag_path(cfg, env_name))


def set_flag(cfg: dict, name: str, value: bool, env_name: str | None = None) -> None:
    flag_path = _resolve_flag_path(cfg, env_name)
    _update_flags(flag_path, {name: value})


def cmd_flag(
    subcmd: str, flag_name: str | None, cfg: dict, env_name: str | None, json_output: bool = False
) -> None:
    import json as _json

    from lanegate import APP_NAME

    if subcmd == "list":
        flags = get_all(cfg, env_name)
        if json_output:
            print(_json.dumps({"env": env_name, "flags": flags}, indent=2))
            return
        env_label = f" [{env_name}]" if env_name else ""
        flag_path = _resolve_flag_path(cfg, env_name)
        if not flags:
            print(f"No feature flags set{env_label} (all default OFF).")
            return
        print(f"Feature flags{env_label} ({flag_path}):")
        for name in sorted(flags):
            state = "ON " if flags[name] else "off"
            print(f"  [{state}]  {name}")

    elif subcmd in ("enable", "disable"):
        if flag_name is None:
            raise FlagError(f"flag name is required for '{subcmd}'")
        value = subcmd == "enable"
        set_flag(cfg, flag_name, value, env_name)
        action = "enabled" if value else "disabled"
        env_label = f" [{env_name}]" if env_name else ""
        print(f"Flag '{flag_name}' {action}{env_label}.")
        if value and not env_name:
            print(
                f"  To scope to a specific environment: {APP_NAME} flag enable {flag_name} --env <name>"
            )
    else:
        raise ValueError(f"unknown flag subcommand: {subcmd}")
