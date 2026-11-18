"""多渠道告警推送测试：渠道识别、报文构造、群机器人联动。"""
from __future__ import annotations

import pytest

from liniot import rules
from liniot.bus import EventBus
from liniot.rules import RuleEngine, build_payload, detect_channel, notify_channels
from liniot.store import Store

ALERT = {"id": 1, "device_id": "sensor-1",
         "message": "设备 sensor-1 的 temperature = 31 > 30", "ts": 1000.0}


# ------------------------------------------------------------------ 渠道识别

def test_detect_channel():
    assert detect_channel("https://oapi.dingtalk.com/robot/send?access_token=x") == "dingtalk"
    assert detect_channel("https://open.feishu.cn/open-apis/bot/v2/hook/x") == "feishu"
    assert detect_channel("https://open.larksuite.com/hook/x") == "feishu"
    assert detect_channel("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x") == "wecom"
    assert detect_channel("https://example.com/hook") == "generic"


def test_build_payload_shapes():
    ding = build_payload("dingtalk", ALERT)
    assert ding == {"msgtype": "text", "text": {"content": f"LinIoT 告警: {ALERT['message']}"}}

    feishu = build_payload("feishu", ALERT)
    assert feishu == {"msg_type": "text", "content": {"text": f"LinIoT 告警: {ALERT['message']}"}}

    assert build_payload("generic", ALERT) is ALERT  # 通用渠道原样透传


def test_notify_channels_posts_each_url(monkeypatch):
    posted = []

    def fake_post(url, json=None, timeout=None):
        posted.append((url, json))
        return object()

    monkeypatch.setattr(rules.httpx, "post", fake_post)
    notify_channels(["https://oapi.dingtalk.com/x", "https://open.feishu.cn/x",
                     "https://qyapi.weixin.qq.com/x", "https://example.com/hook"], ALERT)

    assert len(posted) == 4
    assert posted[0][1]["msgtype"] == "text"        # 钉钉
    assert posted[1][1]["msg_type"] == "text"       # 飞书
    assert posted[2][1]["msgtype"] == "text"        # 企业微信
    assert posted[3][1]["message"] == ALERT["message"]  # 通用透传


def test_notify_channels_swallows_errors(monkeypatch):
    def boom(url, json=None, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(rules.httpx, "post", boom)
    notify_channels(["https://oapi.dingtalk.com/x"], ALERT)  # 不应抛异常


# ------------------------------------------------------------------ 规则联动

def test_rule_fire_pushes_to_alert_webhooks(tmp_path, monkeypatch):
    posted = []

    def fake_post(url, json=None, timeout=None):
        posted.append((url, json))
        return object()

    monkeypatch.setattr(rules.httpx, "post", fake_post)
    store = Store(tmp_path / "n.db")
    engine = RuleEngine(store, EventBus(),
                        webhook_url="https://example.com/generic",
                        alert_webhooks=("https://oapi.dingtalk.com/x",))
    store.upsert_device("sensor-1", "传感器", "sensor", {})
    store.create_rule("sensor-1", "temperature", ">", 30)

    engine.evaluate("sensor-1", [{"metric": "temperature", "value": 31}])

    urls = [u for u, _ in posted]
    assert urls == ["https://example.com/generic", "https://oapi.dingtalk.com/x"]
    assert posted[0][1]["message"].startswith("设备 sensor-1")  # 通用透传
    assert posted[1][1]["text"]["content"].startswith("LinIoT 告警")  # 钉钉报文


# ------------------------------------------------------------------ 自定义模板

TEMPLATE = "[车间A] {device_name}({device_id}) {level}: {message} | {metric}={value}"
CONTEXT = {"metric": "temperature", "value": "31", "device_name": "传感器"}
ALERT_T = {**ALERT, "level": "warning"}
EXPECTED = ("[车间A] 传感器(sensor-1) warning: 设备 sensor-1 的 temperature = 31 > 30"
            " | temperature=31")


def test_default_text_unchanged_without_template():
    assert build_payload("dingtalk", ALERT) == build_payload("dingtalk", ALERT, "", CONTEXT)
    assert build_payload("feishu", ALERT, "")["content"]["text"] == \
        f"LinIoT 告警: {ALERT['message']}"


def test_template_applies_to_three_channels():
    ding = build_payload("dingtalk", ALERT_T, TEMPLATE, CONTEXT)
    wecom = build_payload("wecom", ALERT_T, TEMPLATE, CONTEXT)
    feishu = build_payload("feishu", ALERT_T, TEMPLATE, CONTEXT)
    assert ding == {"msgtype": "text", "text": {"content": EXPECTED}}
    assert wecom == {"msgtype": "text", "text": {"content": EXPECTED}}
    assert feishu == {"msg_type": "text", "content": {"text": EXPECTED}}
    assert build_payload("generic", ALERT_T, TEMPLATE, CONTEXT) is ALERT_T  # 通用透传不变


def test_template_unknown_placeholder_is_empty():
    out = build_payload("dingtalk", ALERT_T, "{device_id}|{nope}|{ts}", CONTEXT)
    assert out["text"]["content"] == "sensor-1||1000.0"


@pytest.mark.parametrize("bad", ["{", "oops }", "{0}", "{device_id!z}", "{message.nope}"])
def test_bad_template_falls_back_to_default(bad):
    out = build_payload("dingtalk", ALERT_T, bad, CONTEXT)
    assert out["text"]["content"] == f"LinIoT 告警: {ALERT['message']}"


def test_rule_fire_uses_template(tmp_path, monkeypatch):
    posted = []
    monkeypatch.setattr(rules.httpx, "post",
                        lambda url, json=None, timeout=None: posted.append((url, json)))
    store = Store(tmp_path / "t.db")
    engine = RuleEngine(store, EventBus(), alert_webhooks=("https://oapi.dingtalk.com/x",),
                        alert_template="[车间A] {device_name} {metric}={value} ({level})")
    store.upsert_device("sensor-1", "传感器", "sensor", {})
    store.create_rule("sensor-1", "temperature", ">", 30)

    engine.evaluate("sensor-1", [{"metric": "temperature", "value": 31}])

    assert posted[0][1]["text"]["content"] == "[车间A] 传感器 temperature=31 (warning)"
