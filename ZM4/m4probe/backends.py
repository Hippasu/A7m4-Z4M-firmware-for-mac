"""USB backends for the staged probe.

Two implementations:

* ``MacUsbBackend``  - real macOS host. Read-only stages need nothing but the
  OS's own ``system_profiler`` / ``ioreg``; the deeper stages need PyUSB.
* ``SimulatedBackend`` - deterministic fixtures so the whole stage chain, the
  gating logic and the verdict engine can be exercised without a camera.

No backend in this file ever writes to the camera. The only mutable operation
is ``claim_interface`` on the host, which is gated by ``allow_claim``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .model import (
    EXPECTED_PRODUCT,
    IOKIT_ERROR_NAMES,
    PID_NORMAL_MSC,
    PID_SERVICE,
    SONY_VID,
    DeviceInfo,
    Tier,
    name_error,
)


USB_STACK_HINT = (
    "S2/S4 需要 PyUSB + libusb-package。推荐项目内隔离环境，且装依赖的 pip 与启动程序的"
    "python 必须是同一个解释器："
    "python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt，"
    "之后一律用 .venv/bin/python 运行 m4probe。临时修复："
    'python3 -m pip install --user "pyusb>=1.2.1,<2" "libusb-package>=1.0.26,<2"。'
    "安装后可直接重跑本阶段；若仍提示缺失，重启控制台。"
    "详见 README.md「新机环境部署」。"
)


class BackendUnavailable(RuntimeError):
    """A stage needs a resource that is not configured or not installed."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


def backend_unavailable_evidence(exc: "BackendUnavailable") -> dict[str, Any]:
    """Actionable evidence for a missing dependency, shown by the debug console."""
    return {
        "dependency": "%s: %s" % (type(exc).__name__, exc),
        "hint": exc.hint or USB_STACK_HINT,
        "python": sys.executable,
        "note": "这是环境依赖缺失，不是相机或链路故障；本阶段记为 SKIP，闸门保持锁定。",
    }


_USB_BACKEND_CACHE: dict[tuple[str, ...], dict[str, Any]] = {}


def usb_stack_status() -> dict[str, Any]:
    """Cheap, read-only status of the optional USB stack (used by the console).

    Import probes are cheap; the libusb dylib lookup is cached and only redone
    when the package states change (e.g. after installing without a restart).
    """
    packages: dict[str, str] = {}
    for name in ("usb.core", "usb.util", "libusb_package"):
        try:
            __import__(name)
            packages[name] = "installed"
        except Exception as exc:
            packages[name] = "MISSING (%s)" % type(exc).__name__
    key = tuple(packages.values())
    if key not in _USB_BACKEND_CACHE:
        _USB_BACKEND_CACHE.clear()
        probe: dict[str, Any] = {"libusb_backend": False, "backend_error": ""}
        if packages["libusb_package"] == "installed":
            try:
                import libusb_package
                backend = libusb_package.get_libusb1_backend()
                probe["libusb_backend"] = backend is not None
                if backend is None:
                    probe["backend_error"] = "get_libusb1_backend() returned None"
            except Exception as exc:
                probe["backend_error"] = "%s: %s" % (type(exc).__name__, exc)
        _USB_BACKEND_CACHE[key] = probe
    probe = _USB_BACKEND_CACHE[key]
    installed = (all(v == "installed" for v in packages.values())
                 and probe["libusb_backend"])
    return {
        "installed": installed,
        "packages": packages,
        "libusb_backend": probe["libusb_backend"],
        "backend_error": probe["backend_error"],
        "hint": "" if installed else USB_STACK_HINT,
        "note": "S0-S1/S3 只需系统自带工具；S2/S4 起才需要本机 libusb。",
    }


class Backend:
    simulated = False
    tier_cap = Tier.HOST_ONLY

    def host_facts(self) -> dict[str, Any]:
        raise NotImplementedError

    def interface_owner(self, pid: int) -> dict[str, Any]:
        """Who holds interface 0 inside IOKit (read-only; no root needed)."""
        return {"ok": False, "note": "this backend does not report interface owners"}

    def enumerate_devices(self, fast: bool = False) -> list[DeviceInfo]:
        """``fast`` skips slow system_profiler and uses IORegistry only (polling)."""
        raise NotImplementedError

    def descriptor_probe(self, pid: int) -> dict[str, Any]:
        raise NotImplementedError

    def occupancy_probe(self, pid: int) -> dict[str, Any]:
        raise NotImplementedError

    def claim_probe(self, pid: int, allow_claim: bool) -> dict[str, Any]:
        raise NotImplementedError

    def capture_probe(self, pid: int, timeout_s: float = 30.0,
                      allow_detach: bool = False) -> dict[str, Any]:
        """Root-only: claim interface 0, then release it (detach is the fallback)."""
        raise NotImplementedError

    def transition_probe(self, timeout_s: float = 90.0,
                         allow_detach: bool = True) -> dict[str, Any]:
        """(see public diagnose shell)"""
        raise NotImplementedError

    def auth_preflight(self) -> dict[str, Any]:
        raise NotImplementedError

    def authenticate_normal(self) -> dict[str, Any]:
        raise NotImplementedError

    def authenticate_service(self) -> dict[str, Any]:
        raise NotImplementedError

    def product_info(self) -> dict[str, Any]:
        raise NotImplementedError

    def read_capability(self, address: int = 0, size: int = 16,
                        paths: list[str] | None = None) -> dict[str, Any]:
        """Read real data through the live service session (read-only).

        ``ok is None`` means the upstream channel exposes no data-read entry
        point, i.e. capability cannot be probed -- not that it failed.
        """
        raise NotImplementedError

    def stop_service(self) -> dict[str, Any]:
        raise NotImplementedError

    def release_service_handle(self) -> dict[str, Any]:
        """(see public diagnose shell)"""
        return {"ok": True, "released": False}

    def wait_for_pid(self, pid: int, timeout_s: float, poll_s: float = 0.5,
                     on_tick: Any = None) -> dict[str, Any]:
        """``on_tick(elapsed, pids)`` is called on every poll for live logging."""
        raise NotImplementedError

    def wait_for_libusb_pid(self, pid: int, timeout_s: float, poll_s: float = 0.5,
                            on_tick: Any = None) -> dict[str, Any]:
        """Wait until PyUSB can see ``054c:<pid>`` (stricter than IOKit enum).

        S7 restore often sees 0DA5 in IORegistry a beat before libusb lists it;
        A subsequent ``transition_probe`` then fails instantly on find.
        """
        raise NotImplementedError

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


# --------------------------------------------------------------------------
# macOS implementation
# --------------------------------------------------------------------------


def _run(cmd: list[str], timeout: float = 20.0) -> tuple[int, str, str]:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return done.returncode, done.stdout, done.stderr
    except FileNotFoundError:
        return 127, "", "missing command: " + cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "", "timeout after %.0fs: %s" % (timeout, " ".join(cmd))


def _err_text(exc: BaseException) -> str:
    return "%s: %s" % (type(exc).__name__, exc)


def _volumes() -> list[str]:
    """Names under /Volumes, used only as before/after evidence (read-only)."""
    try:
        return sorted(os.listdir("/Volumes"))
    except OSError:
        return []


def macos_version() -> str | None:
    """Short macOS product version (e.g. "15.4.1"); the seize behaviour differs."""
    code, out, _ = _run(["sw_vers", "-productVersion"], timeout=5)
    text = out.strip()
    return text if code == 0 and text else None


def _ioreg_blocks(output: str) -> list[dict[str, str]]:
    """Split ``ioreg -l`` output into per-object property dictionaries."""
    blocks: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in output.splitlines():
        if re.search(r"\+-o\s+\S+", line):
            current = {}
            blocks.append(current)
            continue
        if current is None:
            continue
        match = re.match(r'\s*[|+\s]*"([^"]+)"\s*=\s*(.+?)\s*$', line)
        if match:
            current[match.group(1)] = match.group(2)
    return blocks


def interface_owner(pid: int) -> dict[str, Any]:
    """Who holds interface 0 *inside IOKit* — readable without root.

    ``UsbExclusiveOwner`` is set by IOUSBHostFamily when a client opens the
    interface exclusively. For an MSC camera it names the in-kernel
    ``IOUSBMassStorageInterfaceNub``, which explains a claim that fails with
    ``LIBUSB_ERROR_ACCESS`` even from a root process: seizing the *device*
    does not release an interface that another (kernel) client opened
    exclusively.
    """
    code, out, err = _run(["ioreg", "-r", "-c", "IOUSBHostInterface", "-l", "-w", "0"],
                          timeout=15)
    if code != 0:
        return {"ok": False, "error": (err or out).strip()[:200], "pid": hex(pid)}
    for block in _ioreg_blocks(out):
        if block.get("bInterfaceNumber") != "0":
            continue
        product = block.get("USB Product Name", "").strip('"')
        if product and EXPECTED_PRODUCT not in product and "ILCE" not in product:
            continue
        owner = block.get("UsbExclusiveOwner", "").strip('"') or None
        return {"ok": True, "pid": hex(pid), "interface": 0, "product": product,
                "usb_exclusive_owner": owner, "exclusive": owner is not None,
                "note": ("接口 0 已被 %s 以独占方式打开：root 的 USBDeviceOpenSeize 只抢设备，"
                         "不会让这个接口持有者松手——claim 仍会返回 ACCESS，必须先 detach"
                         % owner) if owner else "接口 0 没有独占持有者"}
    return {"ok": True, "pid": hex(pid), "interface": 0,
            "usb_exclusive_owner": None, "exclusive": False,
            "note": "ioreg 里没找到接口 0（设备可能不在 MSC 模式）"}


def _libusb_find(pid: int) -> Any:
    """Locate 054c:<pid> through PyUSB, preferring the bundled libusb."""
    try:
        import usb.core
        try:
            import libusb_package
            backend = libusb_package.get_libusb1_backend()
            finder = libusb_package
        except ImportError:
            backend = None
            finder = usb.core
        return finder.find(idVendor=SONY_VID, idProduct=pid, backend=backend)
    except ImportError as exc:
        raise BackendUnavailable(
            "PyUSB/libusb-package not installed: %s" % exc, USB_STACK_HINT) from exc


def _parse_int(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    match = re.search(r"0x[0-9a-fA-F]+", value)
    if match:
        return int(match.group(0), 16)
    match = re.search(r"-?\d+", value)
    return int(match.group(0)) if match else None


def _walk_spusb(node: Any, out: list[dict[str, Any]]) -> None:
    """system_profiler nests child hubs under ``_items`` / ``Media`` / lists."""
    if isinstance(node, dict):
        if "vendor_id" in node or "product_id" in node:
            out.append(node)
        for key, value in node.items():
            if key in ("_items", "Media", "volumes", "devices") or isinstance(value, (list, dict)):
                _walk_spusb(value, out)
    elif isinstance(node, list):
        for item in node:
            _walk_spusb(item, out)


_IOREG_KEYS = {
    "vid": "idVendor",
    "pid": "idProduct",
    "location": "locationID",
    "session": "sessionID",
    "serial": "USB Serial Number",
    "product": "USB Product Name",
    "vendor": "USB Vendor Name",
}


def _parse_ioreg(text: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        if line.lstrip().startswith("+-o "):
            if current and current.get("vid") is not None:
                blocks.append(current)
            current = {}
            continue
        if current is None:
            continue
        for short, key in _IOREG_KEYS.items():
            match = re.search(r'"%s"\s*=\s*(.+)' % re.escape(key), line)
            if match:
                raw = match.group(1).strip().strip(",")
                if short in ("vid", "pid", "location", "session"):
                    current[short] = _parse_int(raw)
                else:
                    current[short] = raw.strip('"')
    if current and current.get("vid") is not None:
        blocks.append(current)
    return blocks


class MacUsbBackend(Backend):
    tier_cap = Tier.DEVICE_COMMANDS

    def __init__(self, auth_backend: Path | None = None, pmca_dir: Path | None = None) -> None:
        self.auth_backend = auth_backend
        self.pmca_dir = pmca_dir
        self._bridge: Any = None
        self._spusb_cache: str | None = None

    # -- host -------------------------------------------------------------
    def host_facts(self) -> dict[str, Any]:
        facts: dict[str, Any] = {"os": "macOS", "tools": {}, "python_packages": {}}
        code, out, err = _run(["sw_vers"])
        facts["sw_vers"] = out.strip() if code == 0 else err.strip()
        code, out, err = _run(["uname", "-m"])
        facts["arch"] = out.strip() if code == 0 else err.strip()
        code, out, err = _run(["csrutil", "status"], timeout=8)
        facts["sip"] = out.strip() if code == 0 else "unavailable: " + err.strip()
        for tool in ("system_profiler", "ioreg", "diskutil", "brew"):
            code, out, err = _run(["which", tool], timeout=5)
            facts["tools"][tool] = out.strip() if code == 0 else "MISSING"
        for package in ("usb.core", "usb.util", "libusb_package"):
            try:
                __import__(package)
                facts["python_packages"][package] = "installed"
            except Exception as exc:  # pragma: no cover - environment dependent
                facts["python_packages"][package] = "MISSING (%s)" % type(exc).__name__
        code, out, err = _run(["systemextensionsctl", "list"], timeout=10)
        facts["system_extensions"] = out.strip()[:2000] if code == 0 else "unavailable"
        return facts

    # -- enumeration ------------------------------------------------------
    def _spusb_text(self) -> str:
        if self._spusb_cache is None:
            code, out, err = _run(["system_profiler", "-json", "SPUSBDataType"], timeout=45)
            self._spusb_cache = out if code == 0 else ""
            if code != 0:
                self._spusb_error = err.strip()
        return self._spusb_cache

    def enumerate_devices(self, fast: bool = False) -> list[DeviceInfo]:
        found: list[DeviceInfo] = []
        text = "" if fast else self._spusb_text()
        if text:
            try:
                rows: list[dict[str, Any]] = []
                _walk_spusb(json.loads(text), rows)
                for row in rows:
                    vid = _parse_int(row.get("vendor_id"))
                    pid = _parse_int(row.get("product_id"))
                    if vid is None or pid is None:
                        continue
                    # Keep every USB device: the detector must be able to prove
                    # WHY a disk/hub is not a camera, not just hide it.
                    found.append(DeviceInfo(
                        vid=vid, pid=pid,
                        location_id=str(row.get("location_id", "")),
                        product=str(row.get("_name", "")),
                        serial=str(row.get("serial_num", "")),
                        manufacturer=str(row.get("manufacturer", "")),
                        source="system_profiler",
                    ))
            except (ValueError, TypeError) as exc:
                # Sentinel for a parse failure. Use a VID that cannot be Sony:
                # tagging it 054C would make the gatekeeper report a phantom
                # "other Sony device" and even suggest switching USB mode.
                found.append(DeviceInfo(vid=0x0000, pid=0, source="parse_error:%s" % exc))
        code, out, _ = _run(["ioreg", "-r", "-c", "IOUSBHostDevice", "-l", "-w", "0"], timeout=20)
        if code != 0 or not out.strip():
            code, out, _ = _run(["ioreg", "-r", "-c", "IOUSBDevice", "-l", "-w", "0"], timeout=20)
        for row in _parse_ioreg(out):
            if row.get("vid") is None or row.get("pid") is None:
                continue
            match = next((d for d in found if d.vid == row["vid"] and d.pid == row["pid"]), None)
            if match is None:
                # IORegistry is the ONLY source in fast mode. Never assume a
                # Sony VID here: doing so made every USB device on the host
                # (keyboards, disks, iPhones...) look like "another Sony
                # device" to the gatekeeper and produced bogus mode warnings.
                found.append(DeviceInfo(
                    vid=row["vid"], pid=row["pid"],
                    location_id=str(row.get("location", "")),
                    session_id=str(row.get("session", "")),
                    product=row.get("product"), serial=row.get("serial"),
                    manufacturer=row.get("vendor"), kernel_owner="IOUSBHostDevice",
                    source="ioreg",
                ))
            else:
                match.location_id = match.location_id or str(row.get("location", ""))
                match.session_id = str(row.get("session", ""))
                match.serial = match.serial or row.get("serial")
                match.product = match.product or row.get("product")
                match.kernel_owner = "IOUSBHostDevice"
        return found

    # -- standard USB descriptor reads ------------------------------------
    def descriptor_probe(self, pid: int) -> dict[str, Any]:
        try:
            import usb.core
            import usb.util
            try:
                import libusb_package
                backend = libusb_package.get_libusb1_backend()
                finder = libusb_package
            except ImportError:
                backend = None
                finder = usb.core
            dev = finder.find(idVendor=SONY_VID, idProduct=pid, backend=backend)
        except ImportError as exc:
            raise BackendUnavailable(
                "PyUSB/libusb-package not installed: %s" % exc, USB_STACK_HINT
            ) from exc
        result: dict[str, Any] = {"pid": hex(pid), "backend": "pyusb+libusb"}
        if dev is None:
            result["present"] = False
            return result
        result["present"] = True
        result["bus"], result["address"] = dev.bus, dev.address
        try:
            langids = tuple(usb.util.get_langids(dev))
            result["langids"] = [hex(x) for x in langids]
            if langids:
                result["product"] = usb.util.get_string(dev, dev.iProduct, langid=langids[0])
                result["serial_sha256"] = _serial_digest(usb.util.get_string(dev, dev.iSerialNumber, langid=langids[0]))
                result["model_ok"] = result.get("product") == EXPECTED_PRODUCT
            cfg = dev.get_active_configuration()
            intf = cfg[0, 0]
            result["interface_0"] = {
                "class": intf.bInterfaceClass,
                "subclass": intf.bInterfaceSubClass,
                "protocol": intf.bInterfaceProtocol,
                "endpoints": [hex(e.bEndpointAddress) for e in intf],
            }
        except Exception as exc:
            result["error"] = "%s: %s" % (type(exc).__name__, exc)
            result["errno"] = getattr(exc, "errno", None)
            result["backend_error_code"] = getattr(exc, "backend_error_code", None)
            result["error_name"] = name_error(getattr(exc, "backend_error_code", None))
        finally:
            try:
                usb.util.dispose_resources(dev)
            except Exception:
                pass
        return result

    # -- who owns the interface -------------------------------------------
    def interface_owner(self, pid: int) -> dict[str, Any]:
        """IOKit's ``UsbExclusiveOwner`` for interface 0 (module-level helper)."""
        return interface_owner(pid)

    def occupancy_probe(self, pid: int) -> dict[str, Any]:
        code, out, err = _run(["ioreg", "-l", "-w", "0"], timeout=25)
        evidence: dict[str, Any] = {"ioreg_ok": code == 0, "pid": hex(pid)}
        if code != 0:
            evidence["error"] = err.strip()
            return evidence
        classes = ("IOUSBMassStorageDriver", "IOUSBMassStorageDriverNub",
                   "IOUSBHostInterface", "IOBlockStorageDriver", "IOUSBHostDevice")
        evidence["class_hits"] = {name: out.count(name) for name in classes}
        evidence["mass_storage_driver_present"] = evidence["class_hits"]["IOUSBMassStorageDriver"] > 0
        evidence["kernel_claims_likely"] = (
            evidence["mass_storage_driver_present"]
            and evidence["class_hits"]["IOBlockStorageDriver"] > 0
        )
        evidence["iokit_no_resources_constant"] = hex(
            next(k for k, v in IOKIT_ERROR_NAMES.items() if v == "kIOReturnNoResources")
        )
        return evidence

    # -- libusb exclusive claim -------------------------------------------
    def claim_probe(self, pid: int, allow_claim: bool) -> dict[str, Any]:
        try:
            import usb.core
            import usb.util
            try:
                import libusb_package
                backend = libusb_package.get_libusb1_backend()
                finder = libusb_package
            except ImportError:
                backend = None
                finder = usb.core
            dev = finder.find(idVendor=SONY_VID, idProduct=pid, backend=backend)
        except ImportError as exc:
            raise BackendUnavailable(
                "PyUSB/libusb-package not installed: %s" % exc, USB_STACK_HINT
            ) from exc
        result: dict[str, Any] = {"pid": hex(pid), "allow_claim": allow_claim}
        if dev is None:
            result["present"] = False
            return result
        result["present"] = True
        result["euid"] = os.geteuid()
        result["interface_owner"] = interface_owner(pid)
        owner = (result["interface_owner"].get("usb_exclusive_owner")
                 if result["interface_owner"].get("ok") else None)
        try:
            try:
                result["kernel_driver_active"] = bool(dev.is_kernel_driver_active(0))
            except (NotImplementedError, AttributeError) as exc:
                result["kernel_driver_active"] = "not_supported_on_darwin"
                result["kernel_driver_note"] = type(exc).__name__
            if allow_claim:
                try:
                    usb.util.claim_interface(dev, 0)
                    result["claim"] = "OK"
                    usb.util.release_interface(dev, 0)
                    result["release"] = "OK"
                except Exception as exc:
                    result["claim"] = "FAILED"
                    result["claim_error"] = "%s: %s" % (type(exc).__name__, exc)
                    result["error_name"] = name_error(getattr(exc, "backend_error_code", None))
                    result["error_code"] = getattr(exc, "backend_error_code", None)
                    if result["error_name"] == "LIBUSB_ERROR_ACCESS":
                        # Darwin: USBDeviceOpenSeize is the only open path libusb
                        # has. Two independent reasons can leave the interface
                        # unclaimed, and they need different next steps:
                        #   1. euid != 0  -> the seize itself was refused;
                        #   2. an IOKit client holds the interface exclusively
                        #      (UsbExclusiveOwner, e.g. IOUSBMassStorageInterfaceNub)
                        #      -> seizing the *device* does not make it let go,
                        #      so even root's plain claim fails; detach is the
                        #      only rung that works. Measured on this Mac
                        #      (macOS 15.4.1, 2026-11-14).
                        non_root = result["euid"] != 0
                        result["failure_scope"] = ("non_root_refusal" if non_root
                                                   else "kernel_refused_even_as_root")
                        result["failure_stage"] = "claim_interface/USBInterfaceOpen"
                        if non_root:
                            hint = ("非 root（euid=%d）：USBDeviceOpenSeize 在 darwin_open 里就失败，"
                                    "libusb 不持有句柄，claim 因此卡在 USBInterfaceOpen。"
                                    "root 是否够用取决于系统版本，先跑 root root capture 验证。"
                                    % result["euid"])
                        else:
                            result["detach_required"] = True
                            hint = ("root 下仍被拒：%s。USBDeviceOpenSeize 只抢设备，"
                                    "不会让独占该接口的内核客户端松手，所以 plain claim 必然失败；"
                                    "下一步是 detach 兜底（--accept-disconnect，会重新枚举）。"
                                    "本机 2026-10-25 走通的正是「root + detach」这条路。"
                                    % ("接口 0 由 %s 独占" % owner if owner
                                       else "接口 0 有另一个独占持有者（见 interface_owner）"))
                        result["capture_hint"] = hint
                        result["capture_verified"] = False
            else:
                result["claim"] = "NOT_ATTEMPTED"
                result["claim_note"] = "pass --allow-claim to test exclusive interface ownership"
        finally:
            try:
                usb.util.dispose_resources(dev)
            except Exception:
                pass
        return result

    # -- root capture: claim first, detach only as a fallback ----------------
    def capture_probe(self, pid: int, timeout_s: float = 30.0,
                      allow_detach: bool = False) -> dict[str, Any]:
        """Prove or disprove interface-0 capture from a root process.

        Two rungs, cheapest first.

        **Rung 1, claim only (default).** ``darwin_open`` opens the device with
        ``USBDeviceOpenSeize``. As a normal user the seize is refused and libusb
        ``USBInterfaceOpen``. Root alone is **not** sufficient on every macOS
        build: if another IOKit client already holds interface 0 exclusively
        (``UsbExclusiveOwner`` = ``IOUSBMassStorageInterfaceNub`` on this Mac),
        seizing the *device* does not make that client let go and the plain
        claim still returns ``LIBUSB_ERROR_ACCESS``. Measured here on macOS
        15.4.1 (2026-11-14): root + plain claim = ACCESS.

        **Rung 2, detach fallback (``allow_detach``).** ``darwin_detach_kernel_driver``
        asks IOKit to re-enumerate in capture mode
        (``kUSBReEnumerateCaptureDeviceMask``), which makes the kernel client
        release the interface, and ``attach_kernel_driver`` on the way out is a
        real re-enumeration (bus address may change). This is the rung that is
        actually proven on this Mac: the 2026-10-25 root probe called
        ``detach_kernel_driver(0)`` first ("内核驱动已分离") and only then read
        MSC data and reached ``054c:0336``. Strictly more invasive, so it stays
        opt-in.

        Neither rung issues a control request or a bulk transfer.
        """
        usb_util: Any = None
        result: dict[str, Any] = {
            "pid": hex(pid), "euid": os.geteuid(), "allow_detach": allow_detach,
            "volumes_before": _volumes(), "steps": [], "ok": False,
            "macos": macos_version(), "interface_owner": interface_owner(pid),
        }
        owner = (result["interface_owner"].get("usb_exclusive_owner")
                 if result["interface_owner"].get("ok") else None)
        if owner:
            result["interface_exclusive_owner"] = owner

        def note(step: str, **fields: Any) -> dict[str, Any]:
            row: dict[str, Any] = {"step": step}
            row.update(fields)
            result["steps"].append(row)
            return row

        if os.geteuid() != 0:
            result["needs_root"] = True
            note("root", ok=False,
                 error="euid=%d；root 才让 USBDeviceOpenSeize 抢到设备"
                       "（detach 亦同：无 com.apple.vm.device-access entitlement 时"
                       "darwin_detach_kernel_driver 原样返回 LIBUSB_ERROR_ACCESS）"
                       % os.geteuid())
            return result

        dev = _libusb_find(pid)
        if dev is None:
            result["present"] = False
            note("find", ok=False, error="libusb 看不到 %04x:%04x" % (SONY_VID, pid))
            return result
        result["present"] = True
        import usb.util as usb_util
        result["bus_before"], result["address_before"] = dev.bus, dev.address
        try:
            langids = tuple(usb_util.get_langids(dev))
            if langids:
                result["product"] = usb_util.get_string(dev, dev.iProduct, langid=langids[0])
                result["serial_sha256"] = _serial_digest(
                    usb_util.get_string(dev, dev.iSerialNumber, langid=langids[0]))
        except Exception as exc:
            note("identity", ok=False, error=_err_text(exc))

        claimed = False

        def try_claim(mode: str, **extra: Any) -> bool:
            nonlocal claimed
            try:
                usb_util.claim_interface(dev, 0)
            except Exception as exc:
                code = getattr(exc, "backend_error_code", None)
                result["claim"] = "FAILED"
                result["error_name"] = name_error(code)
                note("claim", ok=False, mode=mode, error=_err_text(exc),
                     error_name=result["error_name"], error_code=code)
                return False
            claimed = True
            result["claim"] = "OK"
            result["capture_mode"] = mode
            note("claim", ok=True, mode=mode, **extra)
            return True

        detached = False
        if not try_claim("claim_only",
                         note="root 下 USBDeviceOpenSeize 抢占设备后直接 claim："
                              "未触发重新枚举"):
            if not allow_detach:
                result["detach_skipped"] = True
                result["detach_recommended"] = True
                why = ("接口 0 由 %s 独占，seize 不会让它松手" % owner) if owner else \
                      "接口 0 仍被其他客户端独占"
                note("detach", ok=False, skipped=True,
                     error="claim_only 被拒：%s；detach 是这台机器的第二条（也是实证过的）"
                           "路径，但需要 --accept-disconnect 显式放行" % why)
            else:
                try:
                    dev.detach_kernel_driver(0)
                    detached = True
                    note("detach", ok=True,
                         note="capture 模式重新枚举：内核客户端松开接口；"
                              "结束时的 attach 会真正重新枚举一次（总线地址可能变化，卷可能卸载）")
                except (NotImplementedError, AttributeError) as exc:
                    note("detach", ok=False,
                         error="此后端没有 detach_kernel_driver：%s" % type(exc).__name__)
                except Exception as exc:
                    code = getattr(exc, "backend_error_code", None)
                    result["error_name"] = name_error(code)
                    note("detach", ok=False, error=_err_text(exc),
                         error_name=result["error_name"], error_code=code)
                else:
                    try_claim("detach_then_claim")

        if claimed:
            try:
                usb_util.release_interface(dev, 0)
                result["release"] = "OK"
                note("release", ok=True)
            except Exception as exc:
                result["release"] = "FAILED"
                note("release", ok=False, error=_err_text(exc))
            claimed = False

        if detached:
            try:
                dev.attach_kernel_driver(0)
                result["attach"] = "OK"
                note("attach", ok=True)
            except Exception as exc:
                result["attach"] = "FAILED"
                note("attach", ok=False, error=_err_text(exc))

        try:
            usb_util.dispose_resources(dev)
        except Exception:
            pass

        again = None
        deadline = time.time() + max(1.0, timeout_s)
        while time.time() < deadline:
            again = _libusb_find(pid)
            if again is not None:
                break
            time.sleep(0.5)
        result["present_after"] = again is not None
        if again is not None:
            result["bus_after"], result["address_after"] = again.bus, again.address
            result["reenumerated"] = (result.get("bus_before") != again.bus
                                      or result.get("address_before") != again.address)
            try:
                usb_util.dispose_resources(again)
            except Exception:
                pass
        # Volumes come back asynchronously after the attach re-enumeration. A
        # snapshot taken the instant ``attach`` returns reads "PMHOME vanished",
        # which scares the operator even though macOS remounts a moment later —
        # so wait briefly for the pre-run set to reappear and record how long.
        before_vols = set(result.get("volumes_before") or [])
        settle_start = time.time()
        settle_deadline = settle_start + 8.0
        vols = _volumes()
        while time.time() < settle_deadline and not before_vols <= set(vols):
            time.sleep(0.5)
            vols = _volumes()
        result["volumes_after"] = vols
        result["volumes_settle_s"] = round(time.time() - settle_start, 1)
        result["volumes_recovered"] = before_vols <= set(vols)
        result["volumes_lost"] = sorted(before_vols - set(vols))
        # Without a detach there is nothing to attach, so only a detach run is
        # allowed to report a failed attach.
        result["ok"] = (result.get("claim") == "OK"
                        and result.get("attach", "OK") == "OK")
        return result

    # -- reviewed authentication backend ----------------------------------
    def _get_bridge(self) -> Any:
        if self._bridge is None:
            if self.auth_backend is None:
                raise BackendUnavailable(
                    "auth need --auth-backend pointing at no-fastboot-install/software "
                    "from the upstream checkout"
                )
            raise BackendUnavailable("proprietary auth path not in public tree")
        return self._bridge

    def auth_preflight(self) -> dict[str, Any]:
        if self.auth_backend is None:
            return {"ready": False, "reason": "no --auth-backend supplied"}
        return {"ready": False, "reason": "proprietary auth path not in public tree"}

    def authenticate_normal(self) -> dict[str, Any]:
        return self._get_bridge().authenticate_normal()

    def authenticate_service(self) -> dict[str, Any]:
        return self._get_bridge().authenticate_service()

    def product_info(self) -> dict[str, Any]:
        return self._get_bridge().product_info()

    def read_capability(self, address: int = 0, size: int = 16,
                        paths: list[str] | None = None) -> dict[str, Any]:
        return self._get_bridge().read_capability(address, size, paths=paths)

    def release_service_handle(self) -> dict[str, Any]:
        """

        process then hits LIBUSB_ERROR_ACCESS. Upstream keeps one process; we
        """
        if self._bridge is None:
            return {"ok": True, "released": False, "reason": "no_bridge"}
        had = self._bridge.driver is not None
        try:
            self._bridge.release_driver()
        except Exception as exc:
            return {"ok": False, "released": False, "error": str(exc)}
        return {"ok": True, "released": had}

    def stop_service(self) -> dict[str, Any]:
        return self._get_bridge().stop_service()

    def _detach_msc_if_needed(self, pid: int, allow_detach: bool) -> dict[str, Any]:
        """Make interface 0 writable for bulk/control (root + detach on this Mac).

        Mirrors the 2026-10-25 probe: ``detach_kernel_driver(0)`` first, then
        any later pyusb ``write``/``claim_interface`` can succeed. Without this,
        console — the same wall S4 measures.
        """
        step: dict[str, Any] = {"step": "detach", "pid": hex(pid)}
        if os.geteuid() != 0:
            step.update(ok=False, error="euid=%d；detach 需要 root" % os.geteuid())
            return step
        dev = _libusb_find(pid)
        if dev is None:
            step.update(ok=False, error="libusb 看不到 054c:%04x" % pid)
            return step
        try:
            try:
                active = bool(dev.is_kernel_driver_active(0))
            except (NotImplementedError, AttributeError, Exception) as exc:
                step.update(ok=False, error="无法查询内核驱动：%s" % _err_text(exc))
                return step
            step["kernel_driver_active"] = active
            if not active:
                step.update(ok=True, skipped=True, note="接口 0 没有内核驱动占用，无需 detach")
                return step
            if not allow_detach:
                step.update(ok=False, skipped=True,
                            error="内核驱动占用接口 0，但未授权 detach（需要 --accept-disconnect）")
                return step
            try:
                dev.detach_kernel_driver(0)
                step.update(ok=True, note="内核驱动已分离（与 2026-10-25 实证路径一致）")
            except Exception as exc:
                code = getattr(exc, "backend_error_code", None)
                step.update(ok=False, error=_err_text(exc),
                            error_name=name_error(code), error_code=code)
            return step
        finally:
            try:
                import usb.util
                usb.util.dispose_resources(dev)
            except Exception:
                pass

    def transition_probe(self, timeout_s: float = 90.0,
                         allow_detach: bool = True) -> dict[str, Any]:
        """

        (the path that reached 0336 on 2026-10-25). Nothing here writes to the
        camera beyond the reversible Senser auth that changes USB identity.
        """
        result: dict[str, Any] = {
            "pid_before": hex(PID_NORMAL_MSC), "pid_after": None,
            "euid": os.geteuid(), "allow_detach": allow_detach,
            "macos": macos_version(), "steps": [], "ok": False,
            "volumes_before": _volumes(),
            "interface_owner": interface_owner(PID_NORMAL_MSC),
        }
        owner = (result["interface_owner"].get("usb_exclusive_owner")
                 if isinstance(result["interface_owner"], dict)
                 and result["interface_owner"].get("ok") else None)
        if owner:
            result["interface_exclusive_owner"] = owner

        def note(step: str, **fields: Any) -> dict[str, Any]:
            row: dict[str, Any] = {"step": step}
            row.update(fields)
            result["steps"].append(row)
            return row

        if os.geteuid() != 0:
            result["needs_root"] = True
            note("root", ok=False,
                 error="euid=%d；auth 的 auth 需要 claim 接口 0，普通用户必然 ACCESS"
                       % os.geteuid())
            return result

        # IOKit may already show 0DA5 while libusb still lags after restore.
        find_timeout = max(5.0, min(45.0, float(timeout_s) if timeout_s else 30.0))
        libusb_wait = self.wait_for_libusb_pid(PID_NORMAL_MSC, find_timeout, 0.4)
        result["libusb_wait"] = {
            "appeared": libusb_wait.get("appeared"),
            "elapsed_s": libusb_wait.get("elapsed_s"),
        }
        if not libusb_wait.get("appeared"):
            result["present"] = False
            note("find", ok=False,
                 error="libusb 在 %.0fs 内看不到 054c:0da5（相机未处于 MSC，或恢复后尚未对 libusb 可见）"
                       % find_timeout,
                 wait=result["libusb_wait"])
            return result
        note("find", ok=True, elapsed_s=libusb_wait.get("elapsed_s"),
             waited=bool((libusb_wait.get("elapsed_s") or 0) > 0.2))
        dev = _libusb_find(PID_NORMAL_MSC)
        if dev is None:
            result["present"] = False
            note("find_recheck", ok=False, error="libusb 刚报可见后又消失")
            return result
        result["present"] = True
        result["bus_before"], result["address_before"] = dev.bus, dev.address
        try:
            import usb.util as usb_util
            langids = tuple(usb_util.get_langids(dev))
            if langids:
                result["product"] = usb_util.get_string(
                    dev, dev.iProduct, langid=langids[0])
                result["serial_sha256"] = _serial_digest(
                    usb_util.get_string(dev, dev.iSerialNumber, langid=langids[0]))
            usb_util.dispose_resources(dev)
        except Exception as exc:
            note("identity", ok=False, error=_err_text(exc))

        detach = self._detach_msc_if_needed(PID_NORMAL_MSC, allow_detach=allow_detach)
        result["steps"].append(detach)
        if not detach.get("ok"):
            result["error_name"] = detach.get("error_name") or "DETACH_REQUIRED"
            result["detach_skipped"] = bool(detach.get("skipped"))
            result["error"] = detach.get("error")
            return result
        result["detached"] = not bool(detach.get("skipped"))

        try:
            auth = self.authenticate_normal()
        except Exception as exc:
            note("auth", ok=False, error=_err_text(exc))
            result["error"] = _err_text(exc)
            result["error_name"] = type(exc).__name__
            result["auth"] = {"ok": False, "error": _err_text(exc)}
            return result
        note("auth", ok=True, mode=auth.get("mode"), rounds=auth.get("rounds"))
        result["auth"] = auth

        wait = self.wait_for_pid(PID_SERVICE, timeout_s)
        result["wait"] = wait
        note("wait_0336", ok=bool(wait.get("appeared")),
             elapsed_s=wait.get("elapsed_s"),
             observations=(wait.get("observations") or [])[-5:])
        if not wait.get("appeared"):
            result["error"] = "0336 did not appear within %.0fs" % timeout_s
            result["error_name"] = "NO_0336"
            return result

        services = [d for d in self.enumerate_devices(fast=True) if d.pid == PID_SERVICE]
        if services:
            svc = services[0]
            result["pid_after"] = hex(PID_SERVICE)
            result["service_location_id"] = getattr(svc, "location_id", None)
            # Fast ioreg enumeration often omits libusb bus/address; fill from
            # a direct libusb find so reenumerated / port fallbacks stay honest.
            bus, addr = svc.bus, svc.address
            if bus is None or addr is None:
                again = _libusb_find(PID_SERVICE)
                if again is not None:
                    bus = again.bus if bus is None else bus
                    addr = again.address if addr is None else addr
                    try:
                        import usb.util
                        usb.util.dispose_resources(again)
                    except Exception:
                        pass
            result["bus_after"] = bus
            result["address_after"] = addr
            if (result.get("bus_before") is not None and bus is not None
                    and result.get("address_before") is not None and addr is not None):
                result["reenumerated"] = (
                    result.get("bus_before") != bus
                    or result.get("address_before") != addr)
            else:
                result["reenumerated"] = None
        result["volumes_after"] = _volumes()
        result["ok"] = True
        return result

    def wait_for_pid(self, pid: int, timeout_s: float, poll_s: float = 0.5,
                     on_tick: Any = None) -> dict[str, Any]:
        started = time.monotonic()
        observations: list[dict[str, Any]] = []
        while True:
            pids = sorted({d.pid for d in self.enumerate_devices(fast=True)})
            elapsed = round(time.monotonic() - started, 2)
            observations.append({"t": elapsed, "pids": [hex(p) for p in pids]})
            if on_tick is not None:
                on_tick(elapsed, [hex(p) for p in pids])
            if pid in pids:
                return {"appeared": True, "elapsed_s": round(time.monotonic() - started, 2),
                        "observations": observations[-20:]}
            if time.monotonic() - started >= timeout_s:
                return {"appeared": False, "elapsed_s": round(time.monotonic() - started, 2),
                        "observations": observations[-20:]}
            self.sleep(poll_s)

    def wait_for_libusb_pid(self, pid: int, timeout_s: float, poll_s: float = 0.5,
                            on_tick: Any = None) -> dict[str, Any]:
        started = time.monotonic()
        observations: list[dict[str, Any]] = []
        while True:
            dev = _libusb_find(pid)
            seen = dev is not None
            bus = getattr(dev, "bus", None) if seen else None
            addr = getattr(dev, "address", None) if seen else None
            elapsed = round(time.monotonic() - started, 2)
            row = {"t": elapsed, "libusb": seen,
                   "bus": bus, "address": addr, "pid": hex(pid)}
            observations.append(row)
            if on_tick is not None:
                on_tick(elapsed, [hex(pid)] if seen else [])
            if seen:
                return {"appeared": True, "elapsed_s": elapsed,
                        "bus": bus, "address": addr,
                        "observations": observations[-20:]}
            if time.monotonic() - started >= timeout_s:
                return {"appeared": False, "elapsed_s": elapsed,
                        "observations": observations[-20:]}
            self.sleep(poll_s)


def _serial_digest(serial: str | None) -> str | None:
    import hashlib
    if not serial:
        return None
    return hashlib.sha256(serial.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------

SCENARIOS = ("healthy", "no-resources", "no-transition", "flaky", "restore-fail",
             "capture-denied", "no-privilege", "root-denied", "hasp-degraded",
             "hasp-blocking")


class SimulatedBackend(Backend):
    """Deterministic stand-in for CI, demos and gating-logic tests."""

    simulated = True
    tier_cap = Tier.DEVICE_COMMANDS

    def __init__(self, scenario: str = "healthy", clock_scale: float = 0.0) -> None:
        if scenario not in SCENARIOS:
            raise ValueError("unknown scenario %r; choose from %s" % (scenario, ", ".join(SCENARIOS)))
        self.scenario = scenario
        self.clock_scale = clock_scale
        self.pid = PID_NORMAL_MSC
        self.cycles = 0
        self.service_waits = 0
        self.claim_attempts = 0

    def host_facts(self) -> dict[str, Any]:
        return {
            "os": "macOS (simulated)", "arch": "arm64", "sw_vers": "simulated 27.3",
            "sip": "enabled (simulated)", "scenario": self.scenario,
            "tools": {t: "/usr/bin/" + t for t in ("system_profiler", "ioreg", "diskutil")},
            "python_packages": {p: "installed (simulated)" for p in ("usb.core", "usb.util", "libusb_package")},
            "system_extensions": "simulated",
        }

    def enumerate_devices(self, fast: bool = False) -> list[DeviceInfo]:
        return [DeviceInfo(
            vid=SONY_VID, pid=self.pid, bus=1, address=3,
            location_id="0x01100000 / 1", session_id="sim-session",
            product=EXPECTED_PRODUCT, serial="SIM-BODY-0001",
            manufacturer="Sony Corporation",
            kernel_owner="IOUSBMassStorageDriver" if self.pid == PID_NORMAL_MSC else None,
            source="simulated",
        )]

    def descriptor_probe(self, pid: int) -> dict[str, Any]:
        return {"pid": hex(pid), "present": True, "bus": 1, "address": 3,
                "langids": ["0x409"], "product": EXPECTED_PRODUCT,
                "serial_sha256": _serial_digest("SIM-BODY-0001"), "model_ok": True,
                "interface_0": {"class": 8, "subclass": 6, "protocol": 80,
                                "endpoints": ["0x81", "0x2"]},
                "simulated": True}

    def occupancy_probe(self, pid: int) -> dict[str, Any]:
        return {"pid": hex(pid), "ioreg_ok": True,
                "class_hits": {"IOUSBMassStorageDriver": 1, "IOUSBHostInterface": 1,
                               "IOBlockStorageDriver": 1, "IOUSBMassStorageDriverNub": 1,
                               "IOUSBHostDevice": 1},
                "mass_storage_driver_present": True,
                "kernel_claims_likely": pid == PID_NORMAL_MSC,
                "iokit_no_resources_constant": "0xe00002be", "simulated": True}

    def claim_probe(self, pid: int, allow_claim: bool) -> dict[str, Any]:
        self.claim_attempts += 1
        base: dict[str, Any] = {"pid": hex(pid), "present": True, "allow_claim": allow_claim,
                                "kernel_driver_active": True, "euid": 501, "simulated": True}
        if not allow_claim:
            base.update(claim="NOT_ATTEMPTED")
            return base
        if self.scenario == "no-resources":
            base.update(claim="FAILED", claim_error="simulated IOUSBHost kIOReturnNoResources",
                        error_code=0xE00002BE, error_name="kIOReturnNoResources")
        elif self.scenario in ("no-privilege", "root-denied"):
            # The real-world case: console runs as uid 501, so
            # USBDeviceOpenSeize fails and the claim dies on USBInterfaceOpen.
            base.update(claim="FAILED",
                        claim_error="USBError: [Errno 13] Access denied (insufficient permissions)",
                        error_code=-3, error_name="LIBUSB_ERROR_ACCESS",
                        failure_scope="non_root_refusal",
                        failure_stage="claim_interface/USBInterfaceOpen",
                        capture_verified=False,
                        capture_hint="非 root（euid=501）：先跑 root root capture 验证")
        else:
            base.update(claim="OK", release="OK")
        return base

    def capture_probe(self, pid: int, timeout_s: float = 30.0,
                      allow_detach: bool = False) -> dict[str, Any]:
        base: dict[str, Any] = {
            "pid": hex(pid), "present": True, "simulated": True,
            "allow_detach": allow_detach,
            "euid": 501 if self.scenario in ("capture-denied", "no-privilege") else 0,
            "macos": "15.4.1 (simulated)",
            "volumes_before": ["/Volumes/PMHOME"], "volumes_after": ["/Volumes/PMHOME"],
            "bus_before": 1, "bus_after": 1, "address_before": 1, "address_after": 1,
            "reenumerated": False, "steps": [],
        }
        if self.scenario in ("capture-denied", "no-privilege"):
            base.update(ok=False, needs_root=True,
                        steps=[{"step": "root", "ok": False, "euid": 501}])
            return base
        if self.scenario == "root-denied":
            # Root, but the interface is exclusively held by an IOKit client:
            # plain claim = ACCESS, detach not authorized in this run.
            base["interface_owner"] = {"ok": True, "interface": 0,
                                       "usb_exclusive_owner": "IOUSBMassStorageInterfaceNub",
                                       "exclusive": True}
            base["interface_exclusive_owner"] = "IOUSBMassStorageInterfaceNub"
            steps = [{"step": "claim", "ok": False, "mode": "claim_only",
                      "error": "USBError: [Errno 13] Access denied"
                               " (insufficient permissions)",
                      "error_name": "LIBUSB_ERROR_ACCESS", "error_code": -3}]
            if allow_detach:
                steps += [{"step": "detach", "ok": True},
                          {"step": "claim", "ok": True, "mode": "detach_then_claim"},
                          {"step": "release", "ok": True},
                          {"step": "attach", "ok": True}]
                base.update(ok=True, claim="OK", release="OK", attach="OK",
                            capture_mode="detach_then_claim", reenumerated=True,
                            product=EXPECTED_PRODUCT,
                            serial_sha256=_serial_digest("SIM-BODY-0001"), steps=steps)
                return base
            base["detach_skipped"] = True
            base["detach_recommended"] = True
            steps.append({"step": "detach", "ok": False, "skipped": True,
                          "error": "claim_only 被拒：接口 0 由 IOUSBMassStorageInterfaceNub "
                                   "独占；需要 --accept-disconnect"})
            base.update(ok=False, claim="FAILED", error_name="LIBUSB_ERROR_ACCESS",
                        error_code=-3, steps=steps)
            return base
        if self.scenario == "no-resources":
            steps = [{"step": "claim", "ok": False, "mode": "claim_only",
                      "error": "simulated: root 下 USBInterfaceOpen 仍被内核拒绝"}]
            if allow_detach:
                steps += [{"step": "detach", "ok": True},
                          {"step": "claim", "ok": False, "mode": "detach_then_claim",
                           "error": "simulated: detach 后依然 kIOReturnNoResources"}]
            else:
                base["detach_skipped"] = True
                steps.append({"step": "detach", "ok": False, "skipped": True,
                              "error": "simulated: 未授权 detach 兜底"})
            base.update(ok=False, claim="FAILED", error_name="kIOReturnNoResources",
                        steps=steps)
            return base
        # Healthy root box: the seize in darwin_open already won the interface,
        # so claim succeeds without a detach and the bus address stays put.
        base.update(ok=True, claim="OK", release="OK", capture_mode="claim_only",
                    product=EXPECTED_PRODUCT,
                    serial_sha256=_serial_digest("SIM-BODY-0001"),
                    steps=[{"step": "claim", "ok": True, "mode": "claim_only"},
                           {"step": "release", "ok": True}])
        return base

    def auth_preflight(self) -> dict[str, Any]:
        return {"ready": True, "simulated": True, "auth_py": "simulated",
                "pmca_constants": "simulated", "pmca_crypto": "simulated"}

    def authenticate_normal(self) -> dict[str, Any]:
        if self.scenario == "no-transition":
            return {"ok": False, "mode": "auth", "rounds": [], "error": "no service reenumeration",
                    "simulated": True}
        self.pid = PID_SERVICE
        return {"ok": True, "mode": "auth", "rounds": [1, 2, 3], "simulated": True}

    def transition_probe(self, timeout_s: float = 90.0,
                         allow_detach: bool = True) -> dict[str, Any]:
        """(see public diagnose shell)"""
        result: dict[str, Any] = {
            "pid_before": hex(PID_NORMAL_MSC), "euid": 0, "allow_detach": allow_detach,
            "simulated": True, "present": True, "detached": True,
            "product": EXPECTED_PRODUCT,
            "serial_sha256": _serial_digest("SIM-BODY-0001"),
            "bus_before": 1, "address_before": 1, "steps": [
                {"step": "detach", "ok": True, "note": "simulated detach"},
            ], "ok": False,
        }
        auth = self.authenticate_normal()
        result["auth"] = auth
        result["steps"].append({"step": "auth", "ok": bool(auth.get("ok")),
                                "mode": auth.get("mode"), "rounds": auth.get("rounds")})
        if not auth.get("ok"):
            result["error"] = auth.get("error") or "auth failed"
            result["error_name"] = "NO_TRANSITION"
            result["wait"] = {"appeared": False, "elapsed_s": timeout_s}
            return result
        wait = self.wait_for_pid(PID_SERVICE, timeout_s)
        result["wait"] = wait
        result["steps"].append({"step": "wait_0336", "ok": bool(wait.get("appeared")),
                                "elapsed_s": wait.get("elapsed_s")})
        if wait.get("appeared"):
            result.update(ok=True, pid_after=hex(PID_SERVICE),
                          bus_after=1, address_after=1, reenumerated=False)
        else:
            result["error"] = "0336 did not appear (simulated)"
            result["error_name"] = "NO_0336"
        return result

    def authenticate_service(self) -> dict[str, Any]:
        return {"ok": True, "mode": "auth", "rounds": [1, 2, 3], "simulated": True}

    def product_info(self) -> dict[str, Any]:
        if self.scenario in ("hasp-degraded", "hasp-blocking"):
            # Mirrors the upstream record: the HASP sub-call fails with 129
            # while the service session itself keeps answering. The difference
            # between the two scenarios only shows up on a real data read.
            return {"ok": False, "failure": "hasp", "code": 129, "response": 0,
                    "classification": "hasp_only",
                    "session_ping": {"ok": True, "method": "get_status", "value": "0x0"},
                    "error": "Senser product info error 129 (HASP read failed)",
                    "simulated": True}
        return {"ok": True, "response": 1, "length": 88, "simulated": True}

    def read_capability(self, address: int = 0, size: int = 16,
                        paths: list[str] | None = None) -> dict[str, Any]:
        base: dict[str, Any] = {"method": "read_memory", "address": hex(address),
                                "size": size, "repeats": 2, "simulated": True}
        if self.pid != PID_SERVICE:
            return dict(base, ok=False, failure="no_session",
                        error="0336 不在位，没有可用的服务会话")
        if self.scenario == "hasp-blocking":
            # The 129 turns out not to be HASP-specific: the same error comes
            # back from a plain data read, which callers must surface as a
            # classification mismatch rather than an advisory.
            return dict(base, ok=False, failure="hasp", code=129, reads=[], failed_at=1,
                        error="Senser read memory error 129 (HASP sub-call failed)")
        raw = bytes((0x40 + i * 7) & 0xFF for i in range(size))
        digest = hashlib.sha256(raw).hexdigest()[:16]
        return dict(base, ok=True, samples_equal=True,
                    distinct_byte_values=len(set(raw)),
                    reads=[{"ok": True, "length": size, "sha256": digest,
                            "hex": raw[:8].hex()} for _ in range(2)])

    def stop_service(self) -> dict[str, Any]:
        if self.scenario == "restore-fail":
            return {"ok": False, "error": "simulated: 0336 did not release", "simulated": True}
        self.pid = PID_NORMAL_MSC
        return {"ok": True, "simulated": True}

    def wait_for_pid(self, pid: int, timeout_s: float, poll_s: float = 0.5,
                     on_tick: Any = None) -> dict[str, Any]:
        appeared = pid == self.pid
        elapsed = 2.4 if appeared else timeout_s
        if pid == PID_SERVICE:
            self.service_waits += 1
            # transition block; instability shows up across repeated runs.
            if self.scenario == "flaky" and self.service_waits >= 2 and self.service_waits % 3 == 2:
                appeared, elapsed = False, timeout_s
                self.pid = PID_NORMAL_MSC
        if on_tick is not None:
            # Virtual heartbeat ticks: report the pre-transition identity first
            # so the log reads "still 0DA5 ... then 0336 appears".
            start_pid = PID_NORMAL_MSC if pid == PID_SERVICE else PID_SERVICE
            final_pid = pid if appeared else start_pid
            step = 2.0 if appeared else max(10.0, timeout_s / 6)
            tick = 0.0
            while tick < elapsed:
                on_tick(tick, [hex(start_pid)])
                tick += step
            on_tick(elapsed, [hex(final_pid)])
        self.sleep(min(elapsed, 0.2) * self.clock_scale)
        return {"appeared": appeared, "elapsed_s": elapsed,
                "observations": [{"t": 0.0, "pids": [hex(self.pid)]},
                                 {"t": elapsed, "pids": [hex(pid) if appeared else hex(self.pid)]}],
                "simulated": True}

    def wait_for_libusb_pid(self, pid: int, timeout_s: float, poll_s: float = 0.5,
                            on_tick: Any = None) -> dict[str, Any]:
        # Simulation has no separate libusb lag; mirror wait_for_pid.
        return self.wait_for_pid(pid, timeout_s, poll_s, on_tick=on_tick)

    def sleep(self, seconds: float) -> None:
        if self.clock_scale > 0:
            time.sleep(min(seconds * self.clock_scale, 0.5))


def make_backend(simulate: str | None = None, auth_backend: Path | None = None,
                 pmca_dir: Path | None = None) -> Backend:
    if simulate:
        return SimulatedBackend(simulate)
    return MacUsbBackend(auth_backend=auth_backend, pmca_dir=pmca_dir)
