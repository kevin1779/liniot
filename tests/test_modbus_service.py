"""Modbus TCP 接入适配器测试：映射解析、假客户端轮询、可选依赖降级。"""
from __future__ import annotations

import pytest

from liniot.bus import EventBus
from liniot.config import ModbusConfig
from liniot.modbus_service import ModbusService, parse_map
from liniot.rules import RuleEngine
from liniot.service import DeviceService
from liniot.store import Store


# ------------------------------------------------------------------ 映射解析

def test_parse_map_valid_entries():
    entries = parse_map("sensor-mb:1:holding:0:temperature:0.1;"
                        "switch-mb:2:coil:5:relay")
    assert len(entries) == 2
    assert entries[0].device_id == "sensor-mb"
    assert entries[0].unit == 1 and entries[0].address == 0
    assert entries[0].metric == "temperature" and entries[0].scale == 0.1
    assert entries[1].register == "coil" and entries[1].scale == 1.0


def test_parse_map_skips_invalid_entries():
    entries = parse_map("bad;sensor:1:wrong:0:t;sensor:1:holding:x:t;"
                        "sensor:1:input:3:humidity")
    assert len(entries) == 1  # 只有最后一条合法
    assert entries[0].metric == "humidity"


# ------------------------------------------------------------------ 轮询

class FakeResponse:
    def __init__(self, value, kind="reg", error=False):
        if kind == "reg":
            self.registers = [value]
        else:
            self.bits = [bool(value)]
        self._error = error

    def isError(self):
        return self._error


class FakeClient:
    """按 (slave, register, address) 返回预设值的假 Modbus 客户端。"""

    def __init__(self, values, fail_addresses=()):
        self.values = values
        self.fail_addresses = set(fail_addresses)
        self.connected = False
        self.closed = False

    def connect(self):
        self.connected = True
        return True

    def close(self):
        self.closed = True

    def _fail_or(self, key, value, kind="reg"):
        if key in self.fail_addresses:
            return FakeResponse(0, kind, error=True)
        return FakeResponse(value, kind)

    def read_holding_registers(self, address, count=1, slave=0):
        return self._fail_or((slave, "holding", address),
                             self.values.get((slave, "holding", address), 0))

    def read_input_registers(self, address, count=1, slave=0):
        return self._fail_or((slave, "input", address),
                             self.values.get((slave, "input", address), 0))

    def read_coils(self, address, count=1, slave=0):
        return self._fail_or((slave, "coil", address),
                             self.values.get((slave, "coil", address), 0), kind="bit")

    def read_discrete_inputs(self, address, count=1, slave=0):
        return self._fail_or((slave, "discrete", address),
                             self.values.get((slave, "discrete", address), 0), kind="bit")


def make_service(tmp_path, client=None, client_factory=None,
                 map_spec="sensor-mb:1:holding:0:temperature:0.1;"
                          "sensor-mb:1:coil:3:relay;"
                          "sensor-mb:1:input:8:humidity"):
    store = Store(tmp_path / "m.db")
    service = DeviceService(store, EventBus(), RuleEngine(store, EventBus()))
    service.register("Modbus 传感器", "sensor", device_id="sensor-mb")
    config = ModbusConfig(host="127.0.0.1", port=1502, poll=1.0, map=map_spec)
    if client_factory is None and client is not None:
        client_factory = lambda: client
    modbus = ModbusService(config, service, client_factory=client_factory)
    return store, service, modbus


def test_poll_once_reads_registers_and_coils(tmp_path):
    client = FakeClient({
        (1, "holding", 0): 254,   # × 0.1 → 25.4
        (1, "input", 8): 610,     # × 1.0 → 610.0
        (1, "coil", 3): 1,        # → 1.0
    })
    store, service, modbus = make_service(tmp_path, client)
    assert modbus.enabled is True

    accepted = modbus.poll_once()
    assert accepted == 3
    assert client.connected is True

    metrics = {r["metric"]: r["value"] for r in
               [dict(m) for m in _rows(store)]}
    assert metrics["temperature"] == pytest.approx(25.4)
    assert metrics["humidity"] == 610.0
    assert metrics["relay"] == 1.0
    # 停止时关闭连接
    modbus.stop()
    assert client.closed is True


def _rows(store):
    with store.lock:
        rows = store.conn.execute("SELECT metric, value FROM telemetry").fetchall()
    return rows


def test_poll_once_skips_failed_entry(tmp_path):
    client = FakeClient({(1, "holding", 0): 210},
                        fail_addresses={(1, "input", 8), (1, "coil", 3)})
    store, _, modbus = make_service(tmp_path, client)
    assert modbus.poll_once() == 1  # 只有 holding 成功
    metrics = {r["metric"] for r in _rows(store)}
    assert metrics == {"temperature"}


def test_poll_once_requires_client(tmp_path):
    def no_pymodbus():
        raise RuntimeError("未安装 pymodbus（pip install 'liniot[modbus]'）")
    store, _, modbus = make_service(tmp_path, client_factory=no_pymodbus)
    with pytest.raises(RuntimeError):
        modbus.poll_once()  # 缺依赖时给出明确报错


def test_disabled_service_never_starts(tmp_path):
    store = Store(tmp_path / "d.db")
    service = DeviceService(store, EventBus(), RuleEngine(store, EventBus()))
    modbus = ModbusService(ModbusConfig(host="", map=""), service)
    assert modbus.enabled is False
    modbus.start()
    assert modbus._thread is None  # 空配置不启动线程
    modbus.stop()
