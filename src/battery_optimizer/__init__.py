"""Energy baseline measurement toolkit (ADR-0003)."""

from importlib.metadata import PackageNotFoundError, version

try:
    #: The installed distribution's version: ``[project] version`` in ``pyproject.toml``, the one
    #: place it is set (by ``just release``).
    __version__ = version("battery-optimizer")
except PackageNotFoundError:  # pragma: no cover - a source tree run without installing
    __version__ = "0+unknown"
