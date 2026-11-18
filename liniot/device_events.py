"""设备上线/离线事件：后台巡检在线状态翻转，并复用告警推送通道通知。

离线是按 last_seen 惰性判定的，没有事件触发点，因此用一个轻量后台线程
（间隔 online_timeout / 2）定期比对，仿照 ModbusService 的线程模式。
"""
from __future__ import annotations

import logging
import threading
import time

from .rules import notify_channels, notify_webhook

logger = logging.getLogger("liniot.device_events")


class DeviceEventMonitor:
    def __init__(self, store, devices, webhook_url: str = "",
                 alert_webhooks: tuple = (), interval: float = 45.0,
                 enabled: bool = True) -> None:
        self.store = store
        self.devices = devices
        self.webhook_url = webhook_url
        self.alert_webhooks = tuple(alert_webhooks)
        self.interval = max(float(interval), 1.0)
        self.enabled = enabled
        # 已知的有效在线态；首次见到某设备只记录基线，不发通知
        self._known: dict[str, str] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="liniot-device-events",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception:
                logger.exception("设备在线状态巡检失败")
            self._stop.wait(self.interval)

    # ---------------------------------------------------------------- 巡检

    def check_once(self) -> list[dict]:
        """巡检一轮，返回本轮发出的事件列表。"""
        events: list[dict] = []
        seen: set[str] = set()
        for device in self.store.list_devices():
            device_id = device["id"]
            seen.add(device_id)
            current = self.devices.view(device)["effective_status"]
            previous = self._known.get(device_id)
            self._known[device_id] = current
            if current == "offline" and device["status"] == "online":
                # 落库值滞后于真实在线态：补写离线状态
                self.store.set_status(device_id, "offline")
            if previous is None or previous == current:
                continue
            event = {"event": current, "device_id": device_id,
                     "device_name": device["name"], "ts": time.time()}
            events.append(event)
            self._notify(event)
        for gone in set(self._known) - seen:
            del self._known[gone]
        return events

    def _notify(self, event: dict) -> None:
        label = "已上线" if event["event"] == "online" else "已离线"
        text = f"设备 {event['device_name']} ({event['device_id']}) {label}"
        logger.warning("设备状态翻转: %s", text)
        notify_webhook(self.webhook_url, event)
        notify_channels(self.alert_webhooks, {**event, "message": text})
