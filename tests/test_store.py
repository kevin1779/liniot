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


def test_commands_queue_and_deliver(tmp_path):
    store = Store(tmp_path / "a.db")
    store.upsert_device("light-1", "智能灯", "light", {})
    command = store.queue_command("light-1", {"property": "power", "value": True})
    assert command["payload"]["value"] is True

    pending = store.pending_commands("light-1")
    assert len(pending) == 1
    assert pending[0]["payload"] == {"property": "power", "value": True}
    assert store.pending_commands("light-1") == []  # 取走后不再返回
