"""规则引擎：对遥测流做阈值判断，上升沿触发一次告警（持续超标不重复触发）。"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

import httpx

logger = logging.getLogger("liniot.rules")

OPS: dict[str, Callable[[float, float], bool]] = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
}


def notify_webhook(url: str, payload: dict[str, Any]) -> None:
    """告警 Webhook 回调；网络异常只记日志，不影响主流程。"""
    if not url:
        return
    try:
        httpx.post(url, json=payload, timeout=3)
    except Exception as exc:
        logger.warning("告警 Webhook 调用失败: %s", exc)


CHANNEL_HOSTS = (
    ("oapi.dingtalk.com", "dingtalk"),
    ("open.feishu.cn", "feishu"),
    ("open.larksuite.com", "feishu"),
    ("qyapi.weixin.qq.com", "wecom"),
)


def detect_channel(url: str) -> str:
    """按 URL 主机名识别群机器人渠道；未知地址走通用 JSON 透传。"""
    for host, channel in CHANNEL_HOSTS:
        if host in url:
            return channel
    return "generic"


def alert_text(alert: dict[str, Any]) -> str:
    return f"LinIoT 告警: {alert.get('message', '')}"


def build_payload(channel: str, alert: dict[str, Any]) -> dict[str, Any]:
    """按渠道构造群机器人报文；generic 原样透传告警 JSON。"""
    if channel == "dingtalk":
        return {"msgtype": "text", "text": {"content": alert_text(alert)}}
    if channel == "wecom":
        return {"msgtype": "text", "text": {"content": alert_text(alert)}}
    if channel == "feishu":
        return {"msg_type": "text", "content": {"text": alert_text(alert)}}
    return alert


def notify_channels(urls, alert: dict[str, Any]) -> None:
    """把告警推送到群机器人列表（钉钉/飞书/企业微信自动识别）。"""
    for url in urls:
        try:
            httpx.post(url, json=build_payload(detect_channel(url), alert), timeout=3)
        except Exception as exc:
            logger.warning("告警通知推送失败 %s: %s", url, exc)


class RuleEngine:
    def __init__(self, store, bus, webhook_url: str = "",
                 alert_webhooks: tuple = ()) -> None:
        self.store = store
        self.bus = bus
        self.webhook_url = webhook_url
        self.alert_webhooks = tuple(alert_webhooks)
        # 触发态按 (规则, 设备) 记录：全局规则对每台设备独立做上升沿判定
        self._active: dict[tuple[int, str], bool] = {}

    def evaluate(self, device_id: str, points: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """处理一批遥测点，返回本次新产生的告警列表。"""
        fired: list[dict[str, Any]] = []
        rules = self.store.list_rules(only_enabled=True)
        if not rules:
            return fired
        for rule in rules:
            if rule["device_id"] and rule["device_id"] != device_id:
                continue
            judge = OPS.get(rule["op"])
            if judge is None:
                continue
            matched = [p for p in points if p["metric"] == rule["metric"]]
            if not matched:
                continue
            point = matched[-1]
            triggered = judge(point["value"], rule["threshold"])
            key = (rule["id"], device_id)
            was_active = self._active.get(key, False)
            if triggered and not was_active:
                self._active[key] = True
                fired.append(self._fire(rule, device_id, point["value"]))
            elif not triggered and was_active:
                self._active[key] = False
        return fired

    def _fire(self, rule: dict[str, Any], device_id: str, value: float) -> dict[str, Any]:
        message = (f"设备 {device_id} 的 {rule['metric']} = {value:g} "
                   f"{rule['op']} {rule['threshold']:g}"
                   + (f"（{rule['note']}）" if rule["note"] else ""))
        alert_id = self.store.insert_alert(rule["id"], device_id, message)
        alert = {"id": alert_id, "rule_id": rule["id"], "device_id": device_id,
                 "message": message, "level": "warning", "ts": time.time(),
                 "diagnosis": None}
        self.bus.publish({"type": "alert", "data": alert})
        notify_webhook(self.webhook_url, alert)
        notify_channels(self.alert_webhooks, alert)
        logger.warning("规则触发: %s", message)
        return alert

    def _notify_webhook(self, alert: dict[str, Any]) -> None:  # 兼容保留
        notify_webhook(self.webhook_url, alert)
