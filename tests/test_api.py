"""API 集成测试：REST 端点 + WebSocket。"""
from __future__ import annotations


def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_device_crud_and_telemetry_flow(client):
    resp = client.post("/api/devices", json={"id": "sensor-1", "name": "客厅传感器",
                                             "type": "sensor"})
    assert resp.status_code == 201

    # 幂等注册返回 200
    again = client.post("/api/devices", json={"id": "sensor-1", "name": "客厅传感器",
                                              "type": "sensor"})
    assert again.status_code == 200

    batch = {"points": [{"metric": "temperature", "value": 25.5},
                        {"metric": "humidity", "value": 60}]}
    assert client.post("/api/devices/sensor-1/telemetry", json=batch).json()["accepted"] == 2

    series = client.get("/api/devices/sensor-1/telemetry",
                        params={"metric": "temperature"}).json()
    assert len(series) == 1 and series[0]["value"] == 25.5

    detail = client.get("/api/devices/sensor-1").json()
    assert detail["online"] is True  # 刚上报过数据

    assert client.delete("/api/devices/sensor-1").status_code == 200
    assert client.get("/api/devices/sensor-1").status_code == 404


def test_telemetry_single_point_compat(client):
    client.post("/api/devices", json={"id": "sensor-1", "name": "s", "type": "sensor"})
    resp = client.post("/api/devices/sensor-1/telemetry",
                       json={"metric": "temperature", "value": 21})
    assert resp.json()["accepted"] == 1


def test_rules_crud_and_alert_via_api(client):
    client.post("/api/devices", json={"id": "sensor-1", "name": "s", "type": "sensor"})
    rule = client.post("/api/rules", json={"device_id": "sensor-1", "metric": "temperature",
                                           "op": ">", "threshold": 30}).json()
    assert rule["id"] > 0

    client.post("/api/devices/sensor-1/telemetry",
                json={"metric": "temperature", "value": 35})
    alerts = client.get("/api/alerts").json()
    assert len(alerts) == 1 and "35" in alerts[0]["message"]

    assert client.patch(f"/api/rules/{rule['id']}", json={"enabled": False}).json()["enabled"] == 0
    assert client.delete(f"/api/rules/{rule['id']}").status_code == 200


def test_properties_command_flow(client):
    client.post("/api/devices", json={"id": "light-1", "name": "灯", "type": "light"})
    client.post("/api/devices/light-1/status",
                json={"status": "online", "properties": {"power": False}})

    queued = client.post("/api/devices/light-1/properties",
                         json={"properties": {"power": True}}).json()["queued"]
    assert queued[0]["payload"] == {"property": "power", "value": True}

    commands = client.get("/api/devices/light-1/commands").json()
    assert commands[0]["payload"]["value"] is True
    assert client.get("/api/devices/light-1/commands").json() == []


def test_websocket_hello(client):
    with client.websocket_connect("/ws") as ws:
        hello = ws.receive_json()
    assert hello["type"] == "hello"
    assert hello["data"]["overview"]["devices"] == 0


def test_overview(client):
    client.post("/api/devices", json={"id": "sensor-1", "name": "s", "type": "sensor"})
    client.post("/api/devices/sensor-1/telemetry",
                json={"metric": "temperature", "value": 20})
    overview = client.get("/api/overview").json()
    assert overview == {"devices": 1, "online": 1, "telemetry_points": 1,
                        "alerts_24h": 0, "rules": 0}


def test_ai_chat_endpoint_local_mode(client):
    client.post("/api/devices", json={"id": "light-1", "name": "智能灯", "type": "light"})
    client.post("/api/devices/light-1/status",
                json={"status": "online", "properties": {"power": False}})

    result = client.post("/api/ai/chat", json={"message": "打开所有的灯"}).json()
    assert result["mode"] == "local"
    assert "智能灯" in result["reply"]
    assert client.get("/api/devices/light-1/commands").json()[0]["payload"]["value"] is True


def test_scenes_crud_and_trigger(client):
    client.post("/api/devices", json={"id": "sensor-1", "name": "s", "type": "sensor"})
    client.post("/api/devices", json={"id": "light-1", "name": "灯", "type": "light"})
    client.post("/api/devices/light-1/status",
                json={"status": "online", "properties": {"power": False}})

    scene = client.post("/api/scenes", json={
        "name": "高温开灯", "metric": "temperature", "op": ">", "threshold": 28,
        "action_device_id": "light-1", "action_property": "power",
        "action_value": True, "cooldown": 60}).json()
    assert scene["id"] > 0 and scene["enabled"] == 1

    client.post("/api/devices/sensor-1/telemetry",
                json={"metric": "temperature", "value": 30})
    commands = client.get("/api/devices/light-1/commands").json()
    assert commands[0]["payload"] == {"property": "power", "value": True}

    # 冷却窗口内不重复下发
    client.post("/api/devices/sensor-1/telemetry",
                json={"metric": "temperature", "value": 31})
    assert client.get("/api/devices/light-1/commands").json() == []

    assert client.patch(f"/api/scenes/{scene['id']}",
                        json={"enabled": False}).json()["enabled"] == 0
    assert client.delete(f"/api/scenes/{scene['id']}").status_code == 200
    assert client.get("/api/scenes").json() == []


def test_scene_rejects_unknown_devices(client):
    resp = client.post("/api/scenes", json={
        "name": "x", "metric": "temperature", "op": ">", "threshold": 1,
        "action_device_id": "ghost", "action_property": "power", "action_value": True})
    assert resp.status_code == 404


def test_telemetry_export_csv(client):
    client.post("/api/devices", json={"id": "sensor-1", "name": "客厅传感器", "type": "sensor"})
    client.post("/api/devices/sensor-1/telemetry",
                json={"points": [{"metric": "temperature", "value": 25.5},
                                 {"metric": "humidity", "value": 60},
                                 {"metric": "temperature", "value": 26.5}]})

    resp = client.get("/api/devices/sensor-1/telemetry/export",
                      params={"metric": "temperature"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers["content-disposition"]
    assert 'filename="liniot-sensor-1-temperature.csv"' in resp.headers["content-disposition"]

    # UTF-8 BOM 开头，Excel 按其识别编码；表头之后为该指标的两条数据
    lines = resp.content.decode("utf-8-sig").strip().splitlines()
    assert lines[0] == "device_id,metric,value,ts,time"
    assert len(lines) == 3
    assert lines[1].startswith("sensor-1,temperature,25.5,")
    assert lines[2].startswith("sensor-1,temperature,26.5,")
    assert lines[1].count(",") == 4  # time 列存在

    # 不带 metric 导出全部指标；limit=1 只取最近一条
    all_rows = client.get("/api/devices/sensor-1/telemetry/export").content
    assert len(all_rows.decode("utf-8-sig").strip().splitlines()) == 4
    one = client.get("/api/devices/sensor-1/telemetry/export",
                     params={"limit": 1}).content.decode("utf-8-sig").strip().splitlines()
    assert len(one) == 2 and one[1].startswith("sensor-1,temperature,26.5,")


def test_telemetry_export_edge_cases(client):
    client.post("/api/devices", json={"id": "sensor-1", "name": "s", "type": "sensor"})

    # 无数据设备：仅表头
    empty = client.get("/api/devices/sensor-1/telemetry/export")
    assert empty.status_code == 200
    assert empty.content.decode("utf-8-sig").strip().splitlines() == \
        ["device_id,metric,value,ts,time"]

    # 设备不存在 404；metric 非法 422
    assert client.get("/api/devices/ghost/telemetry/export").status_code == 404
    resp = client.get("/api/devices/sensor-1/telemetry/export",
                      params={"metric": "bad metric!"})
    assert resp.status_code == 422


def test_telemetry_aggregate_windows(client):
    client.post("/api/devices", json={"id": "sensor-1", "name": "s", "type": "sensor"})
    client.post("/api/devices/sensor-1/telemetry", json={"points": [
        {"metric": "temperature", "value": 10.0, "ts": 30.0},
        {"metric": "temperature", "value": 20.0, "ts": 90.0},
        {"metric": "temperature", "value": 30.0, "ts": 150.0},
    ]})

    one_min = client.get("/api/devices/sensor-1/telemetry/aggregate",
                         params={"metric": "temperature", "window": "1m"}).json()
    assert [b["ts"] for b in one_min] == [0, 60, 120]
    assert [b["count"] for b in one_min] == [1, 1, 1]

    five_min = client.get("/api/devices/sensor-1/telemetry/aggregate",
                          params={"metric": "temperature", "window": "5m"}).json()
    assert len(five_min) == 1
    assert (five_min[0]["count"], five_min[0]["avg"], five_min[0]["min"],
            five_min[0]["max"]) == (3, 20.0, 10.0, 30.0)

    assert client.get("/api/devices/sensor-1/telemetry/aggregate",
                      params={"window": "1h"}).status_code == 200
    assert client.get("/api/devices/sensor-1/telemetry/aggregate",
                      params={"window": "2h"}).status_code == 422
    assert client.get("/api/devices/ghost/telemetry/aggregate").status_code == 404
    assert client.get("/api/devices/sensor-1/telemetry/aggregate",
                      params={"metric": "bad metric!"}).status_code == 422


def test_device_group_register_filter_preserve(client):
    # 注册带分组 / 不带分组
    client.post("/api/devices", json={"id": "s1", "name": "客厅", "type": "sensor", "group": "一楼"})
    client.post("/api/devices", json={"id": "s2", "name": "机房", "type": "sensor", "group": "二楼"})
    client.post("/api/devices", json={"id": "s3", "name": "未分组", "type": "sensor"})
    assert client.get("/api/devices/s1").json()["meta"]["group"] == "一楼"

    # 按分组过滤
    rows = client.get("/api/devices", params={"group": "一楼"}).json()
    assert [d["id"] for d in rows] == ["s1"]
    assert client.get("/api/devices", params={"group": "不存在的组"}).json() == []

    # 幂等重注册未指定分组时保留原分组；显式指定则更新
    client.post("/api/devices", json={"id": "s1", "name": "客厅", "type": "sensor"})
    assert client.get("/api/devices/s1").json()["meta"]["group"] == "一楼"
    client.post("/api/devices", json={"id": "s1", "name": "客厅", "type": "sensor", "group": "三楼"})
    assert client.get("/api/devices/s1").json()["meta"]["group"] == "三楼"


def test_overview_series_includes_group(client):
    client.post("/api/devices", json={"id": "s1", "name": "客厅", "type": "sensor", "group": "一楼"})
    client.post("/api/devices", json={"id": "s2", "name": "机房", "type": "sensor"})
    client.post("/api/devices/s1/telemetry", json={"metric": "temperature", "value": 25})

    series = client.get("/api/overview/series").json()
    assert series["s1"]["group"] == "一楼"
    assert series["s2"]["group"] == ""
    assert series["s1"]["metrics"]["temperature"][-1]["value"] == 25


def test_alert_acknowledge_flow(client):
    client.post("/api/devices", json={"id": "s1", "name": "s", "type": "sensor"})
    client.post("/api/rules", json={"device_id": "s1", "metric": "temperature",
                                    "op": ">", "threshold": 30})
    client.post("/api/devices/s1/telemetry", json={"metric": "temperature", "value": 31})

    open_alerts = client.get("/api/alerts", params={"ack": "open"}).json()
    assert len(open_alerts) == 1 and open_alerts[0]["acknowledged"] == 0

    acked = client.post(f"/api/alerts/{open_alerts[0]['id']}/ack").json()
    assert acked["acknowledged"] == 1 and acked["acknowledged_at"] > 0

    assert client.get("/api/alerts", params={"ack": "open"}).json() == []
    assert len(client.get("/api/alerts", params={"ack": "acked"}).json()) == 1
    assert client.post("/api/alerts/999/ack").status_code == 404


def test_auth_token_mode(tmp_path):
    import pytest
    from fastapi.testclient import TestClient
    from liniot.config import Config
    from liniot.main import create_app

    config = Config(db_path=tmp_path / "auth.db", web_dir=tmp_path / "no-web",
                    auth_token="secret-token")
    app = create_app(config)
    with TestClient(app) as c:
        # 健康检查保持开放，且暴露 auth 状态
        assert c.get("/api/health").json()["auth"] is True
        # 无令牌 / 错误令牌 → 401
        assert c.get("/api/devices").status_code == 401
        assert c.get("/api/devices", headers={"X-Auth-Token": "wrong"}).status_code == 401
        # X-Auth-Token / Bearer 两种携带方式均可
        assert c.get("/api/devices", headers={"X-Auth-Token": "secret-token"}).status_code == 200
        assert c.get("/api/devices",
                     headers={"Authorization": "Bearer secret-token"}).status_code == 200
        # 写操作同样受保护
        assert c.post("/api/devices", json={"name": "x"}).status_code == 401

        # WebSocket：无令牌被拒，带令牌可连
        from starlette.websockets import WebSocketDisconnect
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws"):
                pass
        with c.websocket_connect("/ws?token=secret-token") as ws:
            assert ws.receive_json()["type"] == "hello"


def test_multi_user_token_roles(tmp_path):
    from fastapi.testclient import TestClient
    from liniot.config import Config
    from liniot.main import create_app

    config = Config(db_path=tmp_path / "roles.db", web_dir=tmp_path / "no-web",
                    auth_token="admin-key",
                    api_tokens=({"name": "李四", "token": "dev-key", "role": "device"},
                                {"name": "王五", "token": "view-key", "role": "viewer"}))
    app = create_app(config)
    with TestClient(app) as c:
        # 管理员注册设备 + 建规则
        h_admin = {"X-Auth-Token": "admin-key"}
        assert c.get("/api/devices", headers=h_admin).status_code == 200
        assert c.post("/api/devices", json={"id": "s1", "name": "s", "type": "sensor"},
                      headers=h_admin).status_code == 201

        h_dev = {"X-Auth-Token": "dev-key"}
        h_view = {"X-Auth-Token": "view-key"}

        # device 角色：可上报、可拉命令；不可管理（规则/AI/删除）
        assert c.post("/api/devices/s1/telemetry", json={"metric": "temperature", "value": 25},
                      headers=h_dev).status_code == 200
        assert c.get("/api/devices/s1/commands", headers=h_dev).status_code == 200
        assert c.post("/api/rules", json={"metric": "t", "op": ">", "threshold": 1},
                      headers=h_dev).status_code == 403
        assert c.post("/api/ai/chat", json={"message": "x"},
                      headers=h_dev).status_code == 403
        assert c.delete("/api/devices/s1", headers=h_dev).status_code == 403

        # viewer 角色：可读；任何写操作 403
        assert c.get("/api/devices", headers=h_view).status_code == 200
        assert c.post("/api/devices/s1/telemetry", json={"metric": "temperature", "value": 26},
                      headers=h_view).status_code == 403
        assert c.delete("/api/devices/s1", headers=h_view).status_code == 403

        # 无效令牌 401（与角色无权 403 区分）
        assert c.get("/api/devices", headers={"X-Auth-Token": "bad"}).status_code == 401

        # WebSocket：任一有效主体可订阅
        with c.websocket_connect("/ws?token=view-key") as ws:
            assert ws.receive_json()["type"] == "hello"


def test_parse_api_tokens(tmp_path):
    from liniot.config import parse_api_tokens

    entries = parse_api_tokens("张三:t1:admin; 李四:t2:device; bad; 王五:t3:hack")
    assert [e["name"] for e in entries] == ["张三", "李四"]
    assert entries[1]["role"] == "device"


def test_validation_errors(client):
    assert client.post("/api/devices", json={"name": "", "type": "x"}).status_code == 422
    assert client.post("/api/devices", json={"id": "非法ID", "name": "x"}).status_code == 422
    assert client.get("/api/devices/ghost/telemetry").status_code == 404
