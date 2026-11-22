"""设备上下线事件推送测试：离线巡检翻转、恢复上线通知、开关关闭（Issue #11）。"""
from __future__ import annotations

import time

from liniot import rules
from liniot.bus import EventBus
from liniot.config import load_config
from liniot.device_events import DeviceEventMonitor
from liniot.rules import RuleEngine
from liniot.service import DeviceService
from liniot.store import Store

WEBHOOK = "https://example.com/hook"
DINGTALK = "https://oapi.dingtalk.com/x"


def make_core(tmp_path):
    """构建 store + services + monitor 的最小组合；线程不启动，直接调 check_once。"""
    store = Store(tmp_path / "dev.db")
    bus = EventBus()
    devices = DeviceService(store, bus, RuleEngine(store, bus),
                            webhook_url=WEBHOOK, alert_webhooks=(DINGTALK,))
    monitor = DeviceEventMonitor(store, devices, webhook_url=WEBHOOK,
                                 alert_webhooks=(DINGTALK,))
    devices.event_monitor = monitor
    return store, devices, monitor


def age_last_seen(store, device_id: str, seconds: float) -> None:
    """把 last_seen 拨回过去，模拟设备超时未上报（Issue #11 建议的测试手法）。"""
    with store.lock:
        store.conn.execute("UPDATE devices SET last_seen=? WHERE id=?",
                           (time.time() - seconds, device_id))
        store.conn.commit()


def capture_posts(monkeypatch):
    """拦截 httpx.post，返回记录列表。"""
    posted = []

    def fake_post(url, json=None, timeout=None):
        posted.append((url, json))
        return object()

    monkeypatch.setattr(rules.httpx, "post", fake_post)
    return posted


# ------------------------------------------------------------------ 离线翻转

def test_offline_flip_pushes_notification(tmp_path, monkeypatch):
    store, devices, monitor = make_core(tmp_path)
    store.upsert_device("light-1", "智能灯-1", "light", {})
    store.touch("light-1")                     # 设备在线
    age_last_seen(store, "light-1", 200)       # 超过 90 秒未上报
    posted = capture_posts(monkeypatch)

    events = monitor.check_once()

    assert len(events) == 1
    assert events[0]["event"] == "offline"
    assert events[0]["device_id"] == "light-1"
    assert events[0]["device_name"] == "智能灯-1"
    assert store.get_device("light-1")["status"] == "offline"   # 落库状态已翻转
    # 通用回调透传事件 JSON；钉钉渠道推送文案
    assert posted[0][0] == WEBHOOK
    assert posted[0][1] == {"event": "offline", "device_id": "light-1",
                            "device_name": "智能灯-1", "ts": events[0]["ts"]}
    assert posted[1][0] == DINGTALK
    assert posted[1][1]["text"]["content"] == "设备 智能灯-1 (light-1) 已离线"
    # 二次巡检：状态已一致，不重复推送
    assert monitor.check_once() == []
    assert len(posted) == 2


# ------------------------------------------------------------------ 恢复上线

def test_online_notify_on_resume(tmp_path, monkeypatch):
    store, devices, monitor = make_core(tmp_path)
    store.upsert_device("light-1", "智能灯-1", "light", {})     # 注册即 offline
    posted = capture_posts(monkeypatch)

    devices.ingest("light-1", [{"metric": "temperature", "value": 1}])

    assert store.get_device("light-1")["status"] == "online"    # touch 已置 online
    online_posts = [p for p in posted if p[1].get("event") == "online"]
    assert len(online_posts) == 1
    assert online_posts[0][1]["device_id"] == "light-1"
    assert online_posts[0][1]["device_name"] == "智能灯-1"
    assert [p for p in posted if p[0] == DINGTALK][0][1]["text"]["content"] \
        == "设备 智能灯-1 (light-1) 已上线"
    # 再次上报：已是 online，不再重复推送
    posted.clear()
    devices.ingest("light-1", [{"metric": "temperature", "value": 2}])
    assert [p for p in posted if p[1].get("event") == "online"] == []


# ------------------------------------------------------------------ 开关关闭

def test_disabled_keeps_current_behavior(tmp_path, monkeypatch):
    store, devices, monitor = make_core(tmp_path)
    monitor.enabled = False
    store.upsert_device("light-1", "智能灯-1", "light", {})
    store.touch("light-1")
    age_last_seen(store, "light-1", 200)
    posted = capture_posts(monkeypatch)

    assert monitor.check_once() == []          # 不翻转、不推送
    assert store.get_device("light-1")["status"] == "online"    # 行为与旧版本一致

    devices.ingest("light-1", [{"metric": "temperature", "value": 1}])
    assert posted == []                        # 恢复上报同样不推送


# ------------------------------------------------------------------ 配置解析

def test_device_events_env_parse(monkeypatch):
    monkeypatch.setenv("LINIOT_DEVICE_EVENTS", "off")
    assert load_config().device_events is False
    monkeypatch.setenv("LINIOT_DEVICE_EVENTS", "false")
    assert load_config().device_events is False
    monkeypatch.delenv("LINIOT_DEVICE_EVENTS")
    assert load_config().device_events is True  # 默认开启
