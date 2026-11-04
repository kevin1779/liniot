"""AI 助手：用自然语言查询设备状态、下发控制命令。

两种工作模式：
1. LLM 模式（配置 LINIOT_AI_API_KEY 后启用）：把设备快照注入 System Prompt，
   要求模型只输出 {"reply": ..., "actions": [...]} 结构化 JSON，平台校验后执行；
2. 本地回退模式（未配置 Key）：基于关键词的意图识别，离线也能完整演示。

安全边界：AI 只能调用白名单动作 set_property，且仅允许修改设备已有属性，
不能执行任意代码或访问文件系统；所有下发均落入命令表可审计。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import httpx

from .config import AIConfig

logger = logging.getLogger("liniot.ai")

SYSTEM_PROMPT = """你是 LinIoT 物联网平台的智能助手，帮助用户查询设备状态和控制设备。

当前设备快照（JSON）：
{snapshot}

你必须遵守：
1. 只能通过 actions 中的 set_property 控制设备，格式：
   {{"tool": "set_property", "device": "设备ID", "property": "属性名", "value": 值}}
2. property 只能使用设备快照中 properties 里已有的属性名；
3. 查询类问题（温度、湿度、状态等）直接根据快照回答，不需要 action；
4. 必须只输出一个 JSON 对象，不要输出 markdown 代码块或其他内容，格式：
   {{"reply": "给用户的简洁中文回答", "actions": []}}"""

DIAGNOSIS_PROMPT = """你是 LinIoT 物联网平台的告警诊断助手。

告警涉及设备的实时快照（JSON）：
{snapshot}

刚触发的告警：
{alert}

请用不超过三句中文，先分析可能原因，再给一条具体处置建议。
只输出纯文本，不要 JSON、不要标题、不要表情符号。"""

HELP_TEXT = ("我可以帮你查询设备数据和下发控制命令，例如：\n"
             "· 客厅温度是多少？\n"
             "· 打开所有灯 / 关闭智能灯-1\n"
             "· 湿度情况怎么样？\n"
             "· 设备概览\n"
             "（当前为本地规则模式，配置 LINIOT_AI_API_KEY 后可获得完整大模型能力）")


def build_snapshot(store, service) -> list[dict[str, Any]]:
    latest = store.latest_metrics()
    devices = []
    for device in store.list_devices():
        view = service.view(device)
        metrics = {metric: {"value": info["value"], "ts": info["ts"]}
                   for (dev_id, metric), info in latest.items() if dev_id == device["id"]}
        devices.append({"id": view["id"], "name": view["name"], "type": view["type"],
                        "online": view["online"], "properties": view["properties"],
                        "metrics": metrics})
    return devices


async def call_llm(config: AIConfig, messages: list[dict[str, str]]) -> str:
    async with httpx.AsyncClient(timeout=config.timeout) as client:
        resp = await client.post(
            f"{config.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {config.api_key}"},
            json={"model": config.model, "messages": messages, "temperature": 0.2},
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


class DiagnosisRateLimiter:
    """按设备的诊断冷却：同一设备在 cooldown 秒内只诊断一次（Issue #10）。

    传感器抖动或规则在阈值附近反复触发时，告警会密集产生；
    没有限流的话 LLM 调用次数与费用都会失控。冷却状态按设备独立记录。
    """

    def __init__(self, cooldown: float = 300.0) -> None:
        self.cooldown = cooldown
        self._last: dict[str, float] = {}

    def allow(self, device_id: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        last = self._last.get(device_id)
        if last is not None and now - last < self.cooldown:
            return False
        self._last[device_id] = now
        return True


async def diagnose_alert(store, service, config: AIConfig,
                         alert: dict[str, Any]) -> str | None:
    """对单条告警生成原因分析与处置建议；失败返回 None（静默降级）。"""
    if not config.enabled:
        return None
    device_id = alert.get("device_id")
    snapshot = [d for d in build_snapshot(store, service) if d["id"] == device_id]
    messages = [{"role": "system",
                 "content": DIAGNOSIS_PROMPT.format(
                     snapshot=json.dumps(snapshot, ensure_ascii=False),
                     alert=alert.get("message", ""))}]
    try:
        text = await call_llm(config, messages)
        return (text or "").strip()[:500] or None
    except Exception as exc:
        logger.warning("告警诊断生成失败: %s", exc)
        return None


def extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出中稳健地抽取第一个 JSON 对象（容忍代码块围栏与前后缀文本）。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text, flags=re.S)
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(text[i:])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue
    return None


def _ago(ts: float) -> str:
    delta = max(0, time.time() - ts)
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    return f"{int(delta // 86400)} 天前"


def local_intent(message: str, snapshot: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """无 Key 的关键词意图识别：查温度/湿度、开关设备、设备概览。"""
    text = message.strip()
    devices = snapshot

    def match(keyword: str) -> list[dict[str, Any]]:
        low = text.lower()
        return [d for d in devices
                if keyword in d["name"].lower() or keyword == d["id"].lower()
                or keyword in d["id"]]

    turn_on = re.search(r"打开|开启|开灯|启动", text)
    turn_off = re.search(r"关闭|关掉|关灯|停止", text)
    if turn_on or turn_off:
        named = [d for d in devices if any(
            kw in text and kw in (d["name"] + d["id"]) for kw in [d["name"], d["id"]])]
        targets = named or [d for d in devices
                            if d["type"] in ("light", "switch", "actuator")
                            or "power" in d["properties"] or "on" in d["properties"]]
        actions, names = [], []
        for device in targets:
            prop = next((p for p in ("power", "on") if p in device["properties"]), None)
            if prop is None:
                continue
            actions.append({"tool": "set_property", "device": device["id"],
                            "property": prop, "value": bool(turn_on)})
            names.append(device["name"])
        if not actions:
            return "没有找到可控制的设备（需要设备带有 power/on 属性）。", []
        verb = "打开" if turn_on else "关闭"
        return f"好的，已下发命令：{verb} {'、'.join(names)}。", actions

    if "温度" in text or "湿度" in text:
        want_humidity = "湿度" in text and "温度" not in text
        metric = "humidity" if want_humidity else "temperature"
        unit = "%" if want_humidity else "°C"
        lines = []
        for device in devices:
            info = device["metrics"].get(metric)
            if info:
                lines.append(f"· {device['name']}：{info['value']:g}{unit}（{_ago(info['ts'])}）")
        if not lines:
            return f"当前没有设备上报{ '湿度' if want_humidity else '温度'}数据。", []
        return "最新" + ("湿度" if want_humidity else "温度") + "数据：\n" + "\n".join(lines), []

    if re.search(r"概览|状态|所有设备|全部设备|设备列表", text):
        if not devices:
            return "当前平台还没有注册任何设备。", []
        lines = []
        for device in devices:
            props = "、".join(f"{k}={v}" for k, v in device["properties"].items()) or "无属性"
            metrics = "、".join(f"{m}={info['value']:g}"
                                for m, info in device["metrics"].items()) or "无数据"
            lines.append(f"· {device['name']}（{device['type']}，"
                         f"{'在线' if device['online'] else '离线'}）：{props}；{metrics}")
        return "设备概览：\n" + "\n".join(lines), []

    if re.search(r"帮助|help|你能做什么|怎么用", text, re.I):
        return HELP_TEXT, []

    return ("我还没理解这句话。当前为本地规则模式（未配置 AI Key），"
            "可以试试：\n" + HELP_TEXT, [])


def _validate_action(action: dict[str, Any], devices: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not isinstance(action, dict) or action.get("tool") != "set_property":
        return None
    device_id, prop, value = action.get("device"), action.get("property"), action.get("value")
    device = next((d for d in devices if d["id"] == device_id), None)
    if device is None:
        return {"ok": False, "detail": f"未知设备 {device_id}", "action": action}
    if not isinstance(prop, str) or prop not in device["properties"]:
        return {"ok": False, "detail": f"设备 {device_id} 不存在属性 {prop}", "action": action}
    if not isinstance(value, (bool, int, float, str)):
        return {"ok": False, "detail": f"属性值类型不支持: {value!r}", "action": action}
    return {"ok": True, "action": {"tool": "set_property", "device": device_id,
                                   "property": prop, "value": value}}


async def chat(store, service, config: AIConfig, message: str,
               history: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """对话入口：返回 {reply, actions, mode}。"""
    snapshot = build_snapshot(store, service)
    mode, actions, reply = "local", [], ""

    if config.enabled:
        try:
            messages = [{"role": "system",
                         "content": SYSTEM_PROMPT.format(
                             snapshot=json.dumps(snapshot, ensure_ascii=False))}]
            for item in (history or [])[-8:]:
                if item.get("role") in ("user", "assistant") and item.get("content"):
                    messages.append({"role": item["role"], "content": item["content"][:2000]})
            messages.append({"role": "user", "content": message})
            raw = await call_llm(config, messages)
            parsed = extract_json(raw)
            if parsed and isinstance(parsed.get("reply"), str):
                reply = parsed["reply"]
                raw_actions = parsed.get("actions") or []
                actions = [a for a in (_validate_action(x, snapshot) for x in raw_actions
                                       if isinstance(x, dict)) if a]
                mode = "llm"
            else:
                logger.warning("LLM 输出解析失败，回退本地规则: %s", raw[:200])
        except Exception as exc:
            logger.warning("LLM 调用失败，回退本地规则: %s", exc)

    if mode == "local":
        reply, wanted = local_intent(message, snapshot)
        actions = [a for a in (_validate_action(x, snapshot) for x in wanted) if a]

    results: list[dict[str, Any]] = []
    executed: list[dict[str, Any]] = []
    for item in actions:
        action = item["action"]
        if item["ok"]:
            service.set_properties(action["device"], {action["property"]: action["value"]})
            results.append({"ok": True,
                            "detail": f"已下发：{action['device']}.{action['property']} = "
                                      f"{json.dumps(action['value'], ensure_ascii=False)}"})
            executed.append(action)
        else:
            results.append(item)

    if results:
        reply += "\n" + "\n".join(
            ("[已执行] " if r["ok"] else "[已拒绝] ") + r["detail"] for r in results)
    return {"reply": reply, "actions": executed, "results": results, "mode": mode}
