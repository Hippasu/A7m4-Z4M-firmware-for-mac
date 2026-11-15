"""One-call environment doctor: what is missing and how to fix it.

``m4probe --doctor`` and the workbench's "⓪ 环境自检" panel both render
:func:`check`. The result is intentionally UI-shaped: a flat list of checks with
``ok``, a human sentence, and a ``fix`` hint, so a failing check never leaves the
operator guessing which file to put where.
"""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Any

from . import discovery

MIN_PYTHON = (3, 9)
PACKAGES = ("usb.core", "usb.util", "libusb_package")


def _pkg_check(name: str) -> tuple[bool, str]:
    try:
        __import__(name)
        return True, "已安装"
    except Exception as exc:  # pragma: no cover - environment dependent
        return False, "缺失（%s）" % type(exc).__name__


def check(auth_backend: Path | None = None, pmca_dir: Path | None = None,
          backend: Any = None, scan: bool = True,
          persist: bool = True) -> dict[str, Any]:
    """Inspect the host and the two external dependencies.

    ``persist=True`` writes whatever discovery found into ``m4probe.local.json``
    so the next launch is instant and needs no flags at all.
    """
    resolved = discovery.resolve(auth_backend, pmca_dir, scan=scan)
    auth = resolved["auth_backend"]
    pmca = resolved["pmca_dir"]
    checks: list[dict[str, Any]] = []

    version_ok = tuple(sys.version_info[:2]) >= MIN_PYTHON
    checks.append({
        "id": "python",
        "label": "Python 解释器",
        "ok": version_ok,
        "detail": "%s（%s）" % (platform.python_version(), sys.executable),
        "fix": "" if version_ok else "需要 Python %d.%d 以上" % MIN_PYTHON,
    })

    missing = []
    for package in PACKAGES:
        ok, detail = _pkg_check(package)
        if not ok:
            missing.append(package)
    checks.append({
        "id": "pyusb",
        "label": "PyUSB / libusb 驱动包",
        "ok": not missing,
        "detail": "全部已安装" if not missing else "缺失：" + ", ".join(missing),
        "fix": "" if not missing else
               "在**同一个**解释器里安装：%s -m pip install pyusb libusb-package"
               % sys.executable,
    })

    auth_ok = bool(auth and (Path(auth) / discovery.AUTH_RELATIVE).is_file())
    checks.append({
        "id": "auth_backend",
        "label": "认证后端验证",
        "ok": auth_ok,
        "detail": str(auth) if auth_ok else "未找到",
        "fix": "" if auth_ok else
               "把上游 software 目录放进上面「认证后端」下拉，或设置 M4PROBE_AUTH_BACKEND",
    })

    pmca_ok = bool(pmca and all((Path(pmca) / n).is_file() for n in discovery.PMCA_FILES))
    checks.append({
        "id": "pmca",
        "label": "核心文件校验",
        "ok": pmca_ok,
        "detail": str(pmca) if pmca_ok else "未找到",
        "fix": "" if pmca_ok else
               "需要 constants.py 与 crypto.py（来自 Sony-PMCA-RE 的 pmca/usb）；"
               "把含这两个文件的目录粘进下拉框，或点「重新扫描」让它自己找",
    })

    bridge_ready = False
    bridge_detail = "跳过（上游文件未齐）"
    bridge_error = ""
    checks.append({
        "id": "bridge",
        "label": "专有模块（公开壳跳过）",
        "ok": True,
        "detail": "公开诊断壳不加载专有认证实现",
        "fix": "",
    })

    out_dir = discovery.PROJECT_ROOT / "out"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        probe_file = out_dir / ".write-test"
        probe_file.write_text("ok", encoding="utf-8")
        probe_file.unlink()
        writable, write_detail = True, str(out_dir)
    except OSError as exc:
        writable, write_detail = False, str(exc)
    checks.append({
        "id": "output",
        "label": "报告目录可写",
        "ok": writable,
        "detail": write_detail,
        "fix": "" if writable else "检查 out/ 的权限或磁盘空间",
    })

    camera: dict[str, Any] | None = None
    if backend is not None:
        try:
            devices = backend.enumerate_devices(fast=True)
            camera = {
                "count": len(devices),
                "labels": [d.label for d in devices][:6],
                "state": "CONNECTED" if devices else "ABSENT",
            }
        except Exception as exc:
            camera = {"count": 0, "labels": [], "state": "UNKNOWN",
                      "error": "%s: %s" % (type(exc).__name__, exc)}

    ready = all(c["ok"] for c in checks)
    first_bad = next((c for c in checks if not c["ok"]), None)
    if persist and (resolved["auth_backend"] or resolved["pmca_dir"]):
        discovery.save_config(auth_backend=resolved["auth_backend"],
                              pmca_dir=resolved["pmca_dir"])

    summary = ("电脑环境及后端文件已经就绪。"
               if ready else "环境未就绪：%s —— %s" % (first_bad["label"], first_bad["fix"]))
    return {
        "ready": ready,
        "summary": summary,
        "checks": checks,
        "camera": camera,
        "paths": {
            "auth_backend": str(auth) if auth else "",
            "pmca_dir": str(pmca) if pmca else "",
            "config": resolved["config_path"],
            "python": sys.executable,
            "cwd": os.getcwd(),
        },
        "candidates": {
            "auth_backend": resolved["auth_candidates"],
            "pmca_dir": resolved["pmca_candidates"],
        },
        "scanned": resolved["scanned"],
        "config_exists": Path(resolved["config_path"]).is_file(),
    }


def format_text(result: dict[str, Any]) -> str:
    """Terminal rendering for ``m4probe --doctor`` (no colour, copy-friendly)."""
    lines = ["环境自检 · " + ("就绪 ✅" if result["ready"] else "未就绪 ❌"), ""]
    for item in result["checks"]:
        mark = "✅" if item["ok"] else "❌"
        lines.append("%s %-34s %s" % (mark, item["label"], item["detail"]))
        if not item["ok"] and item["fix"]:
            lines.append("   → " + item["fix"])
    if result.get("camera"):
        cam = result["camera"]
        lines.append("")
        lines.append("相机：%s%s" % (cam["state"],
                                  "" if not cam["labels"] else " · " + "、".join(cam["labels"])))
    lines.append("")
    lines.append("认证后端: " + (result["paths"]["auth_backend"] or "—"))
    lines.append("PMCA 目录: " + (result["paths"]["pmca_dir"] or "—"))
    lines.append("配置文件: " + result["paths"]["config"]
                 + ("" if result.get("config_exists") else "（尚未写入）"))
    lines.append("")
    lines.append(result["summary"])
    if result["ready"]:
        lines.append("下一步：python -m m4probe.detect")
    return "\n".join(lines)
