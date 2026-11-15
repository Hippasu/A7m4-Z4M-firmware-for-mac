"""Event-driven camera-link monitoring for macOS (IOKit notifications).

The legacy monitor asked "is the camera there?" every N seconds. This module
instead registers ``IOServiceAddMatchingNotification`` for USB device arrival
and termination, and only takes a snapshot when the kernel signals a change:

    IOKit notification -> CFRunLoop source drains the iterator -> wake up
    -> quick_link() read-only snapshot -> emit a ``connection`` event only
    if the (state, USB mode, MSC volume readiness) tuple actually changed.

Besides USB device arrival/termination we also watch mass-storage driver and
IOMedia objects, so "the camera's MSC volume finished mounting" is an event
too (it produces no USB device notification). The snapshot explicitly names
the camera's USB connection mode: 054C:0DA5 = Mass Storage(MSC), 0336/02A9 =
Senser service, and other Sony PIDs = a wrong mode (MTP / PC Remote /
USB Streaming) that the 0336 chain cannot use.

Important details that are easy to get wrong (all verified on macOS 15.4.1):

* The notification type strings in IOKitKeys.h are ``"IOServiceFirstMatch"``
  and ``"IOServiceTerminate"`` -- NOT the longer ``"...Notification"`` names
  often copied from old sample code. Wrong names make the kernel return
  ``kIOReturnUnsupported`` (0xE00002C7) for *every* registration.
* The notification is not armed until its iterator is drained with
  ``IOIteratorNext``. Registration must be followed by a drain.
* A CFRunLoop is bound to the thread that created it, so the port, source,
  registrations and ``CFRunLoopRunInMode`` must all live on the monitor thread.
* ``CFRunLoopRunInMode`` (via ctypes.CDLL) releases the GIL, so a blocked
  monitor does not freeze the stages running on the main thread.
* The Python CFUNCTYPE callback must be kept alive for as long as the port is
  registered, otherwise a notification can call into freed memory.

There is a deliberate fallback: if IOKit is unavailable (non-macOS, missing
framework, registration refused) or the backend is simulated (no real USB
events can ever arrive), the monitor degrades to the legacy polling loop and
records the reason in the event log. Nothing here touches the camera: the
snapshot uses the fast, read-only IORegistry path only.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
import threading
import time
from typing import Any, Callable

from .live import ACTOR_GATEKEEPER

# Values copied verbatim from IOKit/IOKitKeys.h -- do not "fix" these names.
NOTIFY_FIRST_MATCH = b"IOServiceFirstMatch"
NOTIFY_TERMINATED = b"IOServiceTerminate"

# IOUSBHostDevice is the modern class (macOS 10.11+); IOUSBDevice is kept for
# older kernels. Registering both is harmless: snapshots are de-duplicated by
# the "only emit on state change" rule.
USB_SERVICE_CLASSES = (b"IOUSBHostDevice", b"IOUSBDevice")
# Volume mounting does not create/terminate a USB device, so register these
# too. Verified on macOS 15.4.1: all four classes accept both notifications.
STORAGE_SERVICE_CLASSES = (b"IOUSBMassStorageDriver", b"IOMedia")
DEFAULT_SERVICE_CLASSES = USB_SERVICE_CLASSES + STORAGE_SERVICE_CLASSES
NOTIFICATION_TYPES = (NOTIFY_FIRST_MATCH, NOTIFY_TERMINATED)

KERN_SUCCESS = 0

# How long CFRunLoopRunInMode blocks before we re-check the stop flag. This is
# NOT a polling interval: no USB query happens on a timeout. It only bounds how
# quickly the daemon thread can exit. stop() also calls CFRunLoopWakeUp.
RUNLOOP_WAKE_INTERVAL = 0.4

# ctypes callback signature: void (*)(void *refCon, io_iterator_t iterator)
_IOServiceMatchingCallback = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint)

NotifierFactory = Callable[[], "IOKitNotifier"]


class IOKitUnavailable(RuntimeError):
    """Raised when the event-driven path cannot be set up on this host."""


class ThrottledStorageProbe:
    """Read-only ``diskutil`` check, cached for a short window.

    IOMedia can fire several notifications for a single mount (whole disk,
    partition, APFS container...). Without throttling each one would spawn a
    diskutil walk. Results are reused for ``min_interval`` seconds.
    """

    def __init__(self, probe: Callable[[], dict[str, Any]] | None = None,
                 min_interval: float = 2.0) -> None:
        self._probe = probe
        self._min_interval = max(0.0, min_interval)
        self._last: dict[str, Any] | None = None
        self._last_at = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            if self._last is not None and now - self._last_at < self._min_interval:
                return self._last
            try:
                if self._probe is None:
                    from .detect import probe_storage
                    self._last = probe_storage()
                else:
                    self._last = self._probe()
            except Exception as exc:
                self._last = {"ok": False, "mounted": False,
                              "error": "%s: %s" % (type(exc).__name__, exc)}
            self._last_at = now
            return self._last


def take_snapshot(backend: Any,
                  storage_probe: Callable[[], dict[str, Any]] | None = None,
                  strict_model: bool = False
                  ) -> dict[str, Any]:
    """Read-only link snapshot; monitoring must never break the probe."""
    try:
        from .detect import quick_link
        return quick_link(backend, storage_probe=storage_probe,
                          strict_model=strict_model)
    except Exception as exc:
        return {"state": "UNKNOWN", "connected": False,
                "note": "监测异常：%s: %s" % (type(exc).__name__, exc),
                "pids": [], "products": [],
                "mode": "UNKNOWN", "mode_text": "模式未判定",
                "mode_hint": "", "mode_is_msc": False,
                "other_sony_devices": [], "msc_ready": None,
                "mounted_volumes": [], "model": "",
                "model_match": "UNKNOWN", "model_text": "",
                "model_hint": "", "target_a7m4": None,
                "observed_sony_devices": []}


# ---------------------------------------------------------------------------
# IOKit / CoreFoundation ctypes bindings
# ---------------------------------------------------------------------------

class _Frameworks:
    """Loads and binds the handful of C entry points we need."""

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise IOKitUnavailable("IOKit 只在 macOS 上可用（当前 %s）" % sys.platform)
        iokit_path = ctypes.util.find_library("IOKit")
        cf_path = ctypes.util.find_library("CoreFoundation")
        if not iokit_path or not cf_path:
            raise IOKitUnavailable("找不到 IOKit / CoreFoundation 框架")
        try:
            self.iokit = ctypes.CDLL(iokit_path)
            self.cf = ctypes.CDLL(cf_path)
        except OSError as exc:
            raise IOKitUnavailable("加载框架失败：%s" % exc) from exc
        try:
            self._bind()
            self.mode = ctypes.c_void_p.in_dll(self.cf, "kCFRunLoopDefaultMode")
        except (AttributeError, ValueError) as exc:
            raise IOKitUnavailable("IOKit 符号/常量缺失：%s" % exc) from exc

    def _bind(self) -> None:
        iokit, cf = self.iokit, self.cf
        uint, void = ctypes.c_uint, ctypes.c_void_p
        uint_p = ctypes.POINTER(uint)

        iokit.IONotificationPortCreate.argtypes = [uint]
        iokit.IONotificationPortCreate.restype = void
        iokit.IONotificationPortGetRunLoopSource.argtypes = [void]
        iokit.IONotificationPortGetRunLoopSource.restype = void
        iokit.IONotificationPortDestroy.argtypes = [void]

        iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
        iokit.IOServiceMatching.restype = void
        iokit.IOServiceAddMatchingNotification.argtypes = [
            void, ctypes.c_char_p, void, _IOServiceMatchingCallback, void, uint_p]
        iokit.IOServiceAddMatchingNotification.restype = ctypes.c_int
        iokit.IOIteratorNext.argtypes = [uint]
        iokit.IOIteratorNext.restype = uint
        iokit.IOObjectRelease.argtypes = [uint]
        iokit.IOObjectRelease.restype = ctypes.c_int

        cf.CFRunLoopGetCurrent.restype = void
        cf.CFRunLoopAddSource.argtypes = [void, void, void]
        cf.CFRunLoopRemoveSource.argtypes = [void, void, void]
        cf.CFRunLoopRunInMode.argtypes = [void, ctypes.c_double, ctypes.c_bool]
        cf.CFRunLoopRunInMode.restype = ctypes.c_int32
        cf.CFRunLoopWakeUp.argtypes = [void]

    def drain(self, iterator: int) -> int:
        """Empty a notification iterator. Required to re-arm the notification."""
        count = 0
        while True:
            obj = self.iokit.IOIteratorNext(ctypes.c_uint(iterator))
            if not obj:
                return count
            count += 1
            self.iokit.IOObjectRelease(ctypes.c_uint(obj))


class IOKitNotifier:
    """Owns one notification port on the calling thread.

    Create it on the monitor thread (CFRunLoop is thread-bound). ``wait``
    blocks that thread in the run loop until a USB service arrives/terminates
    or the timeout elapses; it returns True only when a real notification came.
    """

    def __init__(self, frameworks: _Frameworks | None = None,
                 *, classes: tuple[bytes, ...] = DEFAULT_SERVICE_CLASSES,
                 types: tuple[bytes, ...] = NOTIFICATION_TYPES) -> None:
        self.fw = frameworks or _Frameworks()
        self._wake = threading.Event()
        # Keep a strong reference: the C port holds a raw pointer to this
        # callback for as long as it is registered.
        self._callback = _IOServiceMatchingCallback(self._on_notify)
        self._port: int | None = None
        self._source: int | None = None
        self._runloop: int | None = None
        self._iterators: list[int] = []
        self._closed = False

        try:
            self._port = self.fw.iokit.IONotificationPortCreate(0)
            if not self._port:
                raise IOKitUnavailable("IONotificationPortCreate 返回空端口")
            self._source = self.fw.iokit.IONotificationPortGetRunLoopSource(self._port)
            if not self._source:
                raise IOKitUnavailable("IONotificationPortGetRunLoopSource 返回空源")
            self._runloop = self.fw.cf.CFRunLoopGetCurrent()
            self.fw.cf.CFRunLoopAddSource(self._runloop, self._source, self.fw.mode)

            for cls in classes:
                for kind in types:
                    iterator = ctypes.c_uint(0)
                    # IOServiceMatching() returns a +1 dictionary which the
                    # registration consumes; build a fresh one every time.
                    matching = self.fw.iokit.IOServiceMatching(cls)
                    kr = self.fw.iokit.IOServiceAddMatchingNotification(
                        self._port, kind, matching, self._callback,
                        None, ctypes.byref(iterator))
                    if kr != KERN_SUCCESS:
                        raise IOKitUnavailable(
                            "IOServiceAddMatchingNotification(%s, %s) 失败：0x%08x"
                            % (cls.decode(), kind.decode(), kr & 0xFFFFFFFF))
                    self._iterators.append(iterator.value)
                    self.fw.drain(iterator.value)  # arm the notification
            # The initial match delivery is already covered by the synchronous
            # snapshot; do not treat it as a change.
            self._wake.clear()
        except Exception:
            self.close()
            raise

    def _on_notify(self, refcon: int, iterator: int) -> None:
        # Draining inside the callback is mandatory: until the iterator is
        # empty the kernel will not deliver the next notification.
        self.fw.drain(iterator)
        self._wake.set()

    def wait(self, timeout: float = RUNLOOP_WAKE_INTERVAL) -> bool:
        """Block in the run loop; True means a USB service changed."""
        if self._closed:
            return False
        self.fw.cf.CFRunLoopRunInMode(self.fw.mode, float(timeout), True)
        fired = self._wake.is_set()
        self._wake.clear()
        return fired

    def wake(self) -> None:
        """Unblock wait() from another thread so stop() can join promptly."""
        if not self._closed and self._runloop:
            self.fw.cf.CFRunLoopWakeUp(self._runloop)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        fw = self.fw
        try:
            if self._source and self._runloop:
                fw.cf.CFRunLoopRemoveSource(self._runloop, self._source, fw.mode)
            for iterator in self._iterators:
                if iterator:
                    fw.iokit.IOObjectRelease(ctypes.c_uint(iterator))
            if self._port:
                fw.iokit.IONotificationPortDestroy(self._port)
        finally:
            self._iterators = []
            self._port = self._source = self._runloop = None
        # Release the callback only after the port has been destroyed.
        self._callback = None  # type: ignore[assignment]


def make_iokit_notifier() -> IOKitNotifier:
    """Default factory; injectable in tests."""
    return IOKitNotifier()


# ---------------------------------------------------------------------------
# Monitor loops
# ---------------------------------------------------------------------------

def _snapshot_key(snap: dict[str, Any]
                  ) -> tuple[str | None, str | None, bool | None, str | None]:
    """State, USB mode, MSC readiness and body model are all reportable changes.

    The model matters: swapping an A7M4 for another Sony body while the PID /
    mode happens to stay the same must still produce a connection event.
    """
    return (snap.get("state"), snap.get("mode"), snap.get("msc_ready"),
            snap.get("model_match"))


def _changed(key: tuple[Any, ...], last: Any) -> bool:
    if last is None:
        return True
    # Legacy callers pass a plain state string (or a state-only tuple with
    # unknown mode/readiness); keep their state-only comparison semantics.
    if isinstance(last, str):
        return key[0] != last
    if isinstance(last, tuple) and len(last) == 3 and last[1] is None and last[2] is None:
        return key[0] != last[0]
    return key != last


def _report_change(log: Any, snap: dict[str, Any], last: Any,
                   on_snapshot: Callable[[dict[str, Any]], None] | None) -> Any:
    if on_snapshot is not None:
        on_snapshot(snap)
    key = _snapshot_key(snap)
    if _changed(key, last):
        log.connection(snap)
        return key
    return last if last is not None else key


def run_poll_monitor(backend: Any, log: Any, interval: float,
                     stop: threading.Event, last_state: str | None = None,
                     on_snapshot: Callable[[dict[str, Any]], None] | None = None,
                     storage_probe: Callable[[], dict[str, Any]] | None = None,
                     strict_model: bool = False) -> None:
    """Legacy timed polling loop (kept as the portable fallback)."""
    last: Any = (last_state, None, None) if isinstance(last_state, str) else last_state
    while True:
        snap = take_snapshot(backend, storage_probe, strict_model)
        last = _report_change(log, snap, last, on_snapshot)
        if stop.wait(max(0.2, interval)):
            return


def run_event_monitor(backend: Any, log: Any, stop: threading.Event,
                      notifier: IOKitNotifier, last_state: str | None = None,
                      *, heartbeat: float = 0.0,
                      on_snapshot: Callable[[dict[str, Any]], None] | None = None,
                      storage_probe: Callable[[], dict[str, Any]] | None = None,
                      strict_model: bool = False) -> None:
    """Snapshot only when IOKit signals a USB / mass-storage change.

    ``heartbeat`` (default 0) optionally re-verifies the link every N seconds
    to catch a lost notification. With 0 the monitor is purely event-driven.
    """
    last: Any = (last_state, None, None) if isinstance(last_state, str) else last_state
    last_check = time.monotonic()
    while not stop.is_set():
        fired = notifier.wait(RUNLOOP_WAKE_INTERVAL)
        if stop.is_set():
            break
        now = time.monotonic()
        due = heartbeat > 0 and (now - last_check) >= heartbeat
        if not fired and not due:
            continue
        last_check = now
        snap = take_snapshot(backend, storage_probe, strict_model)
        last = _report_change(log, snap, last, on_snapshot)


# ---------------------------------------------------------------------------
# Unified entry point
# ---------------------------------------------------------------------------

class ConnectionMonitor:
    """One-shot initial snapshot + a background change monitor.

    ``mode``:
      ``auto``  - IOKit events on real macOS, polling when unavailable (default)
      ``iokit`` - request IOKit explicitly (still degrades safely if refused)
      ``poll``  - force the legacy polling loop (reproducible regression runs)

    The simulated backend always uses polling: no kernel USB event can ever
    describe a fixture change, so event-driven monitoring would see nothing.
    """

    MODES = ("auto", "iokit", "poll")

    def __init__(self, backend: Any, log: Any, *, mode: str = "auto",
                 poll_interval: float = 2.0, heartbeat: float = 0.0,
                 simulate: bool = False, enabled: bool = True,
                 volume_probe: bool = True,
                 storage_probe: Callable[[], dict[str, Any]] | None = None,
                 notifier_factory: NotifierFactory | None = None,
                 strict_model: bool = False) -> None:
        if mode not in self.MODES:
            raise ValueError("unknown connection monitor mode: " + mode)
        self.backend = backend
        self.log = log
        self.mode = mode
        self.poll_interval = max(0.2, poll_interval)
        self.heartbeat = max(0.0, heartbeat)
        self.simulate = simulate
        self.enabled = enabled
        self.strict_model = bool(strict_model)
        # An injected probe wins; otherwise use the throttled read-only
        # diskutil check unless the caller explicitly disabled volume probing.
        if storage_probe is not None:
            self._storage_probe = storage_probe
        else:
            self._storage_probe = ThrottledStorageProbe() if volume_probe else None
        self._factory = notifier_factory or make_iokit_notifier
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._mode_ready = threading.Event()
        self._notifier: IOKitNotifier | None = None
        self._cache_lock = threading.Lock()
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0
        self.mode_used = "disabled"
        self.fallback_reason: str | None = None
        self.initial: dict[str, Any] | None = None

    def describe(self) -> str:
        model_note = ("；机型定向：仅 ILCE-7M4 计入目标" if self.strict_model
                      else "；机型仅标注不拦截")
        if self.mode_used == "disabled":
            return "连接监测已关闭（仅保留一次性快照）" + model_note
        if self.mode_used == "iokit":
            extra = ("；每 %.1fs 校验一次" % self.heartbeat) if self.heartbeat else "；纯事件驱动，无定时轮询"
            detail = "；监测 USB 连接模式与 MSC 卷挂载" if self._storage_probe is not None else "；仅 PID 模式识别"
            return "IOKit 事件驱动（USB 设备/存储卷变化即时触发）" + extra + detail + model_note
        reason = ("，原因：%s" % self.fallback_reason) if self.fallback_reason else ""
        return "轮询降级（每 %.1fs 一次）%s%s" % (self.poll_interval, reason, model_note)

    def latest(self, max_age: float | None = None) -> dict[str, Any]:
        """Cached last snapshot, for UIs that need to render current state.

        ``max_age`` (seconds): if the cache is older, take a cheap IORegistry
        refresh so missed IOKit events / transitional PIDs cannot stick in the
        connection card forever. ``None`` returns the cache as-is.
        """
        with self._cache_lock:
            snap = dict(self._cached) if self._cached is not None else {}
            age = time.monotonic() - self._cached_at if self._cached is not None else None
        if max_age is None:
            return snap
        if self._cached is not None and age is not None and age <= max_age:
            return snap
        fresh = take_snapshot(self.backend, self._storage_probe, self.strict_model)
        self._cache(fresh)
        return dict(fresh)

    def _cache(self, snap: dict[str, Any]) -> None:
        with self._cache_lock:
            self._cached = snap
            self._cached_at = time.monotonic()

    def start(self) -> dict[str, Any]:
        """Emit the mandatory first connection snapshot, then start watching."""
        snap = take_snapshot(self.backend, self._storage_probe, self.strict_model)
        self.log.connection(snap)          # always the first event of a run
        self._cache(snap)
        self.initial = snap
        if not self.enabled:
            self.mode_used = "disabled"
            self._mode_ready.set()
            self.log.emit("step", "", ACTOR_GATEKEEPER, "c " + self.describe())
            return snap

        self._thread = threading.Thread(target=self._worker,
                                        args=(_snapshot_key(snap),),
                                        daemon=True, name="connection-monitor")
        self._thread.start()
        self._mode_ready.wait(timeout=3.0)
        self.log.emit("step", "", ACTOR_GATEKEEPER, "c " + self.describe())
        return snap

    def _worker(self, last_state: Any) -> None:
        notifier: IOKitNotifier | None = None
        chosen = "poll"
        if self.mode in ("auto", "iokit") and not self.simulate:
            try:
                notifier = self._factory()
                chosen = "iokit"
            except Exception as exc:
                self.fallback_reason = "%s: %s" % (type(exc).__name__, exc)
        elif self.simulate:
            self.fallback_reason = "模拟后端没有真实 USB 事件"
        elif self.mode == "poll":
            self.fallback_reason = "显式指定 --connection-monitor poll"

        self.mode_used = chosen
        self._notifier = notifier
        self._mode_ready.set()
        try:
            if notifier is not None:
                run_event_monitor(self.backend, self.log, self._stop, notifier,
                                  last_state, heartbeat=self.heartbeat,
                                  on_snapshot=self._cache,
                                  storage_probe=self._storage_probe,
                                  strict_model=self.strict_model)
            else:
                run_poll_monitor(self.backend, self.log, self.poll_interval,
                                 self._stop, last_state, on_snapshot=self._cache,
                                 storage_probe=self._storage_probe,
                                 strict_model=self.strict_model)
        finally:
            if notifier is not None:
                notifier.close()
            self._notifier = None

    def stop(self, timeout: float = 5.0) -> None:
        """Idempotent shutdown; wakes the run loop so join() returns fast."""
        self._stop.set()
        if self._notifier is not None:
            try:
                self._notifier.wake()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
