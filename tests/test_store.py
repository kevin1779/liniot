"""存储层测试。"""
from __future__ import annotations

from liniot.store import Store


def test_device_upsert_and_get(tmp_path):
    store = Store(tmp_path / "a.db")
    device, created = store.upsert_device("sensor-1", "温湿度传感器", "sensor", {"room": "客厅"})
    assert created is True
    assert device["id"] == "sensor-1"
    assert device["meta"] == {"room": "客厅"}
    assert device["status"] == "offline"

    _, created_again = store.upsert_device("sensor-1", "改名传感器", "sensor", {})
    assert created_again is False
    assert store.get_device("sensor-1")["name"] == "改名传感器"
    assert store.get_device("missing") is None


def test_status_merge_properties(tmp_path):
    store = Store(tmp_path / "a.db")
    store.upsert_device("light-1", "智能灯", "light", {})
    store.set_status("light-1", "online", {"power": False})
    store.set_status("light-1", "online", {"brightness": 80})  # 增量合并而非覆盖
    device = store.get_device("light-1")
    assert device["properties"] == {"power": False, "brightness": 80}
    assert device["status"] == "online"
    assert device["last_seen"] > 0


def test_telemetry_query_and_retention(tmp_path):
    store = Store(tmp_path / "a.db")
    store.upsert_device("sensor-1", "传感器", "sensor", {})
    points = [{"metric": "temperature", "value": 20 + i, "ts": 1000.0 + i}
              for i in range(30)]
    store.insert_telemetry("sensor-1", points)

    series = store.query_telemetry("sensor-1", "temperature", limit=200)
    assert len(series) == 30
    assert series[0]["value"] == 20  # 时间升序
    assert series[-1]["value"] == 49

    latest = store.latest_metrics()
    assert latest[("sensor-1", "temperature")]["value"] == 49

    store.prune(10)
    assert store.count_telemetry() == 10
    assert store.query_telemetry("sensor-1")[-1]["value"] == 49  # 保留最新


def test_iter_telemetry_streaming(tmp_path):
    """流式迭代：分批遍历、指标过滤与"最近 N 条"截取。"""
    store = Store(tmp_path / "a.db")
    store.upsert_device("sensor-1", "传感器", "sensor", {})
    points = ([{"metric": "temperature", "value": 20 + i, "ts": 1000.0 + i}
               for i in range(30)]
              + [{"metric": "humidity", "value": 60 + i, "ts": 1000.0 + i}
                 for i in range(5)])
    store.insert_telemetry("sensor-1", points)

    # 小批量迭代也能取全量，且按 id 升序
    rows = list(store.iter_telemetry("sensor-1", batch=7))
    assert len(rows) == 35
    assert [r["value"] for r in rows[:3]] == [20, 21, 22]
    assert rows[-1]["value"] == 64

    # 指标过滤
    humidity = list(store.iter_telemetry("sensor-1", metric="humidity"))
    assert len(humidity) == 5 and humidity[0]["value"] == 60

    # limit：最近 N 条（升序产出）
    latest = list(store.iter_telemetry("sensor-1", metric="temperature", limit=3, batch=2))
    assert [r["value"] for r in latest] == [47, 48, 49]

    # limit 超过总数时退化为全量
    assert len(list(store.iter_telemetry("sensor-1", metric="humidity", limit=100))) == 5


def test_aggregate_telemetry(tmp_path):
    """按时间桶聚合：分桶对齐、多指标独立、窗口变化。"""
    store = Store(tmp_path / "a.db")
    store.upsert_device("sensor-1", "传感器", "sensor", {})
    # ts: 0..59（桶 0）+ 60..119（桶 60），window=60
    points = ([{"metric": "temperature", "value": 20 + i, "ts": float(i)}
               for i in range(60)]
              + [{"metric": "temperature", "value": 100.0, "ts": 60.0}]
              + [{"metric": "humidity", "value": 55.0, "ts": 10.0}])
    store.insert_telemetry("sensor-1", points)

    rows = store.aggregate_telemetry("sensor-1", "temperature", window=60)
    assert [r["ts"] for r in rows] == [0, 60]
    assert rows[0]["count"] == 60
    assert rows[0]["avg"] == 49.5 and rows[0]["min"] == 20 and rows[0]["max"] == 79
    assert rows[1]["count"] == 1 and rows[1]["avg"] == 100.0

    # 窗口放大：全部落进同一个桶
    wide = store.aggregate_telemetry("sensor-1", "temperature", window=3600)
    assert len(wide) == 1 and wide[0]["count"] == 61

    # 多指标各自独立分桶
    both = store.aggregate_telemetry("sensor-1", window=60)
    assert {r["metric"] for r in both} == {"temperature", "humidity"}
    assert [r["count"] for r in both if r["metric"] == "humidity"] == [1]


def test_acknowledge_alert(tmp_path):
    store = Store(tmp_path / "ack.db")
    store.upsert_device("s1", "传感器", "sensor", {})
    store.insert_alert(None, "s1", "测试告警")
    alert_id = store.list_alerts()[0]["id"]

    assert store.acknowledge_alert(999) is None  # 不存在

    acked = store.acknowledge_alert(alert_id)
    assert acked["acknowledged"] == 1 and acked["acknowledged_at"] > 0
    assert store.list_alerts(ack="open") == []
    assert len(store.list_alerts(ack="acked")) == 1
    assert store.list_alerts()[0]["acknowledged"] == 1


def test_commands_queue_and_deliver(tmp_path):
    store = Store(tmp_path / "a.db")
    store.upsert_device("light-1", "智能灯", "light", {})
    command = store.queue_command("light-1", {"property": "power", "value": True})
    assert command["payload"]["value"] is True

    pending = store.pending_commands("light-1")
    assert len(pending) == 1
    assert pending[0]["payload"] == {"property": "power", "value": True}
    assert store.pending_commands("light-1") == []  # 取走后不再返回


def test_api_tokens_lifecycle(tmp_path):
    """动态令牌：哈希落库、吊销即失效、last_used 节流刷新。"""
    import time as _time

    from liniot.store import hash_token

    store = Store(tmp_path / "a.db")
    token = store.create_token("老王", "viewer", hash_token("liniot_secret"))
    assert token["name"] == "老王" and token["role"] == "viewer"
    assert token["token_hash"] == hash_token("liniot_secret")
    assert store.count_tokens() == 1

    found = store.find_token(hash_token("liniot_secret"))
    assert found is not None and found["id"] == token["id"]
    assert store.find_token(hash_token("wrong-token")) is None

    # 首次使用记录 last_used_at；60 秒内重复请求不再写库（节流）
    first = store.list_tokens()[0]["last_used_at"]
    _time.sleep(0.01)
    store.find_token(hash_token("liniot_secret"))  # 节流窗口内不刷新
    assert store.list_tokens()[0]["last_used_at"] == first

    # 吊销后查找视为无效；重复吊销幂等；记录保留供审计
    revoked = store.revoke_token(token["id"])
    assert revoked["revoked_at"] is not None
    assert store.find_token(hash_token("liniot_secret")) is None
    assert store.revoke_token(token["id"])["revoked_at"] == revoked["revoked_at"]
    assert len(store.list_tokens()) == 1  # 已吊销记录仍在列表中
    assert store.revoke_token(999) is None
