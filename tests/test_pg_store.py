"""PostgreSQL 后端与 SQLite Store 的行为对齐测试。

设置 LINIOT_TEST_PG_DSN（如 postgresql://liniot:liniot@127.0.0.1:5432/liniot_test）
后运行；未设置时整文件跳过——CI 在带 postgres service 容器的作业里注入该变量。
"""
from __future__ import annotations

import os
import time

import pytest

from liniot.pg_store import PostgresStore
from liniot.store import hash_token

DSN = os.environ.get("LINIOT_TEST_PG_DSN", "")

pytestmark = pytest.mark.skipif(not DSN, reason="需要 LINIOT_TEST_PG_DSN（PostgreSQL 测试库）")

TABLES = ("devices", "telemetry", "rules", "alerts", "scenes",
          "commands", "api_tokens", "shadows")


@pytest.fixture()
def pgstore():
    store = PostgresStore(DSN)
    with store.lock:
        store.conn.execute(f"TRUNCATE {', '.join(TABLES)}")
    try:
        yield store
    finally:
        store.conn.close()  # 及时释放连接，失败用例也不阻塞后续 TRUNCATE


def test_device_upsert_and_get(pgstore):
    device, created = pgstore.upsert_device("sensor-1", "温湿度传感器", "sensor", {"room": "客厅"})
    assert created is True
    assert device["meta"] == {"room": "客厅"}
    assert device["status"] == "offline"

    _, created_again = pgstore.upsert_device("sensor-1", "改名传感器", "sensor", {})
    assert created_again is False
    assert pgstore.get_device("sensor-1")["name"] == "改名传感器"
    assert pgstore.get_device("missing") is None
    assert [d["id"] for d in pgstore.list_devices()] == ["sensor-1"]


def test_status_merge_properties(pgstore):
    pgstore.upsert_device("light-1", "智能灯", "light", {})
    pgstore.set_status("light-1", "online", {"power": False})
    pgstore.set_status("light-1", "online", {"brightness": 80})  # 增量合并而非覆盖
    device = pgstore.get_device("light-1")
    assert device["properties"] == {"power": False, "brightness": 80}
    assert device["status"] == "online"
    assert device["last_seen"] > 0
    pgstore.touch("light-1")
    assert pgstore.get_device("light-1")["last_seen"] > 0


def test_telemetry_query_aggregate_and_prune(pgstore):
    base = 1000.0  # 固定基准：1000..1009 恰好落在 FLOOR(ts/5) 的两个桶里，断言可复现
    pgstore.insert_telemetry(
        "sensor-1", [{"metric": "temperature", "value": 20 + i * 0.5, "ts": base + i}
                     for i in range(10)])
    series = pgstore.query_telemetry("sensor-1", "temperature")
    assert len(series) == 10
    assert [p["value"] for p in series] == [20 + i * 0.5 for i in range(10)]  # 升序

    buckets = pgstore.aggregate_telemetry("sensor-1", window=5)
    assert len(buckets) == 2
    assert buckets[0]["count"] == 5
    assert buckets[0]["min"] == 20.0 and buckets[1]["max"] == 24.5
    assert isinstance(buckets[0]["ts"], int)  # 与 SQLite CAST 行为对齐

    assert pgstore.metrics_of("sensor-1") == ["temperature"]
    latest = pgstore.latest_metrics()
    assert latest[("sensor-1", "temperature")]["value"] == 24.5
    assert pgstore.count_telemetry() == 10

    assert len(list(pgstore.iter_telemetry("sensor-1", limit=3))) == 3

    pgstore.prune(5)
    assert pgstore.count_telemetry() == 5
    assert len(pgstore.query_telemetry("sensor-1")) == 5  # 保留的是最近 5 条


def test_rules_alerts_scenes(pgstore):
    rule = pgstore.create_rule("sensor-1", "temperature", ">", 30, note="高温")
    assert rule["enabled"] == 1 and rule["note"] == "高温"
    pgstore.update_rule(rule["id"], threshold=28)
    assert pgstore.get_rule(rule["id"])["threshold"] == 28
    assert len(pgstore.list_rules(only_enabled=True)) == 1

    alert_id = pgstore.insert_alert(rule["id"], "sensor-1", "温度超限", level="warning")
    pgstore.set_alert_diagnosis(alert_id, "建议检查散热")
    assert pgstore.acknowledge_alert(alert_id)["acknowledged"] == 1
    assert pgstore.list_alerts(ack="open") == []
    assert len(pgstore.list_alerts(ack="acked")) == 1
    assert pgstore.count_alerts_since(time.time() - 3600) == 1
    assert pgstore.acknowledge_alert(999) is None

    scene = pgstore.create_scene("夜间模式", "sensor-1", "temperature", ">", 28,
                                 "light-1", "power", "true", 60)
    assert scene["enabled"] == 1 and scene["cooldown"] == 60
    pgstore.update_scene(scene["id"], threshold=26)
    assert pgstore.get_scene(scene["id"])["threshold"] == 26
    pgstore.touch_scene(scene["id"], time.time())
    assert pgstore.get_scene(scene["id"])["last_fired"] > 0
    pgstore.delete_scene(scene["id"])
    assert pgstore.list_scenes() == []


def test_commands_queue_and_deliver(pgstore):
    pgstore.upsert_device("light-1", "智能灯", "light", {})
    command = pgstore.queue_command("light-1", {"property": "power", "value": True})
    assert command["payload"]["value"] is True

    pending = pgstore.pending_commands("light-1")
    assert len(pending) == 1
    assert pending[0]["payload"] == {"property": "power", "value": True}
    assert pgstore.pending_commands("light-1") == []  # 取走后不再返回


def test_api_tokens_lifecycle(pgstore):
    token = pgstore.create_token("老王", "viewer", hash_token("liniot_secret"))
    assert token["name"] == "老王" and token["role"] == "viewer"
    assert pgstore.count_tokens() == 1

    found = pgstore.find_token(hash_token("liniot_secret"))
    assert found is not None and found["id"] == token["id"]
    assert pgstore.find_token(hash_token("wrong-token")) is None

    # 角色编辑与防自锁计数
    updated = pgstore.update_token(token["id"], "device")
    assert updated["role"] == "device"
    admin = pgstore.create_token("管理员", "admin", hash_token("liniot_admin"))
    assert pgstore.count_active_admin_tokens() == 1
    assert pgstore.count_active_admin_tokens(exclude_id=admin["id"]) == 0

    first = pgstore.list_tokens()[0]["last_used_at"]
    time.sleep(0.01)
    pgstore.find_token(hash_token("liniot_secret"))  # 节流窗口内不刷新
    assert pgstore.list_tokens()[0]["last_used_at"] == first

    revoked = pgstore.revoke_token(token["id"])
    assert revoked["revoked_at"] is not None
    assert pgstore.find_token(hash_token("liniot_secret")) is None
    assert pgstore.revoke_token(token["id"])["revoked_at"] == revoked["revoked_at"]
    assert len(pgstore.list_tokens()) == 2  # 已吊销记录仍在列表中
    assert pgstore.revoke_token(999) is None


def test_device_shadow_lifecycle(pgstore):
    shadow = pgstore.get_shadow("light-1")
    assert shadow["desired"] == {} and shadow["reported"] == {}
    assert shadow["version"] == 0 and shadow["desired_ts"] is None

    pgstore.set_desired("light-1", {"power": True, "brightness": 80})
    shadow = pgstore.get_shadow("light-1")
    assert shadow["desired"] == {"power": True, "brightness": 80}
    assert shadow["version"] == 1 and shadow["desired_ts"] is not None

    pgstore.set_desired("light-1", {"power": False})
    pgstore.set_reported("light-1", {"power": True})
    shadow = pgstore.get_shadow("light-1")
    assert shadow["desired"] == {"power": False, "brightness": 80}
    assert shadow["reported"] == {"power": True}
    assert shadow["version"] == 3

    assert pgstore.set_desired("light-1", {})["version"] == 3  # 空 patch 不升版本

    pgstore.upsert_device("light-1", "智能灯", "light", {})
    pgstore.delete_device("light-1")
    assert pgstore.get_shadow("light-1")["version"] == 0  # 级联清理


def test_shadow_command_requeue(pgstore):
    requeued = pgstore.requeue_shadow_commands("light-1", {"power": True}, grace=0)
    assert [c["payload"]["property"] for c in requeued] == ["power"]
    assert requeued[0]["payload"]["reason"] == "shadow_retry"

    assert pgstore.requeue_shadow_commands("light-1", {"power": True}, grace=0) == []

    pgstore.pending_commands("light-1")
    assert pgstore.requeue_shadow_commands("light-1", {"power": True}, grace=10) == []
    assert len(pgstore.requeue_shadow_commands("light-1", {"power": True}, grace=0)) == 1
    assert pgstore.requeue_shadow_commands("light-1", {}, grace=0) == []


def test_timescaledb_detection(pgstore):
    """探测行为与库内实际状态一致：有扩展则 hypertable，无则普通表。"""
    ext = pgstore.conn.execute(
        "SELECT 1 FROM pg_extension WHERE extname='timescaledb'").fetchone()
    assert pgstore.timescaledb == bool(ext)
    if ext:
        hypertables = pgstore.conn.execute(
            "SELECT hypertable_name FROM timescaledb_information.hypertables"
            " WHERE hypertable_name='telemetry'").fetchall()
        assert len(hypertables) == 1
        # hypertable 上常规读写与聚合全部可用
        pgstore.insert_telemetry("light-1", [{"metric": "t", "value": 1.0, "ts": 1.0}])
        assert pgstore.count_telemetry() == 1
        assert pgstore.aggregate_telemetry("light-1", window=60)


def test_pg_backend_app():
    """LINIOT_DB_URL 指向 PG 时 create_app 全链路可用。"""
    from pathlib import Path

    from fastapi.testclient import TestClient
    from liniot.config import Config
    from liniot.main import create_app

    config = Config(db_url=DSN, web_dir=Path("/nonexistent"))
    app = create_app(config)
    with TestClient(app) as client:
        with app.state.liniot["store"].lock:
            app.state.liniot["store"].conn.execute(f"TRUNCATE {', '.join(TABLES)}")

        assert client.post("/api/devices", json={"id": "light-1", "name": "智能灯",
                                                 "type": "light"}).status_code == 201
        assert client.post("/api/devices/light-1/telemetry",
                           json={"metric": "brightness", "value": 60}).json()["accepted"] == 1
        client.post("/api/devices/light-1/properties", json={"properties": {"power": True}})
        shadow = client.get("/api/devices/light-1/shadow").json()
        assert shadow["delta"] == {"power": True}

        commands = client.get("/api/devices/light-1/commands").json()
        assert commands[0]["payload"] == {"property": "power", "value": True}
        client.post("/api/devices/light-1/status",
                    json={"status": "online", "properties": {"power": True}})
        assert client.get("/api/devices/light-1/shadow").json()["delta"] == {}

        token = client.post("/api/tokens", json={"name": "管理员", "role": "admin"}).json()
        assert token["token"].startswith("liniot_")
        h = {"X-Auth-Token": token["token"]}
        assert client.get("/api/tokens", headers=h).json()[0]["name"] == "管理员"

        # /metrics 在 PG 后端同样可用；最后一个 admin 不可降级（防自锁）
        assert "liniot_devices_total 1" in client.get("/metrics").text
        assert client.patch(f"/api/tokens/{token['id']}", json={"role": "viewer"},
                            headers=h).status_code == 409
