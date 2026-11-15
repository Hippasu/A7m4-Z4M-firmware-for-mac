"""Shared vocabulary for the macOS -> A7M4 0336 staged probe.

Nothing in this module talks to a camera. It only defines identifiers,
result containers and the safety tiers used by the runner.
"""

from __future__ import annotations

import hashlib
import platform
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# Sony ILCE-7M4 USB identities, taken from the upstream project's
# no-fastboot-install/software/app/protocol/auth.py
SONY_VID = 0x054C
PID_NORMAL_MSC = 0x0DA5          # 3493: normal USB mass-storage identity
PID_SERVICE = 0x0336             # 822: Senser/Jiritsu service identity
PID_SENSER_ALT = 0x02A9          # 681: alternate service identity
EXPECTED_PRODUCT = "ILCE-7M4"

# Fastboot identity is a different device and must never be confused with 0336.
FASTBOOT_VID = 0x18D1            # Google/Android fastboot host-side VID
PID_FASTBOOT = 0x4EE0            # VID_18D1 & PID_4EE0, Android fastboot

# The ONLY identities that may be called "the A7M4". A USB disk, a hub or any
# other Sony gadget fails this test and is reported as ignored, never as a camera.
CAMERA_PIDS = (PID_NORMAL_MSC, PID_SERVICE, PID_SENSER_ALT)
IDENTITY_RULE = ("VID=054C(Sony) 且 PID∈{0da5,0336,02a9} 才可能是 A7M4；"
                 "再用 iProduct=ILCE-7M4 复核。任何 USB 移动硬盘/集线器/其他 Sony 设备"
                 "的 VID、PID、产品字符串均无法通过此门。")

# ---------------------------------------------------------------------------
# Body-model identification (not the same thing as a USB identity)
# ---------------------------------------------------------------------------
# Sony allocates a separate PID group per body model AND per USB connection
# mode (e.g. ILCE-7M3: 0C01 auto / 0C02 MSC / 0C03 MTP / 0C34 PC Remote;
# ILCE-6000: 07C3/07C4 MSC / 08B7 MTP / 094E PC Remote). The PID gate is
# therefore already a strong model filter, but a PID alone cannot *prove* the
# body is an ILCE-7M4 -- it is firmware-reported and could be reused. The
# descriptor's product string (iProduct / "USB Product Name") is the only
# piece of evidence that actually names the body.

TARGET_MODEL = "ILCE-7M4"
TARGET_MODEL_NORM = "ILCE7M4"      # ILCE-7M4 / ILCE-7M4K / ILCE-7M4M / ILCE7M4B

# Sony camera product strings start with one of these. Deliberately broad:
# a ZV-E10, an ILCE-7M3 or an FX3 must be reported as "another Sony camera",
# never as "the A7M4 with a wrong USB mode".
SONY_CAMERA_PREFIXES = (
    "ILCE", "ILC", "ILME", "NEX", "SLT", "DSLR", "DSC", "ZV", "ZV1",
    "HDR", "DCR", "HXR", "PXW", "FX", "MHC", "MVC", "CYBERSHOT",
)

MODEL_A7M4 = "A7M4"
MODEL_OTHER_SONY_CAMERA = "OTHER_SONY_CAMERA"
MODEL_OTHER_SONY_DEVICE = "OTHER_SONY_DEVICE"
MODEL_NON_SONY = "NON_SONY"
MODEL_UNKNOWN = "UNKNOWN"

MODEL_RULE = ("以产品字符串（iProduct / USB Product Name）判定机身型号："
              "ILCE-7M4* = 目标机型；ILCE-*/DSC-*/ZV-* 等 = 其他索尼相机；"
              "其余 VID=054C = Sony 非相机配件；非 054C = 非 Sony 设备。"
              "产品字符串缺失时标 UNKNOWN，不臆测型号。")


# Product strings that appear during Sony USB re-enumeration / mode switch.
# They do not name a body; treat as UNKNOWN so --strict-model does not exclude
# a real 0336/0DA5 session that briefly advertises a generic string.
GENERIC_SONY_PRODUCT_NORMS = frozenset({
    "USBBUSPOWEREDDEVICE",
    "USBDEVICE",
    "SONYUSBDEVICE",
    "COMPOSITEUSBDEVICE",
})

# PIDs observed on this Mac while A7M4 is mid-transition (MSC ↔ service).
# Not camera identities; messaging must not claim "wrong body".
TRANSITIONAL_SONY_PIDS = frozenset({0x0D68, 0x096F})


def classify_model(vid: int | None, product: str | None) -> tuple[str, str]:
    """Return ``(model_kind, model_name)`` from a USB product string.

    Pure function, no I/O. ``MODEL_UNKNOWN`` means the host could not read the
    product string -- never guess a model in that case.
    """
    raw = (product or "").strip()
    if not raw:
        return MODEL_UNKNOWN, ""
    norm = re.sub(r"[\s_\-.]+", "", raw).upper()
    for prefix in ("SONYCORPORATION", "SONY"):
        if norm.startswith(prefix):
            norm = norm[len(prefix):]
            break
    if not norm:
        return MODEL_UNKNOWN, raw
    if norm.startswith(TARGET_MODEL_NORM):
        return MODEL_A7M4, TARGET_MODEL
    # Generic re-enumeration strings: unproven body, not "wrong gadget".
    if norm in GENERIC_SONY_PRODUCT_NORMS:
        return MODEL_UNKNOWN, raw
    if vid == SONY_VID:
        if norm.startswith(SONY_CAMERA_PREFIXES):
            return MODEL_OTHER_SONY_CAMERA, raw
        return MODEL_OTHER_SONY_DEVICE, raw
    return MODEL_NON_SONY, raw

# Common host-side error codes worth naming explicitly in reports.
LIBUSB_ERROR_NAMES = {
    -1: "LIBUSB_ERROR_IO",
    -2: "LIBUSB_ERROR_INVALID_PARAM",
    -3: "LIBUSB_ERROR_ACCESS",
    -4: "LIBUSB_ERROR_NO_DEVICE",
    -5: "LIBUSB_ERROR_NOT_FOUND",
    -6: "LIBUSB_ERROR_BUSY",
    -7: "LIBUSB_ERROR_TIMEOUT",
    -8: "LIBUSB_ERROR_OVERFLOW",
    -9: "LIBUSB_ERROR_PIPE",
    -11: "LIBUSB_ERROR_NO_MEM",
    -12: "LIBUSB_ERROR_NOT_SUPPORTED",
    -99: "LIBUSB_ERROR_OTHER",
}
IOKIT_ERROR_NAMES = {
    0xE00002BC: "kIOReturnError",
    0xE00002BD: "kIOReturnNoMemory",
    0xE00002BE: "kIOReturnNoResources",
    0xE00002BF: "kIOReturnIPCError",
    0xE00002C0: "kIOReturnExclusiveAccess",
    0xE00002C5: "kIOReturnNotPermitted",
}


class Tier(Enum):
    """What a stage is allowed to touch."""

    HOST_ONLY = 0        # no USB I/O at all: OS facts, process enumeration
    USB_READONLY = 1     # standard GET_DESCRIPTOR / host-side claim only
    DEVICE_COMMANDS = 2  # Senser authentication, changes USB identity
    INSTALL_WRITES = 3   # p1 / menu RAM writes (needs --enable-writes twin flags)


class Status(Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"
    BLOCKED = "BLOCKED"


class Verdict(Enum):
    FEASIBLE = "FEASIBLE"
    BLOCKED_HOST = "BLOCKED_HOST"
    BLOCKED_OCCUPANCY = "BLOCKED_OCCUPANCY"
    BLOCKED_TRANSITION = "BLOCKED_TRANSITION"
    BLOCKED_SERVICE = "BLOCKED_SERVICE"
    BLOCKED_RESTORE = "BLOCKED_RESTORE"
    UNSTABLE = "UNSTABLE"
    INCOMPLETE = "INCOMPLETE"


@dataclass
class DeviceInfo:
    vid: int
    pid: int
    bus: int | None = None
    address: int | None = None
    location_id: str | None = None
    session_id: str | None = None
    product: str | None = None
    serial: str | None = None
    manufacturer: str | None = None
    port_path: tuple[int, ...] = ()
    kernel_owner: str | None = None
    source: str = "unknown"

    @property
    def label(self) -> str:
        return f"{self.vid:04x}:{self.pid:04x}"

    @property
    def serial_sha256(self) -> str | None:
        """Body binding used by upstream: never persist the raw serial."""
        if not self.serial:
            return None
        return hashlib.sha256(self.serial.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.label,
            "vid": hex(self.vid),
            "pid": hex(self.pid),
            "bus": self.bus,
            "address": self.address,
            "location_id": self.location_id,
            "session_id": self.session_id,
            "product": self.product,
            "manufacturer": self.manufacturer,
            "serial_sha256": self.serial_sha256,
            "port_path": list(self.port_path),
            "kernel_owner": self.kernel_owner,
            "source": self.source,
        }


@dataclass
class StageResult:
    stage: str
    title: str
    tier: Tier
    status: Status
    summary: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    blocking_codes: list[str] = field(default_factory=list)
    # Non-fatal notes that must stay visible next to a PASS (e.g. a
    # ProductInfo HASP sub-call failing while the service session works).
    # They never decide the verdict, but they must never be dropped either.
    warnings: list[str] = field(default_factory=list)
    camera_commands: int = 0
    elapsed_ms: int = 0
    started_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "title": self.title,
            "tier": self.tier.name,
            "status": self.status.value,
            "summary": self.summary,
            "evidence": self.evidence,
            "errors": self.errors,
            "blocking_codes": self.blocking_codes,
            "warnings": self.warnings,
            "camera_commands": self.camera_commands,
            "elapsed_ms": self.elapsed_ms,
            "started_at": self.started_at,
        }


@dataclass
class RunReport:
    run_id: str
    started_at: str
    host: dict[str, Any]
    simulate: str | None
    stages_requested: list[str]
    results: list[StageResult] = field(default_factory=list)
    verdict: Verdict = Verdict.INCOMPLETE
    verdict_reason: str = ""
    next_steps: list[str] = field(default_factory=list)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    finished_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "m4-mac-0336-probe/1",
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "host": self.host,
            "simulate": self.simulate,
            "stages_requested": self.stages_requested,
            "results": [r.to_dict() for r in self.results],
            "verdict": self.verdict.value,
            "verdict_reason": self.verdict_reason,
            "next_steps": self.next_steps,
            "timeline": self.timeline,
            "camera_commands_total": sum(r.camera_commands for r in self.results),
        }


def host_basics() -> dict[str, Any]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def name_error(code: int | str | None) -> str:
    if code is None:
        return "none"
    try:
        value = int(code)
    except (TypeError, ValueError):
        return str(code)
    if value in LIBUSB_ERROR_NAMES:
        return LIBUSB_ERROR_NAMES[value]
    unsigned = value & 0xFFFFFFFF
    if unsigned in IOKIT_ERROR_NAMES:
        return IOKIT_ERROR_NAMES[unsigned]
    return f"0x{unsigned:08x}"
