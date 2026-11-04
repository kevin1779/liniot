"""API 请求/响应模型（pydantic v2）。"""
from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel, Field

PropertyValue = Union[bool, int, float, str]


class DeviceIn(BaseModel):
    id: Optional[str] = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=64)
    type: str = Field(default="generic", max_length=32)
    meta: dict[str, str] = Field(default_factory=dict)
    group: Optional[str] = Field(default=None, max_length=32)  # 写入 meta["group"]（Issue #3）


class TelemetryPoint(BaseModel):
    metric: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,32}$")
    value: float
    ts: Optional[float] = None


class TelemetryIn(BaseModel):
    """单条或批量上报；points 缺省时兼容 {metric, value} 单点形式。"""

    metric: Optional[str] = None
    value: Optional[float] = None
    ts: Optional[float] = None
    points: Optional[list[TelemetryPoint]] = None


class StatusIn(BaseModel):
    status: Literal["online", "offline"]
    properties: dict[str, PropertyValue] = Field(default_factory=dict)


class PropertiesIn(BaseModel):
    properties: dict[str, PropertyValue]


class RuleIn(BaseModel):
    device_id: Optional[str] = None  # 为空时对该 metric 的所有设备生效
    metric: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,32}$")
    op: Literal[">", ">=", "<", "<=", "=="]
    threshold: float
    note: str = Field(default="", max_length=200)
    enabled: bool = True


class RulePatch(BaseModel):
    enabled: Optional[bool] = None
    note: Optional[str] = None
    threshold: Optional[float] = None


class SceneIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    source_device_id: Optional[str] = None  # 为空时任意设备的该指标都触发
    metric: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,32}$")
    op: Literal[">", ">=", "<", "<=", "=="]
    threshold: float
    action_device_id: str
    action_property: str = Field(min_length=1, max_length=32)
    action_value: PropertyValue
    cooldown: int = Field(default=60, ge=1, le=86400)


class ScenePatch(BaseModel):
    enabled: Optional[bool] = None
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    threshold: Optional[float] = None
    cooldown: Optional[int] = Field(default=None, ge=1, le=86400)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=500)
    history: Optional[list[dict[str, str]]] = None
