"""设备服务：统一封装设备注册、遥测入库、状态维护、命令下发。

HTTP 与 MQTT 两条接入通道都汇聚到这里，保证无论设备走哪种协议，
存储、规则判断与实时推送的行为完全一致。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

from .anomaly import AnomalyDetector
from .bus import EventBus
from .rules import RuleEngine, notify_channels, notify_webhook

logger = logging.getLogger("liniot.service")

METRIC_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")


class UnknownDevice(KeyError):
    pass


class DeviceService:
    def __init__(self, store, bus: EventBus, rules: RuleEngine,
                 mqtt=None, online_timeout: float = 90.0,
                 webhook_url: str = "", alert_webhooks: tuple = (),
                 alert_template: str = "") -> None:
        self.store = store
        self.bus = bus
        self.rules = rules
        self.mqtt = mqtt  # 可选的 MQTT 服务，启用时命令同时经 MQTT 下发
        self.online_timeout = online_timeout
        self.webhook_url = webhook_url
        self.alert_webhooks = tuple(alert_webhooks)
        self.alert_template = alert_template
        self.anomaly = AnomalyDetector()
        # SceneEngine 由 create_app 在装配阶段注入，避免构造循环依赖
        self.scene_engine = None

    # ------------------------------------------------------------------ 注册

    def register(self, name: str, type_: str = "generic",
                 device_id: Optional[str] = None,
                 meta: Optional[dict[str, str]] = None) -> tuple[dict[str, Any], bool]:
        if device_id is None:
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "device"
            device_id = f"{slug}-{int(time.time()) % 100000:05d}"
        device, created = self.store.upsert_device(device_id, name, type_, meta or {})
        self.bus.publish({"type": "device", "data": self.view(device)})
        return device, created

    def delete(self, device_id: str) -> None:
        self.store.delete_device(device_id)
        self.anomaly.forget(device_id)
        self.bus.publish({"type": "device_deleted", "data": {"id": device_id}})

    # ------------------------------------------------------------------ 数据

    def ingest(self, device_id: str, points: list[dict[str, Any]],
               source: str = "http") -> int:
        if self.store.get_device(device_id) is None:
            raise UnknownDevice(device_id)
        clean: list[dict[str, Any]] = []
        for p in points:
            metric, value = str(p.get("metric", "")), p.get("value")
            if not METRIC_RE.match(metric) or value is None:
                continue
            clean.append({"metric": metric, "value": float(value), "ts": p.get("ts")})
        if not clean:
            return 0
        count = self.store.insert_telemetry(device_id, clean)
        self.store.touch(device_id)
        self.bus.publish({"type": "telemetry", "data": {"device_id": device_id,
                                                        "points": clean, "source": source}})
        self.rules.evaluate(device_id, clean)
        self._evaluate_anomaly(device_id, clean)
        if self.scene_engine is not None:
            self.scene_engine.evaluate(device_id, clean)
        return count

    def _evaluate_anomaly(self, device_id: str, points: list[dict[str, Any]]) -> None:
        """EWMA 基线异常检测：无需配置规则，命中即生成 anomaly 级告警。"""
        for hit in self.anomaly.evaluate(device_id, points):
            message = (f"设备 {device_id} 的 {hit['metric']} = {hit['value']:g} "
                       f"偏离基线 {hit['sigma']}σ（EWMA 均值 {hit['mean']:g}）")
            alert_id = self.store.insert_alert(None, device_id, message, level="anomaly")
            alert = {"id": alert_id, "rule_id": None, "device_id": device_id,
                     "message": message, "level": "anomaly", "ts": time.time(),
                     "diagnosis": None, "acknowledged": 0, "acknowledged_at": None}
            self.bus.publish({"type": "alert", "data": alert})
            notify_webhook(self.webhook_url, alert)
            notify_channels(self.alert_webhooks, alert, self.alert_template)
            logger.warning("异常检测触发: %s", message)

    def set_status(self, device_id: str, status: str,
                   properties: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        device = self.store.set_status(device_id, status, properties)
        if device is None:
            raise UnknownDevice(device_id)
        self.bus.publish({"type": "device", "data": self.view(device)})
        if properties:
            # 设备回报的属性同步进影子 reported（心跳空属性不算状态变化）
            shadow = self.store.set_reported(device_id, properties)
            self.bus.publish({"type": "shadow", "data": self._shadow_view(shadow)})
        return device

    # ------------------------------------------------------------------ 影子

    @staticmethod
    def _shadow_view(shadow: dict[str, Any]) -> dict[str, Any]:
        """附加 delta：desired 与 reported 不一致的键，设备重连一个请求即知该做什么。"""
        out = dict(shadow)
        out["delta"] = {key: value for key, value in shadow["desired"].items()
                        if shadow["reported"].get(key) != value}
        return out

    def get_shadow(self, device_id: str) -> dict[str, Any]:
        if self.store.get_device(device_id) is None:
            raise UnknownDevice(device_id)
        return self._shadow_view(self.store.get_shadow(device_id))

    # ------------------------------------------------------------------ 命令

    def set_properties(self, device_id: str, patch: dict[str, Any]) -> list[dict[str, Any]]:
        """下发烧写设备属性（如 {power: true}）。

        命令进入队列由设备拉取/订阅（与 v0.x 行为一致），同时写入影子
        desired——设备离线时控制意图持久可见，重连后可凭 delta 补齐状态。
        """
        if self.store.get_device(device_id) is None:
            raise UnknownDevice(device_id)
        commands = []
        for key, value in patch.items():
            payload = {"property": key, "value": value}
            command = self.store.queue_command(device_id, payload)
            if self.mqtt is not None:
                self.mqtt.publish_command(device_id, payload)
            commands.append(command)
            self.bus.publish({"type": "command", "data": command})
        shadow = self.store.set_desired(device_id, patch)
        self.bus.publish({"type": "shadow", "data": self._shadow_view(shadow)})
        return commands

    def poll_commands(self, device_id: str) -> list[dict[str, Any]]:
        commands = self.store.pending_commands(device_id)
        # 影子补发：命令可能已投递但设备拉取后掉线/未执行——desired 与
        # reported 不一致且超过宽限窗口的键自动重新入队，直到 reported 追上
        shadow = self.store.get_shadow(device_id)
        delta = {key: value for key, value in shadow["desired"].items()
                 if shadow["reported"].get(key) != value}
        for command in self.store.requeue_shadow_commands(device_id, delta):
            payload = {"property": command["payload"]["property"],
                       "value": command["payload"]["value"]}
            if self.mqtt is not None:
                self.mqtt.publish_command(device_id, payload)
            self.bus.publish({"type": "command", "data": command})
            commands.append(command)
        return commands

    # ------------------------------------------------------------------ 视图

    def view(self, device: dict[str, Any]) -> dict[str, Any]:
        """附加派生字段：是否在线按 last_seen 惰性计算。"""
        online = device["status"] == "online" and \
            time.time() - device["last_seen"] <= self.online_timeout
        out = dict(device)
        out["online"] = online
        out["effective_status"] = "online" if online else "offline"
        return out

    def overview(self) -> dict[str, Any]:
        devices = [self.view(d) for d in self.store.list_devices()]
        now = time.time()
        return {
            "devices": len(devices),
            "online": sum(1 for d in devices if d["online"]),
            "telemetry_points": self.store.count_telemetry(),
            "alerts_24h": self.store.count_alerts_since(now - 86400),
            "rules": len(self.store.list_rules()),
        }
