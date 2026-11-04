"""AI 助手测试：本地回退意图识别、JSON 解析、LLM 路径（mock）。"""
from __future__ import annotations

import pytest

from liniot.ai import extract_json, local_intent
from liniot.bus import EventBus
from liniot.config import AIConfig, Config
from liniot.rules import RuleEngine
from liniot.service import DeviceService
from liniot.store import Store

SNAPSHOT = [
    {"id": "sensor-1", "name": "客厅传感器", "type": "sensor", "online": True,
     "properties": {}, "metrics": {"temperature": {"value": 25.3, "ts": 1.0},
                                   "humidity": {"value": 60.0, "ts": 1.0}}},
    {"id": "light-1", "name": "智能灯", "type": "light", "online": True,
     "properties": {"power": False}, "metrics": {}},
]


@pytest.fixture()
def service(tmp_path):
    store = Store(tmp_path / "ai.db")
    svc = DeviceService(store, EventBus(), RuleEngine(store, EventBus()))
    svc.register("客厅传感器", "sensor", device_id="sensor-1")
    svc.register("智能灯", "light", device_id="light-1")
    svc.set_status("light-1", "online", {"power": False})
    return svc


def test_extract_json_tolerant():
    assert extract_json('废话 {"reply":"好","actions":[]} 结尾') == {"reply": "好", "actions": []}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json("完全没有 JSON") is None
    assert extract_json('前缀 {"a":{"b":1}} 尾部 {"c":2}') == {"a": {"b": 1}}


def test_local_intent_query_temperature():
    reply, actions = local_intent("客厅传感器温度是多少", SNAPSHOT)
    assert "25.3" in reply
    assert actions == []


def test_local_intent_turn_on_light(service):
    reply, actions = local_intent("打开智能灯", SNAPSHOT)
    assert actions == [{"tool": "set_property", "device": "light-1",
                        "property": "power", "value": True}]
    assert "智能灯" in reply


def test_local_intent_overview():
    reply, _ = local_intent("设备概览", SNAPSHOT)
    assert "客厅传感器" in reply and "智能灯" in reply


@pytest.mark.anyio()
async def test_chat_llm_mode_executes_validated_action(service, monkeypatch):
    from liniot import ai

    async def fake_llm(config, messages):
        return ('{"reply":"好的，已为您打开智能灯。",'
                '"actions":[{"tool":"set_property","device":"light-1",'
                '"property":"power","value":true}]}')

    monkeypatch.setattr(ai, "call_llm", fake_llm)
    result = await ai.chat(service.store, service, AIConfig(api_key="test-key"),
                           "把灯打开")
    assert result["mode"] == "llm"
    assert result["actions"] == [{"tool": "set_property", "device": "light-1",
                                  "property": "power", "value": True}]
    # 命令真实落库，可被设备拉取
    pending = service.poll_commands("light-1")
    assert pending[0]["payload"] == {"property": "power", "value": True}


@pytest.mark.anyio()
async def test_chat_llm_rejects_unknown_property(service, monkeypatch):
    from liniot import ai

    async def fake_llm(config, messages):
        return ('{"reply":"尝试执行。","actions":[{"tool":"set_property",'
                '"device":"light-1","property":"volume","value":99}]}')

    monkeypatch.setattr(ai, "call_llm", fake_llm)
    result = await ai.chat(service.store, service, AIConfig(api_key="k"), "调大音量")
    assert result["actions"] == []
    assert any(not r["ok"] for r in result["results"])


@pytest.mark.anyio()
async def test_chat_without_key_uses_local_mode(service):
    from liniot import ai

    result = await ai.chat(service.store, service, AIConfig(), "关闭智能灯")
    assert result["mode"] == "local"
    assert service.poll_commands("light-1")[0]["payload"]["value"] is False


# ---------------------------------------------------------------- 告警诊断

@pytest.mark.anyio()
async def test_diagnose_alert_with_llm(service, monkeypatch):
    from liniot import ai

    async def fake_llm(config, messages):
        assert "设备" in messages[0]["content"]
        return "传感器可能受热源干扰导致读数升高，建议检查安装位置并重新校准。"

    monkeypatch.setattr(ai, "call_llm", fake_llm)
    alert = {"id": 1, "device_id": "sensor-1", "message": "设备 sensor-1 的 temperature = 45 > 30"}
    text = await ai.diagnose_alert(service.store, service, AIConfig(api_key="k"), alert)
    assert "校准" in text


@pytest.mark.anyio()
async def test_diagnose_alert_without_key_returns_none(service):
    from liniot import ai

    alert = {"id": 1, "device_id": "sensor-1", "message": "x"}
    assert await ai.diagnose_alert(service.store, service, AIConfig(), alert) is None


@pytest.mark.anyio()
async def test_diagnose_alert_llm_failure_is_silent(service, monkeypatch):
    from liniot import ai

    async def broken_llm(config, messages):
        raise RuntimeError("network down")

    monkeypatch.setattr(ai, "call_llm", broken_llm)
    alert = {"id": 1, "device_id": "sensor-1", "message": "x"}
    assert await ai.diagnose_alert(service.store, service, AIConfig(api_key="k"), alert) is None


def test_diagnosis_rate_limiter_per_device():
    from liniot.ai import DiagnosisRateLimiter

    limiter = DiagnosisRateLimiter(cooldown=300)
    assert limiter.allow("sensor-1", now=1000) is True
    assert limiter.allow("sensor-1", now=1299) is False   # 冷却期内
    assert limiter.allow("sensor-2", now=1299) is True    # 设备之间相互独立
    assert limiter.allow("sensor-1", now=1300) is True    # 冷却到期恢复


@pytest.mark.anyio()
async def test_diagnosis_worker_skips_within_cooldown(service, monkeypatch):
    import asyncio

    from liniot import ai, main as liniot_main

    calls = []

    async def fake_llm(config, messages):
        calls.append(1)
        return "诊断建议。"

    monkeypatch.setattr(ai, "call_llm", fake_llm)
    bus = EventBus()
    id1 = service.store.insert_alert(None, "sensor-1", "告警一")
    id2 = service.store.insert_alert(None, "sensor-1", "告警二（冷却期内）")
    id3 = service.store.insert_alert(None, "light-1", "另一设备告警")

    config = Config()
    config.ai = AIConfig(api_key="k")
    bus.attach_loop(asyncio.get_running_loop())
    worker = asyncio.create_task(
        liniot_main.diagnosis_worker(service.store, service, config, bus))
    await asyncio.sleep(0.05)
    for alert_id in (id1, id2, id3):
        bus.publish({"type": "alert", "data": {"id": alert_id,
                                               "device_id": "sensor-1" if alert_id == id1 or alert_id == id2 else "light-1",
                                               "message": "测试告警", "diagnosis": None}})
    await asyncio.sleep(0.25)
    worker.cancel()

    assert len(calls) == 2  # sensor-1 第二条被冷却跳过，light-1 正常诊断
    rows = {a["id"]: a for a in service.store.list_alerts()}
    assert rows[id1]["diagnosis"] == "诊断建议。"
    assert rows[id2]["diagnosis"] is None
    assert rows[id3]["diagnosis"] == "诊断建议。"


@pytest.mark.anyio()
async def test_diagnosis_worker_persists_and_broadcasts(service, monkeypatch):
    import asyncio

    from liniot import ai, main as liniot_main

    async def fake_llm(config, messages):
        return "建议检查传感器安装位置。"

    monkeypatch.setattr(ai, "call_llm", fake_llm)
    bus = EventBus()
    alert_id = service.store.insert_alert(None, "sensor-1", "测试告警")

    config = Config()
    config.ai = AIConfig(api_key="k")
    bus.attach_loop(asyncio.get_running_loop())
    worker = asyncio.create_task(
        liniot_main.diagnosis_worker(service.store, service, config, bus))
    await asyncio.sleep(0.05)
    bus.publish({"type": "alert", "data": {"id": alert_id, "device_id": "sensor-1",
                                           "message": "测试告警", "diagnosis": None}})
    await asyncio.sleep(0.2)
    worker.cancel()
    row = [a for a in service.store.list_alerts() if a["id"] == alert_id][0]
    assert row["diagnosis"] == "建议检查传感器安装位置。"
