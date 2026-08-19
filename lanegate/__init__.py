# Single source of truth for the brand name.
# Rename: change APP_NAME here + pyproject.toml name/scripts entry + (optionally) the lanegate/ dir.
from importlib.metadata import PackageNotFoundError, version

APP_NAME = "lanegate"

# Distribution metadata is set by the build backend and is the authoritative
# version for an installed CLI.  A source checkout without installed metadata
# should not pretend to be a released version.
try:
    __version__ = version(APP_NAME)
except PackageNotFoundError:
    __version__ = "0+unknown"
