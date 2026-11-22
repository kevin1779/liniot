"""设备上下线事件巡检与推送：状态翻转时更新落库并复用现有推送通道（Issue #11）。

两个翻转方向的发现路径不同：
- 离线：后台巡检线程按 online_timeout / 2 的间隔检查，落库 status 滞后于
  惰性判定出的 effective_status 时执行翻转并推送；
- 上线：store.touch() 会同步把 status 置回 online，巡检永远看不到该翻转，
  由 DeviceService.ingest 在恢复上报时回调 notify_online 即时推送。

通知复用 rules.py 的 notify_webhook（通用回调透传事件 JSON）与
notify_channels（群机器人按渠道报文推送文案），通道识别逻辑无感知。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .rules import device_event_text, notify_channels, notify_webhook

logger = logging.getLogger("liniot.device_events")


class DeviceEventMonitor:
    """设备在线状态巡检器；仿 ModbusService 的后台线程模式，测试可直接调 check_once。"""

    def __init__(self, store, devices,
                 webhook_url: str = "", alert_webhooks: tuple = (),
                 enabled: bool = True) -> None:
        self.store = store
        self.devices = devices            # 仅用 view() 的在线判定与 online_timeout
        self.webhook_url = webhook_url
        self.alert_webhooks = tuple(alert_webhooks)
        self.enabled = enabled
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def interval(self) -> float:
        # 间隔取超时的一半，保证离线发现延迟不超时；下限 1 秒防配置过小空转
        return max(self.devices.online_timeout / 2.0, 1.0)

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run,
                                        name="liniot-device-events", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception:
                logger.exception("设备状态巡检失败，%s 秒后重试", self.interval)
            self._stop.wait(self.interval)

    # ---------------------------------------------------------------- 巡检与推送

    def check_once(self) -> list[dict[str, Any]]:
        """巡检一轮：对 view() 判定的 effective_status 与落库 status 不一致的
        设备执行翻转并推送事件，返回本轮翻转列表。"""
        if not self.enabled:
            return []
        events: list[dict[str, Any]] = []
        for device in self.store.list_devices():
            effective = self.devices.view(device)["effective_status"]
            if effective == device["status"]:
                continue
            self.store.set_status(device["id"], effective)
            events.append({"event": effective, "device_id": device["id"],
                           "device_name": device["name"], "ts": time.time()})
        for event in events:
            self._push(event)
            logger.warning("设备%s: %s (%s)",
                           "上线" if event["event"] == "online" else "离线",
                           event["device_name"], event["device_id"])
        return events

    def notify_online(self, device_id: str) -> None:
        """设备恢复上报时由 DeviceService.ingest 回调：touch() 已置 online，这里即时推送。"""
        if not self.enabled:
            return
        device = self.store.get_device(device_id)
        if device is None:
            return
        event = {"event": "online", "device_id": device_id,
                 "device_name": device["name"], "ts": time.time()}
        self._push(event)
        logger.warning("设备上线: %s (%s)", device["name"], device_id)

    def _push(self, event: dict[str, Any]) -> None:
        """推送单个事件：通用回调透传事件 JSON，群机器人推送文案。"""
        notify_webhook(self.webhook_url, event)
        notify_channels(self.alert_webhooks, event, text=device_event_text(event))
