"""LinIoT 配置模块：所有配置项均可通过环境变量覆盖。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MQTTConfig:
    """外接 MQTT Broker 配置。host 为空表示禁用 MQTT 通道（仅 HTTP 接入）。"""

    host: str = ""
    port: int = 1883
    username: str = ""
    password: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.host)


@dataclass
class AIConfig:
    """LLM 助手配置，兼容所有 OpenAI 风格 /chat/completions 接口。

    未配置 api_key 时自动降级为本地规则意图识别（离线可演示）。
    """

    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    timeout: float = 25.0
    diagnosis: bool = True   # 告警后自动生成 LLM 诊断（需启用大模型）
    diagnosis_cooldown: float = 300.0  # 同设备诊断冷却秒数（Issue #10）

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


@dataclass
class ModbusConfig:
    """Modbus TCP 接入配置（Issue #5）。host 为空表示禁用该通道。

    映射格式（LINIOT_MODBUS_MAP，分号分隔多条）：
        设备ID:从站地址:寄存器类型:起始地址:指标名[:系数]
    寄存器类型 ∈ holding / input / coil / discrete；系数默认 1.0。
    例：sensor-mb:1:holding:0:temperature:0.1;switch-mb:1:coil:0:relay
    """

    host: str = ""
    port: int = 502
    poll: float = 10.0
    map: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.host) and bool(self.map.strip())


@dataclass
class Config:
    db_path: Path = Path("data/liniot.db")
    retention_points: int = 20000        # 每设备保留的遥测点数上限
    online_timeout: float = 90.0         # 超过该秒数未见心跳视为离线
    webhook_url: str = ""                # 规则触发时的通用 JSON 回调
    alert_webhooks: tuple = ()           # 群机器人推送（钉钉/飞书/企微自动识别）
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)
    modbus: ModbusConfig = field(default_factory=ModbusConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    web_dir: Path = Path("web")          # 仪表盘静态资源目录


def load_config() -> Config:
    """从环境变量构建配置（每次调用重新读取，便于测试注入）。"""
    env = os.environ
    # 默认使用仓库根目录的 web/，无论从哪个工作目录启动都能找到仪表盘资源
    repo_web = Path(__file__).resolve().parent.parent / "web"
    web_default = str(repo_web) if repo_web.is_dir() else "web"
    cfg = Config(
        db_path=Path(env.get("LINIOT_DB_PATH", "data/liniot.db")),
        retention_points=int(env.get("LINIOT_RETENTION", "20000")),
        online_timeout=float(env.get("LINIOT_ONLINE_TIMEOUT", "90")),
        webhook_url=env.get("LINIOT_WEBHOOK_URL", ""),
        alert_webhooks=tuple(u for u in
                             (s.strip() for s in env.get("LINIOT_ALERT_WEBHOOKS", "").replace(";", ",").split(","))
                             if u),
        mqtt=MQTTConfig(
            host=env.get("LINIOT_MQTT_HOST", ""),
            port=int(env.get("LINIOT_MQTT_PORT", "1883")),
            username=env.get("LINIOT_MQTT_USERNAME", ""),
            password=env.get("LINIOT_MQTT_PASSWORD", ""),
        ),
        modbus=ModbusConfig(
            host=env.get("LINIOT_MODBUS_HOST", ""),
            port=int(env.get("LINIOT_MODBUS_PORT", "502")),
            poll=float(env.get("LINIOT_MODBUS_POLL", "10")),
            map=env.get("LINIOT_MODBUS_MAP", ""),
        ),
        ai=AIConfig(
            api_key=env.get("LINIOT_AI_API_KEY", ""),
            base_url=env.get("LINIOT_AI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            model=env.get("LINIOT_AI_MODEL", "gpt-4o-mini"),
            diagnosis=env.get("LINIOT_AI_DIAGNOSIS", "on").lower() not in ("off", "0", "false"),
            diagnosis_cooldown=float(env.get("LINIOT_AI_DIAGNOSIS_COOLDOWN", "300")),
        ),
        web_dir=Path(env.get("LINIOT_WEB_DIR", web_default)),
    )
    return cfg
