"""Check PyPI for newer versions of brr-cli and print a notice."""

import json
import time
import urllib.request
from importlib.metadata import version as _pkg_version
from packaging.version import Version
from rich.console import Console

from brr.state import STATE_DIR

_PACKAGE = "brr-cli"
_CACHE_FILE = STATE_DIR / ".update_check"
_CACHE_TTL = 86400  # 24 hours
_PYPI_URL = f"https://pypi.org/pypi/{_PACKAGE}/json"
_TIMEOUT = 2  # seconds


def _fetch_latest_version() -> str | None:
    try:
        req = urllib.request.Request(_PYPI_URL)
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read())
        return data["info"]["version"]
    except Exception:
        return None


def _read_cache() -> tuple[float, str] | None:
    try:
        text = _CACHE_FILE.read_text().strip()
        ts_str, ver = text.split(":", 1)
        return float(ts_str), ver
    except Exception:
        return None


def _write_cache(version: str) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(f"{time.time():.0f}:{version}")
    except Exception:
        pass


def print_update_notice() -> None:
    """Print a notice if a newer version is available on PyPI."""
    try:
        installed = Version(_pkg_version(_PACKAGE))

        cached = _read_cache()
        if cached:
            ts, ver = cached
            if time.time() - ts < _CACHE_TTL:
                if Version(ver) <= installed:
                    return
                Console(stderr=True).print(
                    f"\n[yellow]A new version of brr is available: "
                    f"{installed} → {ver}[/yellow]\n"
                    f"Run: [bold]uv tool upgrade {_PACKAGE}[/bold]\n"
                )
                return

        latest_str = _fetch_latest_version()
        if latest_str is None:
            return

        _write_cache(latest_str)

        if Version(latest_str) > installed:
            Console(stderr=True).print(
                f"\n[yellow]A new version of brr is available: "
                f"{installed} → {latest_str}[/yellow]\n"
                f"Run: [bold]uv tool upgrade {_PACKAGE}[/bold]\n"
            )
    except Exception:
        pass
