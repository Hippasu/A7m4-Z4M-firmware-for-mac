"""Answer one question: is the A7M4 connected to this Mac, and how far?

``python3 -m m4probe.detect`` is a stand-alone, strictly read-only tool. It does
not authenticate, does not claim interfaces and does not write anything.

"Connected" is a ladder, not a boolean:

    ABSENT           no Sony USB device is registered by the gatekeeper
    ENUMERATED       054C:0DA5 is present (electrical + USB enumeration OK)
    MSC_READY        0DA5 plus a mounted external volume (mass-storage ready)
    VERIFIED         stable ILCE-7M4 identity, visible to libusb, same port
    SERVICE_ALIVE    054C:0336 is present and answers GET_STATUS
    STALE_SERVICE    0336 is listed but does not answer (old devnode)
    AMBIGUOUS        more than one Sony camera is connected

The ladder mirrors upstream: normal_identity.py reads descriptors three times,
usb_session.py binds the physical port, and auth.py::_device_alive() filters
stale service devnodes with a harmless GET_STATUS.
"""

from __future__ import annotations

import argparse
import json
import plistlib
import subprocess
import sys
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from .backends import MacUsbBackend, SimulatedBackend, _run
from .model import (
    CAMERA_PIDS, EXPECTED_PRODUCT, FASTBOOT_VID, IDENTITY_RULE, MODEL_A7M4,
    TRANSITIONAL_SONY_PIDS,
    MODEL_NON_SONY, MODEL_OTHER_SONY_CAMERA, MODEL_OTHER_SONY_DEVICE,
    MODEL_RULE, MODEL_UNKNOWN, PID_FASTBOOT, PID_NORMAL_MSC, PID_SENSER_ALT,
    PID_SERVICE, SONY_VID, TARGET_MODEL, DeviceInfo, Tier, classify_model,
    name_error,
)


class LinkState(Enum):
    ABSENT = "ABSENT"
    ENUMERATED = "ENUMERATED"
    MSC_READY = "MSC_READY"
    VERIFIED = "VERIFIED"
    SERVICE_ALIVE = "SERVICE_ALIVE"
    STALE_SERVICE = "STALE_SERVICE"
    AMBIGUOUS = "AMBIGUOUS"


STATE_TEXT = {
    LinkState.ABSENT: "未连接：登记簿上没有 A7M4 相机身份（054C:0DA5 或 054C:0336）",
    LinkState.ENUMERATED: "物理已连接：看到 054C:0DA5，但存储卷还没就绪",
    LinkState.MSC_READY: "海量存储就绪：相机已作为移动硬盘挂载",
    LinkState.VERIFIED: "连接已核验：机身标识稳定、libusb 可见、物理口一致",
    LinkState.SERVICE_ALIVE: "相机在线，但处于 0336 服务身份，需要恢复后才能正常使用",
    LinkState.STALE_SERVICE: "登记簿上有 0336，但它不响应（陈旧 devnode）",
    LinkState.AMBIGUOUS: "检测到多台 Sony 设备，无法判定是哪一台",
}

NEXT_ACTION = {
    LinkState.ABSENT: [
        "确认相机已开机、电池有电、用的是数据线而不是充电线",
        "相机菜单 → 设置 → USB → USB 连接模式选「海量存储器(MSC)」",
        "重新插拔后运行 python3 -m m4probe.detect --watch 2 --count 10",
    ],
    LinkState.ENUMERATED: [
        "等待几秒让 macOS 挂载存储卷；仍不挂载则检查 USB 连接模式是否为 MSC",
        "运行 python3 -m m4probe --stages S0,S1,S2,S3 --output out/triage",
    ],
    LinkState.MSC_READY: [
        "可以开始 S1/S2 的只读身份核验",
        "若要测试独占接口，再加 --allow-claim",
    ],
    LinkState.VERIFIED: [
        "可继续主机侧只读核验",
    ],
    LinkState.SERVICE_ALIVE: [
        "这是服务身份，不是普通连接；请重新插拔或重启相机，恢复普通 USB（MSC）连接",
        "请先恢复普通 USB 连接后再继续",
    ],
    LinkState.STALE_SERVICE: [
        "用 GET_STATUS 复核；重新插拔 USB 让门卫清除旧登记",
        "请先恢复普通 USB 连接后再继续",
    ],
    LinkState.AMBIGUOUS: [
        "只保留一台 Sony 设备后重测；上游要求机身唯一",
    ],
}

READY_FOR_PROBE = {
    LinkState.ABSENT: False,
    LinkState.ENUMERATED: True,
    LinkState.MSC_READY: True,
    LinkState.VERIFIED: True,
    LinkState.SERVICE_ALIVE: False,
    LinkState.STALE_SERVICE: False,
    LinkState.AMBIGUOUS: False,
}


# ---------------------------------------------------------------------------
# USB connection mode of the camera
# ---------------------------------------------------------------------------
# Sony ILCE-7M4 exposes a DIFFERENT USB product id for every "USB Connection
# Mode": Mass Storage(MSC) / MTP / PC Remote / USB Streaming. The 0336 chain
# only works in MSC, whose identity is 054C:0DA5 (PID_NORMAL_MSC). Other modes
# show up as Sony VID devices with other PIDs; the gatekeeper must be able to
# say "the cable is plugged but the mode is wrong" instead of a bare ABSENT.
# We deliberately do NOT hard-code MTP/PC Remote PIDs (firmware dependent):
# any VID=054C device outside CAMERA_PIDS is reported as OTHER_SONY_MODE with
# an actionable hint and the observed id, never as a confirmed camera mode.

MODE_MSC = "MSC"
MODE_SERVICE = "SERVICE"
MODE_OTHER_SONY = "OTHER_SONY_MODE"
MODE_FASTBOOT = "FASTBOOT"
MODE_NONE = "NONE"
MODE_AMBIGUOUS = "AMBIGUOUS"
MODE_UNKNOWN = "UNKNOWN"

MODE_TEXT = {
    MODE_MSC: "大容量存储 MSC 模式（054C:0DA5）",
    MODE_SERVICE: "Senser 服务模式（054C:0336 / 02A9，需先恢复）",
    MODE_OTHER_SONY: "非 A7M4 相机身份：Sony 设备 PID 不属于 0DA5/0336/02A9"
                     "（可能是别的索尼机型、Sony 配件，或 A7M4 处于 MTP / PC 遥控 / USB 串流）",
    MODE_FASTBOOT: "Fastboot 身份（18D1:4EE0，另一条刷机链路）",
    MODE_NONE: "未检测到 Sony 相机设备",
    MODE_AMBIGUOUS: "多台 Sony 相机身份并存，模式无法判定",
    MODE_UNKNOWN: "模式未判定",
}

MODE_HINT = {
    MODE_MSC: "已处于大容量存储模式，可继续 S1/S2 只读核验",
    MODE_SERVICE: "请重新插拔，恢复普通 USB（MSC）连接",
    MODE_OTHER_SONY: "先确认接入的是 ILCE-7M4；若是别的索尼机型本探针不适用；"
                     "若确为 A7M4，相机菜单 → 设置 → USB → USB 连接模式 → 选「海量存储器(MSC)」，然后重新插拔",
    MODE_FASTBOOT: "这是另一条刷机链路，本探针不适用",
    MODE_NONE: "确认开机、数据线与相机 USB 连接模式",
    MODE_AMBIGUOUS: "只保留一台机身再重测",
    MODE_UNKNOWN: "重新枚举或查看 --audit 输出",
}


# ---------------------------------------------------------------------------
# Body-model identification (which Sony camera is this, really?)
# ---------------------------------------------------------------------------
MODEL_KIND_TEXT = {
    MODEL_A7M4: "目标机型：Sony ILCE-7M4（A7M4 / α7 IV）",
    MODEL_OTHER_SONY_CAMERA: "另一台索尼相机，不是 ILCE-7M4",
    MODEL_OTHER_SONY_DEVICE: "Sony 非相机设备（耳机 / 手柄 / 播放器等）",
    MODEL_NON_SONY: "非 Sony 设备",
    MODEL_UNKNOWN: "未能读取产品字符串，机身型号未证实",
}

MODEL_KIND_HINT = {
    MODEL_A7M4: "产品字符串与 VID/PID 一致，可继续 S1/S2 只读身份核验",
    MODEL_OTHER_SONY_CAMERA: "本探针的 0336 链路只针对 ILCE-7M4；请换接 A7M4 机身",
    MODEL_OTHER_SONY_DEVICE: "这不是相机，不要按相机 USB 模式排障；请接 ILCE-7M4",
    MODEL_NON_SONY: "与 Sony 无关，已排除",
    MODEL_UNKNOWN: "PID 命中但读不到 iProduct；用 --audit 或完整检测复核机身型号，"
                   "也可用 --strict-model 阻止未证实机型进入探针",
}


def _model_fields(buckets: dict[str, list[DeviceInfo]],
                  cameras: list[DeviceInfo]) -> dict[str, Any]:
    """Identify the physical body, independently of its USB connection mode.

    A PID says "this USB identity belongs to the A7M4 family"; only the
    product string says "this body is an ILCE-7M4". Unknown product strings
    yield ``target_a7m4=None`` (unproven), never a guess.
    """
    if cameras:
        primary: DeviceInfo | None = cameras[0]
    else:
        pool = list(buckets.get("wrong_model", [])) + list(buckets.get("other_sony", []))
        primary = next((d for d in pool if d.product), pool[0] if pool else None)
    if primary is None:
        kind, name = MODEL_UNKNOWN, ""
    else:
        kind, name = classify_model(primary.vid, primary.product)
    sony_devices = (list(cameras) + list(buckets.get("wrong_model", []))
                    + list(buckets.get("other_sony", [])))
    return {
        "model": name,
        "model_match": kind,
        "model_text": MODEL_KIND_TEXT[kind],
        "model_hint": MODEL_KIND_HINT[kind],
        "target_model": TARGET_MODEL,
        "target_a7m4": (True if kind == MODEL_A7M4 else
                        None if kind == MODEL_UNKNOWN else False),
        "model_rule": MODEL_RULE,
        "observed_sony_devices": [
            {"id": d.label, "product": d.product,
             "model_match": classify_model(d.vid, d.product)[0]}
            for d in sony_devices
        ],
    }


def mode_of(buckets: dict[str, list[DeviceInfo]],
            cameras: list[DeviceInfo]) -> str:
    """Classify the camera's USB connection mode from the identity gate."""
    if len(cameras) > 1:
        return MODE_AMBIGUOUS
    if cameras:
        pid = cameras[0].pid
        if pid in (PID_SERVICE, PID_SENSER_ALT):
            return MODE_SERVICE
        if pid == PID_NORMAL_MSC:
            return MODE_MSC
        return MODE_UNKNOWN
    if buckets.get("fastboot"):
        return MODE_FASTBOOT
    if buckets.get("other_sony") or buckets.get("wrong_model"):
        return MODE_OTHER_SONY
    return MODE_NONE


def _sony_looks_transitional(buckets: dict[str, list[DeviceInfo]]) -> bool:
    """True when every observed Sony non-camera entry looks like a mode-switch blip."""
    others = list(buckets.get("other_sony", [])) + list(buckets.get("wrong_model", []))
    if not others:
        return False
    for d in others:
        if d.pid in TRANSITIONAL_SONY_PIDS:
            continue
        if classify_model(d.vid, d.product)[0] == MODEL_UNKNOWN:
            continue
        return False
    return True


def _mode_fields(buckets: dict[str, list[DeviceInfo]],
                 cameras: list[DeviceInfo],
                 storage_probe: Callable[[], dict[str, Any]] | None = None
                 ) -> dict[str, Any]:
    """Mode + body model + optional mass-storage readiness, all read-only."""
    mode = mode_of(buckets, cameras)
    other = (list(buckets.get("other_sony", []))
             + list(buckets.get("wrong_model", [])))
    fields: dict[str, Any] = {
        "mode": mode,
        "mode_text": MODE_TEXT[mode],
        "mode_hint": MODE_HINT[mode],
        "mode_is_msc": mode == MODE_MSC,
        "other_sony_devices": [d.label for d in other],
        "msc_ready": None,           # None = not checked; False/True = probed
        "mounted_volumes": [],
        **_model_fields(buckets, cameras),
    }
    if mode == MODE_MSC and storage_probe is not None:
        try:
            storage = storage_probe()
        except Exception as exc:
            storage = {"mounted": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        fields["msc_ready"] = bool(storage.get("mounted"))
        fields["mounted_volumes"] = list(storage.get("mounted_external_volumes") or [])
    return fields


# ---------------------------------------------------------------------------
# Real macOS probes (all read-only)
# ---------------------------------------------------------------------------

def probe_storage() -> dict[str, Any]:
    """Mounted external volumes that are actually on the USB bus.

    A plain ``diskutil list -plist external`` also lists mounted DMG disk
    images, which would produce a false "camera storage is ready". Each mount
    is therefore inspected with ``diskutil info -plist`` and only volumes whose
    BusProtocol / IOProviderClass is USB are counted.
    """
    code, out, err = _run(["diskutil", "list", "-plist", "external"], timeout=15)
    result: dict[str, Any] = {
        "source": "diskutil list -plist external + diskutil info -plist",
        "ok": code == 0,
        "mounted": False,
        "usb_volumes": [],
        "ignored_volumes": [],
        "mounted_external_volumes": [],
    }
    if code != 0:
        result["error"] = err.strip()
        return result
    mounts: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "MountPoint" and isinstance(value, str) and value.startswith("/Volumes/"):
                    mounts.append(value)
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    try:
        data = plistlib.loads(out.encode("utf-8"))
        walk(data)
        result["external_disks"] = len(data.get("WholeDisks", []))
    except Exception as exc:
        result["parse_error"] = "%s: %s" % (type(exc).__name__, exc)
        return result

    for mount in sorted(set(mounts)):
        info_code, info_out, _ = _run(["diskutil", "info", "-plist", mount], timeout=10)
        if info_code != 0:
            continue
        try:
            info = plistlib.loads(info_out.encode("utf-8"))
        except Exception:
            continue
        row = {
            "mount_point": mount,
            "bus_protocol": info.get("BusProtocol"),
            "media_name": info.get("MediaName"),
            "device_identifier": info.get("DeviceIdentifier"),
            "content": info.get("Content"),
            "ejectable": info.get("Ejectable"),
            "removable": info.get("Removable"),
        }
        bus = "%s %s" % (info.get("BusProtocol") or "", info.get("IOProviderClass") or "")
        if "usb" in bus.lower():
            result["usb_volumes"].append(row)
        else:
            result["ignored_volumes"].append(row)
    result["mounted_external_volumes"] = [r["mount_point"] for r in result["usb_volumes"]]
    result["mounted"] = bool(result["usb_volumes"])
    result["note"] = "仅把 USB 总线上的卷当作相机存储；DMG/网络卷已排除"
    return result


def probe_libusb(pid: int) -> dict[str, Any]:
    """Is the device visible to PyUSB, and does it answer a harmless GET_STATUS?"""
    result: dict[str, Any] = {"pid": hex(pid), "visible": False, "alive": False}
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
        result["error"] = "PyUSB/libusb-package 未安装：%s" % exc
        return result
    if dev is None:
        result["note"] = "libusb 后端看不到该设备（内核驱动可能已占用）"
        return result
    result["visible"] = True
    result["bus"], result["address"] = dev.bus, dev.address
    try:
        langids = tuple(usb.util.get_langids(dev))
        result["langids"] = [hex(x) for x in langids]
        if langids:
            result["product"] = usb.util.get_string(dev, dev.iProduct, langid=langids[0])
            result["serial_sha256"] = _digest(usb.util.get_string(dev, dev.iSerialNumber, langid=langids[0]))
    except Exception as exc:
        result["descriptor_error"] = "%s: %s" % (type(exc).__name__, exc)
    try:
        dev.ctrl_transfer(0x80, 0, 0, 0, 2, 2000)
        result["alive"] = True
        result["get_status"] = "OK"
    except Exception as exc:
        result["get_status"] = "FAILED"
        result["error_name"] = name_error(getattr(exc, "backend_error_code", None))
        result["error_code"] = getattr(exc, "backend_error_code", None)
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
    finally:
        try:
            usb.util.dispose_resources(dev)
        except Exception:
            pass
    return result


def _digest(serial: str | None) -> str | None:
    import hashlib
    return hashlib.sha256(serial.encode("utf-8")).hexdigest() if serial else None


def default_probes() -> dict[str, Callable[..., dict[str, Any]]]:
    return {"storage": probe_storage, "libusb": probe_libusb}


# ---------------------------------------------------------------------------
# Core detection
# ---------------------------------------------------------------------------

def classify_devices(devices: list[DeviceInfo],
                     strict_model: bool = False) -> dict[str, list[DeviceInfo]]:
    """The single gate that decides what may be called "the A7M4".

    A camera requires BOTH the Sony vendor ID AND one of the three camera
    product IDs. Everything else — USB disks, hubs, card readers, other Sony
    gadgets sharing VID 054C — is recorded as ignored, never as a camera.

    ``strict_model`` adds a body-model check on top of the PID gate: a device
    whose product string clearly names ANOTHER Sony camera (or a Sony gadget)
    is moved to ``wrong_model`` instead of being counted as the target body.
    A missing product string is NOT excluded — macOS does not always expose
    iProduct, so an unreadable string means "unproven", not "wrong body".
    """
    buckets: dict[str, list[DeviceInfo]] = {
        "cameras": [], "fastboot": [], "other_sony": [],
        "wrong_model": [], "others": []}
    for d in devices:
        if d.vid == SONY_VID and d.pid in CAMERA_PIDS:
            # Only exclude a clearly *other camera* body (ILCE-7M3 / ZV-…).
            # Generic strings ("Sony USB Device") are UNKNOWN and must not
            # kick a real 0336/0DA5 session out of the camera bucket.
            if strict_model and classify_model(d.vid, d.product)[0] == MODEL_OTHER_SONY_CAMERA:
                buckets["wrong_model"].append(d)
                continue
            buckets["cameras"].append(d)
        elif d.vid == FASTBOOT_VID and d.pid == PID_FASTBOOT:
            buckets["fastboot"].append(d)
        elif d.vid == SONY_VID:
            buckets["other_sony"].append(d)
        else:
            buckets["others"].append(d)
    return buckets


def detect_connection(backend: Any, *, repeats: int = 3,
                      probes: dict[str, Callable[..., dict[str, Any]]] | None = None,
                      sleep: Callable[[float], None] = time.sleep,
                      interval: float = 0.15,
                      strict_model: bool = False) -> dict[str, Any]:
    """Read-only, layered connection check. Never sends a camera command."""
    active = probes or default_probes()
    observations: list[dict[str, Any]] = []
    for index in range(max(1, repeats)):
        devices = backend.enumerate_devices(fast=True)
        observations.append({
            "index": index,
            "pids": sorted({d.label for d in devices}),
            "locations": sorted({str(d.location_id) for d in devices if d.location_id}),
            "serials": sorted({d.serial_sha256 for d in devices if d.serial_sha256}),
            "products": sorted({d.product for d in devices if d.product}),
        })
        if index + 1 < repeats:
            sleep(interval)

    devices = backend.enumerate_devices()
    buckets = classify_devices(devices, strict_model=strict_model)
    cameras = buckets["cameras"]
    evidence: dict[str, Any] = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "tier": Tier.HOST_ONLY.name,
        "camera_commands": 0,
        "devices": [d.to_dict() for d in cameras],
        "observations": observations,
        "repeat_count": repeats,
        "identity_rule": IDENTITY_RULE,
        "model_rule": MODEL_RULE,
        "strict_model": strict_model,
        "audit": {
            "usb_devices_seen": len(devices),
            "cameras": len(cameras),
            "fastboot": len(buckets["fastboot"]),
            "other_sony": [d.to_dict() for d in buckets["other_sony"]],
            "wrong_model": [d.to_dict() for d in buckets["wrong_model"]],
            "non_camera_usb_devices": [
                d.to_dict() for d in (buckets["others"] + buckets["other_sony"]
                                      + buckets["wrong_model"])],
        },
    }
    evidence["usb_mode"] = _mode_fields(buckets, cameras)
    evidence["model_identification"] = _model_fields(buckets, cameras)
    if len(cameras) > 1:
        return _pack(LinkState.AMBIGUOUS, evidence, "多台 A7M4 相机身份并存")

    identities = {tuple(sorted(o["serials"])) for o in observations}
    locations = {tuple(sorted(o["locations"])) for o in observations}
    evidence["stability"] = {
        "serial_stable": len(identities) == 1,
        "port_stable": len(locations) == 1,
        "identities": len(identities),
        "locations": len(locations),
    }

    if not cameras:
        evidence["storage"] = active["storage"]()
        if buckets["fastboot"]:
            evidence["note"] = "发现 18D1:4EE0 Fastboot 身份，属于另一条刷机链路，不是普通 MSC 连接"
            return _pack(LinkState.ABSENT, evidence, "仅发现 Fastboot 身份")
        if buckets["wrong_model"]:
            evidence["note"] = ("PID 命中 A7M4 身份组，但产品字符串显示为其他机型；"
                                "--strict-model 已将其排除")
            return _pack(LinkState.ABSENT, evidence,
                         "PID 命中但机身不是 ILCE-7M4（严格机型模式已排除）")
        if buckets["other_sony"]:
            if _model_fields(buckets, cameras)["model_match"] == MODEL_OTHER_SONY_CAMERA:
                evidence["note"] = ("发现另一台索尼相机（不是 ILCE-7M4）；其 USB PID 属于"
                                    "自己的机型组，不在 A7M4 的 0DA5/0336/02A9 内")
                return _pack(LinkState.ABSENT, evidence,
                             "接入的是其他索尼机型，不是 A7M4")
            evidence["note"] = ("发现 VID=054C 但 PID 不是相机身份的 Sony 设备（耳机/播放器等），"
                                "已按身份规则排除")
            return _pack(LinkState.ABSENT, evidence, "只有非相机的 Sony 设备")
        if buckets["others"]:
            evidence["note"] = ("发现 USB 存储类或其他设备，但 VID/PID 与 A7M4 完全不同，"
                                "不会被识别为相机")
            return _pack(LinkState.ABSENT, evidence, "门卫登记簿无 A7M4 相机身份")
        return _pack(LinkState.ABSENT, evidence, "门卫登记簿没有任何 USB 设备")

    device = cameras[0]
    evidence["primary"] = device.to_dict()

    if device.pid in (PID_SERVICE, PID_SENSER_ALT):
        status = active["libusb"](device.pid)
        evidence["libusb"] = status
        evidence["identity_proof"] = _identity_proof(device, status)
        state = LinkState.SERVICE_ALIVE if status.get("alive") else LinkState.STALE_SERVICE
        return _pack(state, evidence,
                     "0336 在线" if state is LinkState.SERVICE_ALIVE else "0336 不响应 GET_STATUS")

    if device.pid == PID_NORMAL_MSC:
        status = active["libusb"](PID_NORMAL_MSC)
        storage = active["storage"]()
        evidence["libusb"] = status
        evidence["storage"] = storage
        evidence["identity_proof"] = _identity_proof(device, status)
        evidence["identity_ok"] = (
            device.product == EXPECTED_PRODUCT or status.get("product") == EXPECTED_PRODUCT
        ) and bool(device.serial_sha256 or status.get("serial_sha256"))
        evidence["libusb_visible"] = bool(status.get("visible"))
        stable = evidence["stability"]["serial_stable"] and evidence["stability"]["port_stable"]
        if evidence["identity_ok"] and stable and evidence["libusb_visible"] and storage.get("mounted"):
            return _pack(LinkState.VERIFIED, evidence, "机身、端口、libusb、存储卷四项齐备")
        if storage.get("mounted"):
            return _pack(LinkState.MSC_READY, evidence, "已挂载外部存储卷")
        return _pack(LinkState.ENUMERATED, evidence, "已枚举 0DA5，存储卷尚未就绪")

    return _pack(LinkState.ENUMERATED, evidence, "发现 Sony 设备，身份未识别：%s" % device.label)


def quick_link(backend: Any,
               storage_probe: Callable[[], dict[str, Any]] | None = None,
               strict_model: bool = False
               ) -> dict[str, Any]:
    """Cheap connection snapshot for background monitoring.

    Uses only the fast enumeration path (IORegistry) plus the identity gate;
    no libusb probe. ``storage_probe`` is optional: when supplied and the
    camera is in MSC mode, it additionally checks whether the mass-storage
    volume is mounted. The monitor passes a throttled probe so a burst of
    IOMedia notifications cannot trigger repeated diskutil walks.

    ``state`` keeps its original values (ABSENT / ENUMERATED / SERVICE_ALIVE /
    AMBIGUOUS) so existing consumers and reports stay compatible; the USB
    connection mode is reported in the new ``mode`` / ``mode_text`` fields and
    the physical body in ``model`` / ``model_match`` / ``target_a7m4``.
    """
    devices = backend.enumerate_devices(fast=True)
    buckets = classify_devices(devices, strict_model=strict_model)
    cameras = buckets["cameras"]
    if len(cameras) > 1:
        state, connected, note = LinkState.AMBIGUOUS.value, False, "多台 A7M4 相机身份并存"
    elif not cameras:
        state, connected, note = LinkState.ABSENT.value, False, "登记簿上没有 A7M4 相机身份"
    elif cameras[0].pid in (PID_SERVICE, PID_SENSER_ALT):
        state, connected, note = LinkState.SERVICE_ALIVE.value, True, "0336 服务身份在线"
    elif cameras[0].pid == PID_NORMAL_MSC:
        state, connected, note = LinkState.ENUMERATED.value, True, "0DA5 已连接（普通身份）"
    else:
        state, connected = LinkState.ENUMERATED.value, True
        note = "Sony 设备在线：%s" % cameras[0].label

    fields = _mode_fields(buckets, cameras, storage_probe)
    kind = fields["model_match"]
    labels = ", ".join(fields["other_sony_devices"]) or "-"
    transitional = _sony_looks_transitional(buckets)
    if transitional and not cameras:
        fields["mode_text"] = ("索尼 USB 临时身份（切换连接模式中，"
                               "不是「非 A7M4」结论）")
        fields["mode_hint"] = ("请等待数秒让枚举完成；稳定后应为 054C:0DA5 / ILCE-7M4。"
                               "若一直停在临时身份，请在相机菜单选「海量存储器(MSC)」后重新插拔")
        note = ("检测到索尼 USB 临时身份（%s）：相机可能正在切换连接模式，请稍候；"
                "稳定后应显示 ILCE-7M4 / 0DA5，不要按「非 A7M4」处理"
                % labels)
    elif kind == MODEL_OTHER_SONY_CAMERA:
        note = ("接入的是另一台索尼相机 %s（%s），不是 ILCE-7M4；"
                "本探针的 0336 链路只适用于 A7M4，请换接 A7M4 机身"
                % (fields["model"] or "型号未读到", labels))
    elif kind == MODEL_OTHER_SONY_DEVICE:
        note = ("检测到 Sony 设备 %s（%s），不是相机，更不是 A7M4；请接 ILCE-7M4"
                % (fields["model"] or "型号未读到", labels))
    elif fields["mode"] == MODE_OTHER_SONY:
        note = ("登记簿上没有 A7M4 的 0DA5/0336 身份；发现 Sony 设备 %s —— "
                "机身型号读不到，若这是相机，多半是 USB 连接模式处于 MTP / "
                "PC 遥控 / USB 串流，请在相机菜单切到「海量存储器(MSC)」"
                % labels)
    elif fields["mode"] == MODE_MSC:
        suffix = ("，USB 存储卷已挂载" if fields["msc_ready"] else
                  "，USB 存储卷未挂载" if fields["msc_ready"] is False else "")
        unproven = "（机身型号未证实，仅有 PID 证据）" if kind == MODEL_UNKNOWN else ""
        note = "0DA5 已连接：相机处于「海量存储器(MSC)」模式" + unproven + suffix
    primary = cameras[0] if len(cameras) == 1 else None
    return {
        "state": state,
        "connected": connected,
        "note": note,
        "pids": [d.label for d in cameras],
        "products": sorted({d.product for d in cameras if d.product}),
        # Body binding for safety switches (serial preferred; session/location fallback).
        "serial_sha256": primary.serial_sha256 if primary else None,
        "session_id": primary.session_id if primary else None,
        "location_id": primary.location_id if primary else None,
        **fields,
    }


def _identity_proof(device: DeviceInfo, status: dict[str, Any] | None = None) -> dict[str, Any]:
    """The three facts that make this device an A7M4 and nothing else."""
    product = (status or {}).get("product") or device.product
    return {
        "vid": hex(device.vid),
        "pid": hex(device.pid),
        "vid_is_sony": device.vid == SONY_VID,
        "pid_is_camera": device.pid in CAMERA_PIDS,
        "product_string": product,
        "product_is_ilce7m4": product == EXPECTED_PRODUCT,
        "serial_sha256": device.serial_sha256 or (status or {}).get("serial_sha256"),
        "rule": IDENTITY_RULE,
    }


def _pack(state: LinkState, evidence: dict[str, Any], reason: str) -> dict[str, Any]:
    mode_info = evidence.get("usb_mode") or {}
    mode = mode_info.get("mode", MODE_UNKNOWN)
    # The full detector already ran the storage probe; fold its result into
    # the mode info so JSON output and the lightweight snapshot agree.
    storage = evidence.get("storage")
    if storage is not None and mode == MODE_MSC:
        mode_info["msc_ready"] = bool(storage.get("mounted"))
        mode_info["mounted_volumes"] = list(storage.get("mounted_external_volumes") or [])
        evidence["usb_mode"] = mode_info
    return {
        "schema": "m4-mac-camera-link/1",
        "state": state.value,
        "state_text": STATE_TEXT[state],
        "reason": reason,
        "connected": state not in (LinkState.ABSENT, LinkState.AMBIGUOUS),
        "ready_for_probe": READY_FOR_PROBE[state],
        "next_actions": NEXT_ACTION[state],
        "mode": mode,
        "mode_text": MODE_TEXT.get(mode, MODE_TEXT[MODE_UNKNOWN]),
        "mode_hint": MODE_HINT.get(mode, ""),
        "model": mode_info.get("model", ""),
        "model_match": mode_info.get("model_match", MODEL_UNKNOWN),
        "model_text": mode_info.get("model_text", ""),
        "target_model": TARGET_MODEL,
        "target_a7m4": mode_info.get("target_a7m4"),
        "evidence": evidence,
    }


# ---------------------------------------------------------------------------
# Simulation fixtures
# ---------------------------------------------------------------------------

SCENARIOS = ("verified", "msc-ready", "enumerated", "absent",
             "service-alive", "stale-service", "ambiguous",
             "usb-disk", "other-sony", "other-sony-camera", "foreign-body")

# Another Sony camera in Mass Storage mode: ILCE-7M3 uses its OWN PID group
# (0C02), so the A7M4 PID gate cannot see it at all.
OTHER_SONY_CAMERA_FIXTURE = DeviceInfo(
    vid=SONY_VID, pid=0x0C02, bus=1, address=6,
    location_id="0x01440000 / 4", session_id="sim-other-camera",
    product="ILCE-7M3", serial="SONY-A7M3-1",
    manufacturer="Sony Corporation", source="simulated",
)
# Worst case: a foreign body reports an A7M4-family PID (firmware reuse,
# spoofed descriptor, or a future model sharing the PID). Only the product
# string can tell these apart -- this is what --strict-model is for.
FOREIGN_BODY_FIXTURE = DeviceInfo(
    vid=SONY_VID, pid=PID_NORMAL_MSC, bus=1, address=7,
    location_id="0x01450000 / 5", session_id="sim-foreign-body",
    product="ILCE-7M3", serial="SONY-FOREIGN-1",
    manufacturer="Sony Corporation", source="simulated",
)

# Real-world vendor/product pairs that must NEVER match the A7M4 gate.
USB_DISK_FIXTURE = DeviceInfo(
    vid=0x1058, pid=0x2667, bus=1, address=4,
    location_id="0x01400000 / 1", session_id="sim-disk",
    product="Elements 25A3", serial="WX21D4MISKD",
    manufacturer="Western Digital", source="simulated",
)
OTHER_SONY_FIXTURE = DeviceInfo(
    vid=SONY_VID, pid=0x0B57, bus=1, address=5,
    location_id="0x01430000 / 3", session_id="sim-sony-gadget",
    product="Sony Headset", serial="SONY-GADGET-1",
    manufacturer="Sony Corporation", source="simulated",
)


class ScenarioBackend:
    """Only overrides enumeration; the rest is a SimulatedBackend."""

    simulated = True

    def __init__(self, scenario: str) -> None:
        if scenario not in SCENARIOS:
            raise ValueError("unknown scenario: " + scenario)
        self.scenario = scenario
        self.inner = SimulatedBackend("healthy")

    def _device(self, pid: int) -> DeviceInfo:
        return DeviceInfo(
            vid=SONY_VID, pid=pid, bus=1, address=3,
            location_id="0x01100000 / 1", session_id="sim-session",
            product=EXPECTED_PRODUCT, serial="SIM-BODY-0001",
            manufacturer="Sony Corporation", source="simulated",
        )

    def enumerate_devices(self, fast: bool = False) -> list[DeviceInfo]:
        s = self.scenario
        if s == "absent":
            return []
        if s == "usb-disk":
            return [USB_DISK_FIXTURE]
        if s == "other-sony":
            return [OTHER_SONY_FIXTURE]
        if s == "other-sony-camera":
            return [OTHER_SONY_CAMERA_FIXTURE]
        if s == "foreign-body":
            return [FOREIGN_BODY_FIXTURE]
        if s == "ambiguous":
            return [self._device(PID_NORMAL_MSC), self._device(PID_SERVICE)]
        pid = PID_SERVICE if s in ("service-alive", "stale-service") else PID_NORMAL_MSC
        return [self._device(pid)]

    def host_facts(self) -> dict[str, Any]:
        return self.inner.host_facts()


def simulation_probes(scenario: str) -> dict[str, Callable[..., dict[str, Any]]]:
    mounted = scenario in ("verified", "msc-ready", "usb-disk")
    alive = scenario == "service-alive"
    # msc-ready models the very common macOS case: the system has mounted the
    # camera as a disk, but libusb cannot see/claim it while the kernel owns it.
    visible = scenario not in ("stale-service", "msc-ready")

    def storage() -> dict[str, Any]:
        mount = "/Volumes/Elements" if scenario == "usb-disk" else "/Volumes/SONY"
        return {"source": "simulated", "ok": True, "mounted": mounted,
                "mounted_external_volumes": [mount] if mounted else [],
                "usb_volumes": [{"mount_point": mount, "bus_protocol": "USB"}] if mounted else [],
                "ignored_volumes": [],
                "external_disks": 1 if mounted else 0}

    def libusb(pid: int) -> dict[str, Any]:
        # A foreign body must not magically pass the identity proof just
        # because the simulated libusb probe returns the expected product.
        product = ("ILCE-7M3" if scenario in ("foreign-body", "other-sony-camera")
                   else EXPECTED_PRODUCT)
        return {"pid": hex(pid), "visible": visible, "alive": alive,
                "get_status": "OK" if alive else "FAILED",
                "product": product,
                "serial_sha256": _digest("SIM-BODY-0001"),
                "langids": ["0x409"], "source": "simulated"}

    return {"storage": storage, "libusb": libusb}


def make_backend(simulate: str | None):
    return ScenarioBackend(simulate) if simulate else MacUsbBackend()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _render(result: dict[str, Any], as_json: bool) -> str:
    if as_json:
        return json.dumps(result, ensure_ascii=False, indent=2)
    lines = [
        "状态: %s" % result["state"],
        "USB 连接模式: %s" % result.get("mode_text", "-"),
        "机身型号: %s ｜ 目标机型判定: %s ｜ 是否 A7M4: %s" % (
            result.get("model") or "未读到", result.get("model_match", "-"),
            {True: "是", False: "否", None: "未证实"}.get(result.get("target_a7m4"), "-")),
        "说明: %s" % result["state_text"],
        "判定依据: %s" % result["reason"],
        "已连接: %s ｜ 可进入探针: %s ｜ 相机命令: %d" % (
            "是" if result["connected"] else "否",
            "是" if result["ready_for_probe"] else "否",
            result["evidence"]["camera_commands"]),
        "",
        "证据:",
    ]
    ev = result["evidence"]
    for d in ev.get("devices", []):
        lines.append("  设备 %s  %s  locationID=%s" % (
            d["id"], d.get("product") or "-", d.get("location_id") or "-"))
    audit = ev.get("audit") or {}
    if audit.get("non_camera_usb_devices"):
        lines.append("  非相机 USB 设备: %d 台，VID/PID 与 A7M4 不符，已排除：" % len(
            audit["non_camera_usb_devices"]))
        for d in audit["non_camera_usb_devices"]:
            lines.append("    - %s  %s  %s" % (d["id"], d.get("product") or "-",
                                               d.get("manufacturer") or "-"))
    if ev.get("identity_proof"):
        p = ev["identity_proof"]
        lines.append("  身份证明: VID=%s(Python 端 %s) PID=%s  iProduct=%s" % (
            p["vid"], "Sony" if p["vid_is_sony"] else "非 Sony",
            p["pid"], p["product_string"] or "-"))
    if ev.get("libusb"):
        u = ev["libusb"]
        lines.append("  libusb: 可见=%s  GET_STATUS=%s  %s" % (
            u.get("visible"), u.get("get_status", "-"), u.get("error_name", "")))
    if ev.get("storage"):
        s = ev["storage"]
        lines.append("  存储卷: USB 已挂载=%s  %s" % (
            s.get("mounted"), ", ".join(s.get("mounted_external_volumes", [])) or "-"))
        if s.get("ignored_volumes"):
            lines.append("  已排除的非 USB 外部卷: %s" % ", ".join(
                r["mount_point"] for r in s["ignored_volumes"]))
    if ev.get("stability"):
        t = ev["stability"]
        lines.append("  稳定性: 机身摘要=%s  物理口=%s（重复 %d 次）" % (
            "稳定" if t["serial_stable"] else "不稳定",
            "稳定" if t["port_stable"] else "不稳定", ev["repeat_count"]))
    lines.append("")
    lines.append("下一步:")
    for i, action in enumerate(result["next_actions"], 1):
        lines.append("  %d. %s" % (i, action))
    return "\n".join(lines)


def _render_audit(backend: Any, strict_model: bool = False) -> str:
    """Show the whole gatekeeper registry and why each row is or is not the A7M4."""
    devices = backend.enumerate_devices()
    buckets = classify_devices(devices, strict_model=strict_model)
    lines = [
        "USB 设备审计（只读，%d 台）" % len(devices),
        "判定规则: %s" % IDENTITY_RULE,
        "",
        "VID:PID      判定            产品字符串           厂商",
    ]
    for d in devices:
        if d in buckets["cameras"]:
            verdict = "A7M4 相机身份"
        elif d in buckets["fastboot"]:
            verdict = "Fastboot（另一条链路）"
        elif d in buckets["other_sony"]:
            verdict = "Sony 设备，PID 非相机，排除"
        elif d in buckets["wrong_model"]:
            verdict = "PID 命中但机型不符，排除"
        else:
            verdict = "非 Sony，排除"
        lines.append("%-12s %-24s %-20s %s" % (
            d.label, verdict, (d.product or "-")[:20], (d.manufacturer or "-")[:24]))
    lines.append("")
    lines.append("相机身份: %d 台 ｜ Fastboot: %d ｜ 其他 Sony: %d ｜ 机型不符: %d ｜ 非 Sony: %d" % (
        len(buckets["cameras"]), len(buckets["fastboot"]),
        len(buckets["other_sony"]), len(buckets["wrong_model"]), len(buckets["others"])))
    lines.append("机型判定规则: %s" % MODEL_RULE)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="m4probe.detect",
        description="Read-only layered check: is the A7M4 connected to this Mac?")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--watch", type=float, default=0.0,
                        help="poll interval in seconds; 0 means check once")
    parser.add_argument("--count", type=int, default=5, help="number of polls in watch mode")
    parser.add_argument("--repeats", type=int, default=3, help="enumeration repeats per check")
    parser.add_argument("--simulate", choices=SCENARIOS, default=None,
                        help="deterministic fixture, no camera needed")
    parser.add_argument("--output", type=Path, default=None,
                        help="also append each result as JSON to this file")
    parser.add_argument("--audit", action="store_true",
                        help="list every USB device and mark whether it matches the A7M4 gate")
    parser.add_argument("--strict-model", action="store_true",
                        help="only count a body whose product string is NOT another Sony "
                             "model/gadget as the A7M4 (unreadable strings still pass)")
    args = parser.parse_args(argv)

    backend = make_backend(args.simulate)
    probes = simulation_probes(args.simulate) if args.simulate else default_probes()
    if args.audit:
        print(_render_audit(backend, strict_model=args.strict_model), flush=True)
        if not args.watch:
            return 0
    polls = args.count if args.watch > 0 else 1
    last_state: str | None = None
    results: list[dict[str, Any]] = []
    stream = None
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        stream = args.output.open("a", encoding="utf-8")
    try:
        for index in range(max(1, polls)):
            result = detect_connection(backend, repeats=args.repeats, probes=probes,
                                       strict_model=args.strict_model)
            results.append(result)
            if stream:
                stream.write(json.dumps(result, ensure_ascii=False) + "\n")
                stream.flush()
            changed = result["state"] != last_state
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            elif changed or index == 0 or not args.watch:
                if index and args.watch:
                    print("\n--- 状态变化 @ %s ---" % datetime.now().strftime("%H:%M:%S"), flush=True)
                print(_render(result, False), flush=True)
            elif args.watch:
                print("[%s] %s（无变化）" % (
                    datetime.now().strftime("%H:%M:%S"), result["state"]), flush=True)
            last_state = result["state"]
            if index + 1 < polls:
                time.sleep(args.watch)
    finally:
        if stream:
            stream.close()
    return 0 if results and results[-1]["connected"] else 1


if __name__ == "__main__":
    sys.exit(main())
