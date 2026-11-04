"""FastAPI 应用工厂：组装存储、事件总线、规则引擎、MQTT 服务与 API。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import __version__
from . import ai
from .ai import DiagnosisRateLimiter
from .api import build_router, register_websocket
from .bus import EventBus
from .config import Config, load_config
from .modbus_service import ModbusService
from .mqtt_service import MQTTService
from .rules import RuleEngine
from .scene import SceneEngine
from .service import DeviceService
from .store import Store

DESCRIPTION = """LinIoT — 轻量级 AIoT 开源平台。

* 设备接入：MQTT（外接任意 Broker）与 HTTP 双通道
* 数据存储：SQLite 时序存储，按设备自动保留
* 规则引擎：阈值告警 + Webhook 通知
* 实时仪表盘：WebSocket 推送，零前端构建
* AI 助手：自然语言查询与控制设备（兼容 OpenAI 接口，可离线回退）
"""


async def diagnosis_worker(store, devices, config: Config, bus: EventBus) -> None:
    """监听告警事件，异步生成 LLM 诊断并回写；配置 Key 后自动启用。

    同一设备在 diagnosis_cooldown 秒内只诊断一次，避免传感器抖动时
    重复调用 LLM（Issue #10）。
    """
    queue = bus.subscribe()
    limiter = DiagnosisRateLimiter(config.ai.diagnosis_cooldown)
    while True:
        message = await queue.get()
        if message.get("type") != "alert":
            continue
        alert = message["data"]
        if alert.get("diagnosis"):
            continue
        if not limiter.allow(alert.get("device_id") or ""):
            continue
        text = await ai.diagnose_alert(store, devices, config.ai, alert)
        if text:
            store.set_alert_diagnosis(alert["id"], text)
            bus.publish({"type": "alert_diagnosis",
                         "data": {"id": alert["id"], "diagnosis": text}})


def create_app(config: Config | None = None) -> FastAPI:
    config = config or load_config()
    store = Store(config.db_path)
    bus = EventBus()
    rules = RuleEngine(store, bus, config.webhook_url)
    mqtt = MQTTService(config.mqtt, service=None)
    devices = DeviceService(store, bus, rules, mqtt, config.online_timeout,
                            config.webhook_url)
    mqtt.service = devices
    devices.scene_engine = SceneEngine(store, devices)
    modbus = ModbusService(config.modbus, devices)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bus.attach_loop(asyncio.get_running_loop())
        mqtt.start()
        modbus.start()
        worker = (asyncio.create_task(diagnosis_worker(store, devices, config, bus))
                  if config.ai.diagnosis else None)
        yield
        if worker is not None:
            worker.cancel()
        mqtt.stop()
        modbus.stop()

    app = FastAPI(title="LinIoT", version=__version__, description=DESCRIPTION,
                  lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                       allow_headers=["*"])
    app.include_router(build_router(store, bus, rules, devices, config))
    register_websocket(app, store, bus, devices)

    if config.web_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(config.web_dir), html=True), name="web")

    app.state.liniot = {"store": store, "bus": bus, "rules": rules,
                        "devices": devices, "mqtt": mqtt, "modbus": modbus,
                        "config": config}
    return app
