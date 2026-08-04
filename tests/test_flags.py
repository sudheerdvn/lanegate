"""Tests for flags.py — per-env resolution, call-time path expansion, atomic write."""

import json
import os
import sys
import threading
from unittest.mock import patch

import pytest

from lanegate.flags import LOCK_SUFFIX, FlagError, _resolve_flag_path, get_all, set_flag

_unix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="fcntl / flock not available on Windows",
)


def _cfg(flag_file=None, environments=None) -> dict:
    return {
        "flag_file": flag_file or "~/.lanegate/feature_flags.json",
        "environments": environments or [],
    }


def test_default_flag_file_resolved(tmp_path):
    cfg = _cfg(flag_file=str(tmp_path / "flags.json"))
    path = _resolve_flag_path(cfg, None)
    assert path == tmp_path / "flags.json"


def test_env_flag_file_resolved(tmp_path):
    staging_flags = tmp_path / "staging.json"
    cfg = _cfg(
        flag_file=str(tmp_path / "live.json"),
        environments=[{"name": "staging", "flag_file": str(staging_flags)}],
    )
    path = _resolve_flag_path(cfg, "staging")
    assert path == staging_flags


@_unix_only
def test_home_swap_isolation(tmp_path):
    """Changing HOME at call time routes to a different flag file."""
    home_a = tmp_path / "home_a"
    home_b = tmp_path / "home_b"
    home_a.mkdir()
    home_b.mkdir()

    cfg = _cfg(flag_file="~/.lanegate/flags.json")

    with patch.dict(os.environ, {"HOME": str(home_a)}):
        path_a = _resolve_flag_path(cfg, None)
    with patch.dict(os.environ, {"HOME": str(home_b)}):
        path_b = _resolve_flag_path(cfg, None)

    assert path_a != path_b
    assert str(home_a) in str(path_a)
    assert str(home_b) in str(path_b)


@_unix_only
def test_set_and_get_flag(tmp_path):
    cfg = _cfg(flag_file=str(tmp_path / "flags.json"))
    set_flag(cfg, "my_feature", True)
    flags = get_all(cfg)
    assert flags["my_feature"] is True


@_unix_only
def test_disable_flag(tmp_path):
    cfg = _cfg(flag_file=str(tmp_path / "flags.json"))
    set_flag(cfg, "my_feature", True)
    set_flag(cfg, "my_feature", False)
    flags = get_all(cfg)
    assert flags["my_feature"] is False


@_unix_only
def test_per_env_flags_are_independent(tmp_path):
    """Enabling a flag in staging must NOT affect production."""
    staging_file = tmp_path / "staging.json"
    prod_file = tmp_path / "prod.json"
    cfg = _cfg(
        flag_file=str(prod_file),
        environments=[
            {"name": "staging", "flag_file": str(staging_file)},
            {"name": "production", "flag_file": str(prod_file)},
        ],
    )
    set_flag(cfg, "new_algo", True, env_name="staging")
    staging_flags = get_all(cfg, "staging")
    prod_flags = get_all(cfg, "production")
    assert staging_flags.get("new_algo") is True
    assert prod_flags.get("new_algo") is None  # not set in production


def test_get_all_missing_file(tmp_path):
    cfg = _cfg(flag_file=str(tmp_path / "nonexistent.json"))
    assert get_all(cfg) == {}


def test_get_all_corrupt_file(tmp_path):
    flag_file = tmp_path / "flags.json"
    flag_file.write_text("NOT JSON {{{{")
    cfg = _cfg(flag_file=str(flag_file))
    assert get_all(cfg) == {}


@_unix_only
def test_atomic_write_creates_parent_dirs(tmp_path):
    nested = tmp_path / "deep" / "nested" / "flags.json"
    cfg = _cfg(flag_file=str(nested))
    set_flag(cfg, "x", True)
    assert nested.exists()
    data = json.loads(nested.read_text())
    assert data["x"] is True


# ── FLG-01: unknown env raises FlagError ────────────────────────────────────


def test_unknown_env_raises_flag_error(tmp_path):
    """An env name not in the config must raise FlagError, not silently fall back."""
    cfg = _cfg(
        flag_file=str(tmp_path / "default.json"),
        environments=[{"name": "staging", "flag_file": str(tmp_path / "staging.json")}],
    )
    with pytest.raises(FlagError, match="unknown environment"):
        _resolve_flag_path(cfg, "typo-env")


def test_unknown_env_set_flag_raises(tmp_path):
    """set_flag with an unregistered env name must raise FlagError."""
    cfg = _cfg(flag_file=str(tmp_path / "default.json"))
    with pytest.raises(FlagError, match="unknown environment"):
        set_flag(cfg, "my_feature", True, env_name="ghost-env")


def test_unknown_env_does_not_touch_default_file(tmp_path):
    """After a FlagError the default flag file must be untouched."""
    default = tmp_path / "default.json"
    cfg = _cfg(flag_file=str(default))
    with pytest.raises(FlagError):
        set_flag(cfg, "my_feature", True, env_name="nonexistent")
    assert not default.exists()


def test_known_env_without_flag_file_raises_error(tmp_path):
    """An env registered in the list but with no flag_file must raise FlagError (not silently fall back)."""
    default = tmp_path / "default.json"
    cfg = _cfg(
        flag_file=str(default),
        environments=[{"name": "staging", "flag_file": None}],
    )
    # Must raise FlagError since per-env scoping requires an explicit flag_file
    with pytest.raises(FlagError, match="has no flag_file configured"):
        _resolve_flag_path(cfg, "staging")


def test_set_flag_on_env_without_flag_file_raises(tmp_path):
    """set_flag with an env that lacks flag_file must raise FlagError (F47 fix)."""
    cfg = _cfg(
        flag_file=str(tmp_path / "default.json"),
        environments=[{"name": "staging", "flag_file": None}],
    )
    with pytest.raises(FlagError, match="has no flag_file configured"):
        set_flag(cfg, "my_feature", True, env_name="staging")


# ── FLG-02: lock is on the stable flag file, not a temp file ─────────────────


@_unix_only
def test_lock_file_is_stable_sidecar(tmp_path):
    """After set_flag the lock file must be a stable sidecar, not a .tmp or random name."""
    flag_file = tmp_path / "flags.json"
    cfg = _cfg(flag_file=str(flag_file))
    set_flag(cfg, "alpha", True)
    lock_file = flag_file.with_suffix(LOCK_SUFFIX)
    assert lock_file.exists(), f"Expected stable lock file at {lock_file}"
    # No leftover .tmp files
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == [], f"Unexpected .tmp files left: {tmp_files}"


@_unix_only
def test_concurrent_writes_both_survive(tmp_path):
    """Two threads writing different flags concurrently must both be present in the final file."""
    flag_file = tmp_path / "flags.json"
    cfg = _cfg(flag_file=str(flag_file))

    errors: list[Exception] = []

    def write_alpha():
        try:
            set_flag(cfg, "alpha", True)
        except Exception as exc:
            errors.append(exc)

    def write_beta():
        try:
            set_flag(cfg, "beta", True)
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=write_alpha)
    t2 = threading.Thread(target=write_beta)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == [], f"Concurrent write raised: {errors}"
    final = json.loads(flag_file.read_text())
    assert final.get("alpha") is True, "alpha flag was lost in concurrent write"
    assert final.get("beta") is True, "beta flag was lost in concurrent write"
