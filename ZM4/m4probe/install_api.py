"""Public contract for the proprietary install module (open-core boundary).

Shell loads InstallModule via load_install_module; missing module degrades
gracefully (diagnose / connection detect still work).
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

MISSING_MODULE_ERROR = (
    "需要官方安装模块才能使用完整功能。"
    "请安装完整版发行包，或将模块放到 "
    "~/Library/Application Support/ZM4/install-module/。"
)


@dataclass(frozen=True)
class QuotaResult:
    ok: bool
    error: str = ""
    camera_ids: tuple[str, ...] = ()
    max_cameras: int = 2
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class InstallContext:
    """Hints passed into the install module (paths / identities)."""

    machine_id: str = ""
    camera_id: str = ""
    cwd: Path | None = None
    auth_backend: Path | None = None
    pmca_dir: Path | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class InstallModule(Protocol):
    def available(self) -> bool: ...

    def capability(self) -> dict[str, Any]: ...

    def check_quota(self, machine_id: str, camera_id: str) -> QuotaResult: ...

    def privileged_module(self, kind: str) -> str: ...

    def resolve_vendor_paths(self) -> dict[str, Path | None]: ...

    def run_install_chain(self, ctx: InstallContext) -> dict[str, Any]: ...


def _try_import_name(name: str) -> InstallModule | None:
    try:
        mod = importlib.import_module(name)
    except Exception:
        return None
    getter = getattr(mod, "get_install_module", None)
    if not callable(getter):
        return None
    try:
        inst = getter()
    except Exception:
        return None
    if inst is None:
        return None
    if not getattr(inst, "available", lambda: False)():
        return None
    return inst  # type: ignore[return-value]


def _try_import_path(root: Path) -> InstallModule | None:
    """Load ``<root>/zm4_install`` or treat ``root`` itself as the package dir."""
    root = root.expanduser()
    if not root.is_dir():
        return None
    candidates = [root]
    nested = root / "zm4_install"
    if nested.is_dir():
        candidates.insert(0, nested)
    for pkg_dir in candidates:
        init = pkg_dir / "__init__.py"
        if not init.is_file():
            continue
        parent = str(pkg_dir.parent.resolve())
        if parent not in sys.path:
            sys.path.insert(0, parent)
        name = pkg_dir.name
        # Drop cached failed imports so a freshly placed module can load.
        for key in list(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key, None)
        return _try_import_name(name)
    return None


def _app_resource_candidates() -> list[Path]:
    """macOS complete-build locations for an embedded ``zm4_install``."""
    out: list[Path] = []
    env_res = (os.environ.get("ZM4_APP_RESOURCES") or "").strip()
    if env_res:
        out.append(Path(env_res).expanduser())
    # Contained in .app when the workbench is launched as a bundle helper.
    try:
        exe = Path(sys.executable).resolve()
        # .../ZM4.app/Contents/MacOS/<bin> → Resources
        if exe.parent.name == "MacOS" and exe.parent.parent.name == "Contents":
            out.append(exe.parent.parent / "Resources")
    except Exception:  # noqa: BLE001
        pass
    # Common desktop complete build (dev / ad-hoc).
    desktop_app = Path.home() / "Desktop" / "ZM4.app" / "Contents" / "Resources"
    out.append(desktop_app)
    # Dedup while preserving order.
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in out:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return uniq


def load_install_module(*, force_reload: bool = False) -> InstallModule | None:
    """Resolve the install module.

    Order (open-core):
      1. ``ZM4_INSTALL_MODULE`` — import name or filesystem directory
      2. App bundle ``Contents/Resources/`` (``ZM4_APP_RESOURCES`` / .app)
      3. ``~/Library/Application Support/ZM4/install-module/``
      4. ``import zm4_install`` (monorepo / site-packages)
    """
    if force_reload:
        for key in list(sys.modules):
            if key == "zm4_install" or key.startswith("zm4_install."):
                sys.modules.pop(key, None)

    env = (os.environ.get("ZM4_INSTALL_MODULE") or "").strip()
    if env:
        path = Path(env)
        if path.is_dir() or "/" in env or env.startswith("~"):
            found = _try_import_path(path)
            if found is not None:
                return found
        found = _try_import_name(env)
        if found is not None:
            return found

    for res in _app_resource_candidates():
        # Resources/zm4_install or Resources as package parent
        found = _try_import_path(res / "zm4_install")
        if found is not None:
            return found
        found = _try_import_path(res)
        if found is not None:
            return found

    app_support = (
        Path.home() / "Library" / "Application Support" / "ZM4" / "install-module"
    )
    found = _try_import_path(app_support)
    if found is not None:
        return found
    found = _try_import_path(app_support / "zm4_install")
    if found is not None:
        return found

    return _try_import_name("zm4_install")


def install_module_state() -> dict[str, Any]:
    """Snapshot for ``/api/state`` (safe when module missing)."""
    mod = load_install_module()
    if mod is None:
        return {
            "available": False,
            "version": "",
            "error": MISSING_MODULE_ERROR,
            "max_cameras_per_machine": 2,
            "bundled_vendor": False,
        }
    cap = {}
    try:
        cap = dict(mod.capability() or {})
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "version": "",
            "error": "安装模块 capability 失败：%s" % exc,
            "max_cameras_per_machine": 2,
            "bundled_vendor": False,
        }
    return {
        "available": True,
        "version": str(cap.get("version") or ""),
        "error": "",
        "max_cameras_per_machine": int(cap.get("max_cameras_per_machine") or 2),
        "bundled_vendor": bool(cap.get("bundled_vendor")),
        "capability": cap,
    }
