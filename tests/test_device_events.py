"""设备上线/离线事件推送测试：两个翻转方向、首次基线、开关。"""
from __future__ import annotations

import time

from liniot import rules
from liniot.bus import EventBus
from liniot.config import load_config
from liniot.device_events import DeviceEventMonitor
from liniot.rules import RuleEngine
from liniot.service import DeviceService
from liniot.store import Store


def _setup(tmp_path, monkeypatch, enabled=True):
    posted = []
    monkeypatch.setattr(rules.httpx, "post",
                        lambda url, json=None, timeout=None: posted.append((url, json)))
    store = Store(tmp_path / "e.db")
    bus = EventBus()
    svc = DeviceService(store, bus, RuleEngine(store, bus), None, 90.0)
    monitor = DeviceEventMonitor(store, svc, "https://example.com/generic",
                                 ("https://oapi.dingtalk.com/x",), enabled=enabled)
    store.upsert_device("light-1", "智能灯-1", "light", {})
    store.touch("light-1")
    return store, svc, monitor, posted


def _age(store, device_id, seconds):
    store.conn.execute("UPDATE devices SET last_seen=? WHERE id=?",
                       (time.time() - seconds, device_id))
    store.conn.commit()


def test_offline_then_online_events(tmp_path, monkeypatch):
    store, svc, monitor, posted = _setup(tmp_path, monkeypatch)
    assert monitor.check_once() == []          # 首次只建立基线
    assert posted == []

    _age(store, "light-1", 200)                # 超时未上报
    events = monitor.check_once()
    assert [e["event"] for e in events] == ["offline"]
    assert store.get_device("light-1")["status"] == "offline"  # 落库补写
    assert posted[0][0] == "https://example.com/generic"
    assert posted[0][1]["event"] == "offline" and posted[0][1]["device_id"] == "light-1"
    assert posted[0][1]["device_name"] == "智能灯-1"
    assert posted[1][1]["text"]["content"].endswith("设备 智能灯-1 (light-1) 已离线")

    assert monitor.check_once() == []          # 状态未变不重复通知

    svc.ingest("light-1", [{"metric": "x", "value": 1}])  # 恢复上报
    events = monitor.check_once()
    assert [e["event"] for e in events] == ["online"]
    assert posted[-1][1]["text"]["content"].endswith("设备 智能灯-1 (light-1) 已上线")


def test_disabled_monitor_does_not_start(tmp_path, monkeypatch):
    _, _, monitor, posted = _setup(tmp_path, monkeypatch, enabled=False)
    monitor.start()
    assert monitor._thread is None and posted == []


def test_config_switch(monkeypatch):
    monkeypatch.delenv("LINIOT_DEVICE_EVENTS", raising=False)
    assert load_config().device_events is True
    for value in ("off", "0", "false"):
        monkeypatch.setenv("LINIOT_DEVICE_EVENTS", value)
        assert load_config().device_events is False
