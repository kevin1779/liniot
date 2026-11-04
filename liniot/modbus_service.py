"""Modbus TCP 接入适配器：可配置轮询从站寄存器并转为遥测（Issue #5）。

pymodbus 为可选依赖（pip install ".[modbus]"）；未安装时本服务不可用，
平台其余功能不受影响。接入模式与 MQTTService 一致：把外部协议转成
DeviceService 调用，存储 / 规则 / 仪表盘对数据来源无感知。

映射通过 LINIOT_MODBUS_MAP 配置（见 ModbusConfig 文档），设备需先注册。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger("liniot.modbus")

REGISTER_TYPES = ("holding", "input", "coil", "discrete")

try:  # 可选依赖：未安装时服务禁用，平台其余功能不受影响
    from pymodbus.client import ModbusTcpClient
except ImportError:  # pragma: no cover - 视环境而定
    ModbusTcpClient = None


@dataclass
class MapEntry:
    device_id: str
    unit: int
    register: str
    address: int
    metric: str
    scale: float = 1.0


def parse_map(spec: str) -> list[MapEntry]:
    """解析映射配置；格式非法的条目跳过并告警，不让单个错误拖垮启动。"""
    entries: list[MapEntry] = []
    for chunk in filter(None, (s.strip() for s in spec.split(";"))):
        parts = chunk.split(":")
        if len(parts) < 5:
            logger.warning("Modbus 映射格式错误（设备:从站:类型:地址:指标[:系数]）: %r", chunk)
            continue
        device_id, unit, register, address, metric = parts[:5]
        scale = float(parts[5]) if len(parts) > 5 else 1.0
        if register not in REGISTER_TYPES:
            logger.warning("Modbus 映射寄存器类型非法（%s）: %r", "/".join(REGISTER_TYPES), chunk)
            continue
        try:
            entries.append(MapEntry(device_id.strip(), int(unit), register,
                                    int(address), metric.strip(), scale))
        except ValueError:
            logger.warning("Modbus 映射数值非法: %r", chunk)
    return entries


class ModbusService:
    def __init__(self, config, service,
                 client_factory: Callable[[], Any] | None = None) -> None:
        self.config = config
        self.service = service
        self.entries = parse_map(config.map)
        # client_factory 供测试注入假客户端；生产环境用 pymodbus
        self._client_factory = client_factory or self._default_client
        self._client: Any = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        # 只看配置；pymodbus 未安装时由 _default_client 给出明确报错
        return self.config.enabled and bool(self.entries)

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="liniot-modbus", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:
                logger.exception("Modbus 轮询失败，%s 秒后重试", self.config.poll)
            self._stop.wait(self.config.poll)

    # ---------------------------------------------------------------- 轮询

    def _default_client(self) -> Any:
        if ModbusTcpClient is None:
            raise RuntimeError("未安装 pymodbus（pip install 'liniot[modbus]'）")
        return ModbusTcpClient(self.config.host, port=self.config.port)

    def _connect(self) -> None:
        if self._client is None:
            self._client = self._client_factory()
            if not self._client.connect():
                raise ConnectionError(f"Modbus 连接失败: {self.config.host}:{self.config.port}")

    def _read(self, entry: MapEntry) -> float:
        client = self._client
        if entry.register in ("holding", "input"):
            reader = (client.read_holding_registers if entry.register == "holding"
                      else client.read_input_registers)
            rr = reader(entry.address, count=1, slave=entry.unit)
            if getattr(rr, "isError", lambda: False)():
                raise IOError(f"Modbus 读取出错: {entry}")
            raw = rr.registers[0]
        else:
            reader = (client.read_coils if entry.register == "coil"
                      else client.read_discrete_inputs)
            rr = reader(entry.address, count=1, slave=entry.unit)
            if getattr(rr, "isError", lambda: False)():
                raise IOError(f"Modbus 读取出错: {entry}")
            raw = 1 if rr.bits[0] else 0
        return raw * entry.scale

    def poll_once(self) -> int:
        """执行一轮轮询：读取全部映射条目并入库，返回接受的数据点数。"""
        self._connect()
        by_device: dict[str, list[dict[str, Any]]] = {}
        for entry in self.entries:
            try:
                value = self._read(entry)
            except Exception as exc:
                # 单个条目失败不影响其余条目，下一轮重试
                logger.warning("Modbus 读取失败 %s: %s", entry, exc)
                continue
            by_device.setdefault(entry.device_id, []).append(
                {"metric": entry.metric, "value": value, "ts": time.time()})
        accepted = 0
        for device_id, points in by_device.items():
            accepted += self.service.ingest(device_id, points, source="modbus")
        return accepted
