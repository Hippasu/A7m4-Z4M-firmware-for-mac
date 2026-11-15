"""Zero-config path discovery for the auth backend and the PMCA files.

The probe needs two things on disk that do not live in this repository:

* ``<software>/app/protocol/auth.py``  -- the upstream Senser auth module
* ``pmca/usb/{constants,crypto}.py``    -- the Sony-PMCA-RE crypto tables

Asking the operator to paste ``<Sony-PMCA-RE>/pmca/usb`` is exactly the kind of
step that makes a workbench unusable, so this module looks for both in the
obvious places (explicit flag -> env var -> install-module vendor ->
config file -> known layout -> bounded filesystem scan) and remembers what
it found in ``m4probe.local.json`` beside the package.

Set ``ZM4_INSTALL_USE_LOCAL_VENDOR=1`` to skip the bundled install-module
vendor (debug against machine-local auth/pmca paths).

Nothing here touches USB or the camera: it is pure filesystem inspection and is
therefore safe to run from the web console on every page load.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "m4probe.local.json"

PMCA_FILES = ("constants.py", "crypto.py")
AUTH_RELATIVE = Path("app/protocol/auth.py")

# Directories that never contain what we want and are expensive to walk.
SKIP_NAMES = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "Library", ".Trash", ".cache", ".npm", ".cargo", "dist", "build",
    ".mypy_cache", ".pytest_cache", "site-packages", "Photos Library.photoslibrary",
}

# Where a checkout normally lives on a developer Mac. Kept shallow on purpose:
# discovery must stay well under a second on the second run (config cache).
SCAN_ROOTS = (
    Path.home() / "Documents",
    Path.home() / "WorkBuddy",
    Path.home() / "Downloads",
    Path.home() / "Desktop",
    PROJECT_ROOT.parent,
)

# Hard ceilings so a pathological home directory cannot stall the console.
MAX_DEPTH = 8
MAX_DIRS = 40000
MAX_SECONDS = 4.0


def load_config() -> dict[str, Any]:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_config(**updates: Any) -> dict[str, Any]:
    data = load_config()
    for key, value in updates.items():
        if value in (None, "", False):
            data.pop(key, None)
        else:
            data[key] = str(value)
    try:
        CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                               encoding="utf-8")
    except OSError:
        pass
    return data


def _env_path(name: str) -> Path | None:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else None


def _looks_like_pmca(path: Path) -> bool:
    return path.is_dir() and all((path / name).is_file() for name in PMCA_FILES)


def _use_local_vendor_only() -> bool:
    return (os.environ.get("ZM4_INSTALL_USE_LOCAL_VENDOR") or "").strip() in (
        "1", "true", "yes", "on",
    )


def _install_module_vendor_hits() -> dict[str, Path | None]:
    """Paths from ``zm4_install.resolve_vendor_paths`` (empty when skipped/missing)."""
    if _use_local_vendor_only():
        return {"auth_backend": None, "pmca_dir": None}
    try:
        from .install_api import load_install_module
    except Exception:
        return {"auth_backend": None, "pmca_dir": None}
    try:
        mod = load_install_module()
    except Exception:
        return {"auth_backend": None, "pmca_dir": None}
    if mod is None:
        return {"auth_backend": None, "pmca_dir": None}
    resolver = getattr(mod, "resolve_vendor_paths", None)
    if not callable(resolver):
        return {"auth_backend": None, "pmca_dir": None}
    try:
        paths = resolver() or {}
    except Exception:
        return {"auth_backend": None, "pmca_dir": None}
    auth = paths.get("auth_backend")
    pmca = paths.get("pmca_dir")
    return {
        "auth_backend": Path(auth) if auth else None,
        "pmca_dir": Path(pmca) if pmca else None,
    }


def pmca_layout_candidates(base: Path) -> list[Path]:
    """Plausible ``pmca/usb`` directories under a checkout or release ZIP."""
    return [
        base / "pmca/usb",                  # Sony-PMCA-RE source tree
        base / "vendor/sony_pmca/pmca/usb",  # official release ZIP layout
        base / "usb",                        # already pointing at the pmca package
    ]


def _iter_dirs(roots: Iterable[Path], deadline: float) -> Iterable[tuple[Path, int]]:
    """Breadth-first walk with depth / count / time ceilings."""
    seen = 0
    for root in roots:
        if not root.is_dir():
            continue
        queue: list[tuple[Path, int]] = [(root, 0)]
        while queue:
            current, depth = queue.pop(0)
            seen += 1
            if seen > MAX_DIRS or time.monotonic() > deadline:
                return
            yield current, depth
            if depth >= MAX_DEPTH:
                continue
            try:
                entries = list(os.scandir(current))
            except OSError:
                continue
            for entry in entries:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if entry.name in SKIP_NAMES or entry.name.startswith("."):
                    continue
                queue.append((Path(entry.path), depth + 1))


def find_pmca(scan: bool = True) -> list[dict[str, str]]:
    """All usable ``pmca/usb`` directories, best first, with where they came from."""
    found: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(path: Path | None, source: str) -> None:
        if path is None:
            return
        path = path.expanduser()
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen or not _looks_like_pmca(path):
            return
        seen.add(key)
        found.append({"path": key, "source": source})

    add(_env_path("M4PROBE_PMCA_DIR"), "环境变量 M4PROBE_PMCA_DIR")
    add(_install_module_vendor_hits().get("pmca_dir"), "安装模块 vendor")
    config = load_config()
    if config.get("pmca_dir"):
        add(Path(config["pmca_dir"]), "已保存的配置")
    add(PROJECT_ROOT / "vendor/sony_pmca/pmca/usb", "项目内 vendor 目录")

    # A pmca package installed into the running interpreter.
    try:
        import importlib.util

        spec = importlib.util.find_spec("pmca")
        if spec and spec.submodule_search_locations:
            for location in spec.submodule_search_locations:
                add(Path(location) / "usb", "已安装的 pmca 包")
    except (ImportError, ValueError):
        pass

    if scan and not found:
        deadline = time.monotonic() + MAX_SECONDS
        for current, _depth in _iter_dirs(SCAN_ROOTS, deadline):
            if current.name not in ("pmca", "sony_pmca"):
                continue
            for candidate in pmca_layout_candidates(current.parent):
                add(candidate, "自动扫描发现")
            add(current / "usb", "自动扫描发现")
            add(current / "pmca/usb", "自动扫描发现")
            if found:
                return found
    return found


def find_auth_backend(scan: bool = True) -> list[dict[str, str]]:
    """All directories that contain ``app/protocol/auth.py``, best first."""
    found: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(path: Path | None, source: str) -> None:
        if path is None:
            return
        path = path.expanduser()
        if not (path / AUTH_RELATIVE).is_file():
            return
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen:
            return
        seen.add(key)
        found.append({"path": key, "source": source})

    add(_env_path("M4PROBE_AUTH_BACKEND"), "环境变量 M4PROBE_AUTH_BACKEND")
    add(_install_module_vendor_hits().get("auth_backend"), "安装模块 vendor")
    config = load_config()
    if config.get("auth_backend"):
        add(Path(config["auth_backend"]), "已保存的配置")
    add(PROJECT_ROOT / "vendor/sony_auth", "项目内 vendor 目录")

    # The PMCA checkout often sits next to the auth backend; try siblings first
    # so the common case never needs a full scan.
    for entry in list(found):
        base = Path(entry["path"])
        for up in (base, base.parent, base.parent.parent):
            for name in ("software", "no-fastboot-install/software"):
                add(up / name, "与 PMCA 同目录")

    if scan and not found:
        deadline = time.monotonic() + MAX_SECONDS
        for current, _depth in _iter_dirs(SCAN_ROOTS, deadline):
            if (current / AUTH_RELATIVE).is_file():
                add(current, "自动扫描发现")
                return found
    return found


def resolve(auth_backend: Path | None = None, pmca_dir: Path | None = None,
            scan: bool = True) -> dict[str, Any]:
    """Pick the auth backend / PMCA dir to use, plus what else was available."""
    auth_hits = find_auth_backend(scan=scan)
    pmca_hits = find_pmca(scan=scan)
    chosen_auth = auth_backend or (Path(auth_hits[0]["path"]) if auth_hits else None)
    chosen_pmca = pmca_dir or (Path(pmca_hits[0]["path"]) if pmca_hits else None)
    # An explicit flag that points nowhere must not silently become the guess.
    if auth_backend and not (Path(auth_backend) / AUTH_RELATIVE).is_file():
        chosen_auth = None
    if pmca_dir and not _looks_like_pmca(Path(pmca_dir)):
        chosen_pmca = None
    return {
        "auth_backend": chosen_auth,
        "pmca_dir": chosen_pmca,
        "auth_candidates": auth_hits,
        "pmca_candidates": pmca_hits,
        "config_path": str(CONFIG_PATH),
        "scanned": bool(scan),
    }
