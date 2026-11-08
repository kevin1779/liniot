"""REST + WebSocket API 路由。"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import re
import time
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, \
    WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from . import ai
from .config import Config
from .schemas import (ChatIn, DeviceIn, PropertiesIn, RuleIn, RulePatch,
                      SceneIn, ScenePatch, StatusIn, TelemetryIn)
from .service import METRIC_RE, DeviceService, UnknownDevice

logger = logging.getLogger("liniot.api")

WINDOW_MAP = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600}


def build_router(store, bus, rules, devices: DeviceService, config: Config):
    DEVICE_WRITE_RE = re.compile(
        r"^/api/devices(?:$|/[^/]+/(?:telemetry|status|commands)$)")

    def resolve_principal(request: Request) -> dict[str, str] | None:
        """解析访问主体；未配置任何令牌时全员匿名管理员（开箱即用）。"""
        if not config.auth_token and not config.api_tokens:
            return {"name": "anonymous", "role": "admin"}
        supplied = request.headers.get("x-auth-token") or ""
        if not supplied:
            auth = request.headers.get("authorization") or ""
            supplied = auth[7:] if auth.lower().startswith("bearer ") else ""
        if config.auth_token and supplied == config.auth_token:
            return {"name": "shared", "role": "admin"}
        for entry in config.api_tokens:
            if entry["token"] == supplied:
                return {"name": entry["name"], "role": entry["role"]}
        return None

    def require_token(request: Request) -> None:
        """访问控制：401 = 令牌无效；403 = 角色无权执行该操作。

        角色矩阵（Issue #9 延伸的多用户体系）：
            admin  全部操作
            device 读取 + 上报遥测/状态 + 拉取命令 + 设备自注册
            viewer 只读
        """
        principal = resolve_principal(request)
        if principal is None:
            raise HTTPException(401, "需要有效的访问令牌")
        request.state.principal = principal
        role, method, path = principal["role"], request.method, request.url.path
        if role == "admin":
            return
        if role == "viewer" and method == "GET":
            return
        if role == "device" and (method == "GET" or
                                 (method == "POST" and DEVICE_WRITE_RE.match(path))):
            return
        raise HTTPException(403, f"角色 {role} 无权执行该操作")

    def device_or_404(device_id: str) -> dict[str, Any]:
        device = store.get_device(device_id)
        if device is None:
            raise HTTPException(404, f"设备不存在: {device_id}")
        return device

    # ------------------------------------------------------------------ 基础

    def health() -> dict[str, Any]:
        return {"status": "ok", "mqtt": config.mqtt.enabled, "ai": config.ai.enabled,
                "modbus": config.modbus.enabled, "auth": bool(config.auth_token)}

    # 健康检查保持开放（探活用）；其余 /api/* 在配置令牌后需要鉴权
    open_router = APIRouter(prefix="/api")
    open_router.add_api_route("/health", health, methods=["GET"])
    router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

    @router.get("/overview")
    def overview() -> dict[str, Any]:
        return devices.overview()

    @router.get("/overview/series")
    def overview_series(limit: int = 120) -> dict[str, Any]:
        """概览页所有设备的近期曲线（单次请求，供仪表盘轮询/推送刷新）。"""
        result: dict[str, Any] = {}
        for device in store.list_devices():
            entry = {"name": device["name"], "type": device["type"],
                     "group": device["meta"].get("group", ""),
                     "online": devices.view(device)["online"], "metrics": {}}
            for metric in store.metrics_of(device["id"]):
                entry["metrics"][metric] = store.query_telemetry(device["id"], metric, limit)
            result[device["id"]] = entry
        return result

    # ------------------------------------------------------------------ 设备

    @router.get("/devices")
    def list_devices(group: str | None = None) -> list[dict[str, Any]]:
        device_list = [devices.view(d) for d in store.list_devices()]
        if group is not None:
            device_list = [d for d in device_list if d["meta"].get("group") == group]
        return device_list

    @router.post("/devices")
    def create_device(body: DeviceIn, response: Response) -> dict[str, Any]:
        # 幂等：同 id 重复注册时更新名称/类型/元数据，方便设备重启后重新注册
        existing = store.get_device(body.id) if body.id else None
        meta = dict(body.meta)
        group = (body.group or "").strip()
        if group:
            meta["group"] = group
        elif existing and existing["meta"].get("group"):
            # 重新注册未指定分组时保留原分组，避免设备重启上报把分组弄丢
            meta["group"] = existing["meta"]["group"]
        device, created = devices.register(body.name, body.type, body.id, meta)
        response.status_code = 201 if created else 200
        return {"device": devices.view(device), "created": created}

    @router.get("/devices/{device_id}")
    def get_device(device_id: str) -> dict[str, Any]:
        return devices.view(device_or_404(device_id))

    @router.delete("/devices/{device_id}")
    def delete_device(device_id: str) -> dict[str, Any]:
        device_or_404(device_id)
        devices.delete(device_id)
        return {"deleted": device_id}

    @router.get("/devices/{device_id}/telemetry")
    def get_telemetry(device_id: str, metric: str | None = None,
                      limit: int = 200) -> list[dict[str, Any]]:
        device_or_404(device_id)
        return store.query_telemetry(device_id, metric, max(1, min(limit, 2000)))

    @router.post("/devices/{device_id}/telemetry")
    def post_telemetry(device_id: str, body: TelemetryIn) -> dict[str, Any]:
        device_or_404(device_id)
        if body.points is not None:
            points = [p.model_dump() for p in body.points]
        elif body.metric is not None and body.value is not None:
            points = [{"metric": body.metric, "value": body.value, "ts": body.ts}]
        else:
            raise HTTPException(422, "需要 points 字段或 metric/value 单点字段")
        try:
            count = devices.ingest(device_id, points, source="http")
        except UnknownDevice:
            raise HTTPException(404, f"设备不存在: {device_id}")
        return {"accepted": count}

    @router.get("/devices/{device_id}/telemetry/export")
    def export_telemetry(device_id: str, metric: str | None = None,
                         limit: int | None = None) -> StreamingResponse:
        """流式导出遥测 CSV（UTF-8 BOM，Excel 直接打开中文不乱码）。"""
        device_or_404(device_id)
        if metric and not METRIC_RE.match(metric):
            raise HTTPException(422, f"metric 不合法: {metric}")
        limit = max(1, limit) if limit is not None else None
        filename = f"liniot-{device_id}" + (f"-{metric}" if metric else "") + ".csv"

        def rows() -> Iterator[str]:
            # 复用单个 StringIO，攒约 64KB 再产出：既保证流式内存有界，
            # 又避免逐行 yield 造成的小包网络开销
            buf = io.StringIO()
            writer = csv.writer(buf, lineterminator="\n")
            yield "\ufeff"  # UTF-8 BOM：Excel 依据它按 UTF-8 解码
            writer.writerow(["device_id", "metric", "value", "ts", "time"])
            for p in store.iter_telemetry(device_id, metric, limit):
                writer.writerow([device_id, p["metric"], p["value"], p["ts"],
                                 time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p["ts"]))])
                if buf.tell() >= 64 * 1024:
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate()
            if buf.tell():
                yield buf.getvalue()

        return StreamingResponse(rows(), media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @router.get("/devices/{device_id}/telemetry/aggregate")
    def aggregate_telemetry(device_id: str, metric: str | None = None,
                            window: str = "5m") -> list[dict[str, Any]]:
        """按时间窗聚合遥测（avg/min/max/count），大时间跨度曲线用。"""
        device_or_404(device_id)
        seconds = WINDOW_MAP.get(window)
        if seconds is None:
            raise HTTPException(422, f"window 仅支持: {', '.join(WINDOW_MAP)}")
        if metric is not None and not METRIC_RE.match(metric):
            raise HTTPException(422, f"metric 不合法: {metric}")
        return store.aggregate_telemetry(device_id, metric, seconds)

    @router.post("/devices/{device_id}/status")
    def post_status(device_id: str, body: StatusIn) -> dict[str, Any]:
        try:
            return devices.view(devices.set_status(device_id, body.status, body.properties))
        except UnknownDevice:
            raise HTTPException(404, f"设备不存在: {device_id}")

    @router.post("/devices/{device_id}/properties")
    def post_properties(device_id: str, body: PropertiesIn) -> dict[str, Any]:
        try:
            commands = devices.set_properties(device_id, body.properties)
        except UnknownDevice:
            raise HTTPException(404, f"设备不存在: {device_id}")
        return {"queued": commands}

    @router.get("/devices/{device_id}/commands")
    def get_commands(device_id: str) -> list[dict[str, Any]]:
        device_or_404(device_id)
        return devices.poll_commands(device_id)

    # ------------------------------------------------------------------ 规则

    @router.get("/rules")
    def list_rules() -> list[dict[str, Any]]:
        return store.list_rules()

    @router.post("/rules", status_code=201)
    def create_rule(body: RuleIn) -> dict[str, Any]:
        if body.device_id is not None:
            device_or_404(body.device_id)
        return store.create_rule(body.device_id, body.metric, body.op,
                                 body.threshold, body.note, body.enabled)

    @router.patch("/rules/{rule_id}")
    def patch_rule(rule_id: int, body: RulePatch) -> dict[str, Any]:
        rule = store.update_rule(rule_id, enabled=body.enabled, note=body.note,
                                 threshold=body.threshold)
        if rule is None:
            raise HTTPException(404, f"规则不存在: {rule_id}")
        return rule

    @router.delete("/rules/{rule_id}")
    def delete_rule(rule_id: int) -> dict[str, Any]:
        store.delete_rule(rule_id)
        return {"deleted": rule_id}

    @router.get("/alerts")
    def list_alerts(limit: int = 50, ack: str | None = None) -> list[dict[str, Any]]:
        return store.list_alerts(max(1, min(limit, 500)), ack=ack)

    @router.post("/alerts/{alert_id}/ack")
    def ack_alert(alert_id: int) -> dict[str, Any]:
        alert = store.acknowledge_alert(alert_id)
        if alert is None:
            raise HTTPException(404, f"告警不存在: {alert_id}")
        bus.publish({"type": "alert_ack",
                     "data": {"id": alert["id"], "acknowledged_at": alert["acknowledged_at"]}})
        return alert

    # ------------------------------------------------------------------ 场景

    @router.get("/scenes")
    def list_scenes() -> list[dict[str, Any]]:
        return store.list_scenes()

    @router.post("/scenes", status_code=201)
    def create_scene(body: SceneIn) -> dict[str, Any]:
        if body.source_device_id is not None:
            device_or_404(body.source_device_id)
        device_or_404(body.action_device_id)
        return store.create_scene(
            body.name, body.source_device_id, body.metric, body.op,
            body.threshold, body.action_device_id, body.action_property,
            json.dumps(body.action_value, ensure_ascii=False), body.cooldown)

    @router.patch("/scenes/{scene_id}")
    def patch_scene(scene_id: int, body: ScenePatch) -> dict[str, Any]:
        scene = store.update_scene(scene_id, enabled=body.enabled, name=body.name,
                                   threshold=body.threshold, cooldown=body.cooldown)
        if scene is None:
            raise HTTPException(404, f"场景不存在: {scene_id}")
        return scene

    @router.delete("/scenes/{scene_id}")
    def delete_scene(scene_id: int) -> dict[str, Any]:
        store.delete_scene(scene_id)
        return {"deleted": scene_id}

    # ------------------------------------------------------------------ AI

    @router.post("/ai/chat")
    async def ai_chat(body: ChatIn) -> dict[str, Any]:
        return await ai.chat(store, devices, config.ai, body.message, body.history)

    return [open_router, router]


def register_websocket(app, store, bus, devices: DeviceService, config) -> None:
    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        # 配置令牌后，WebSocket 用 ?token= 携带（浏览器 WS 无法自定义请求头）；
        # admin/device/viewer 任一有效主体均可订阅实时事件
        supplied = websocket.query_params.get("token") or ""
        valid = (config.auth_token and supplied == config.auth_token) or \
            any(e["token"] == supplied for e in config.api_tokens)
        if (config.auth_token or config.api_tokens) and not valid:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        queue = bus.subscribe()
        try:
            hello = {"type": "hello",
                     "data": {"devices": [devices.view(d) for d in store.list_devices()],
                              "overview": devices.overview()}}
            await websocket.send_text(json.dumps(hello, ensure_ascii=False))
            while True:
                message = await queue.get()
                await websocket.send_text(json.dumps(message, ensure_ascii=False))
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.debug("WebSocket 连接异常退出: %s", exc)
        finally:
            bus.unsubscribe(queue)
