# AI 助手

LinIoT 内置自然语言助手：查询设备数据、下发控制命令。设计目标是
**接得上任意大模型，也离线可用**。

## 两种工作模式

| | LLM 模式 | 本地回退模式 |
|---|---|---|
| 启用条件 | 配置 `LINIOT_AI_API_KEY` | 默认（无 Key） |
| 能力 | 完整自然语言理解与推理 | 关键词意图：温度/湿度查询、开关灯、概览 |
| 输出标记 | `mode: "llm"` | `mode: "local"` |

配置示例（任选一家 OpenAI 兼容服务）：

```bash
# OpenAI
export LINIOT_AI_API_KEY=sk-xxx
export LINIOT_AI_BASE_URL=https://api.openai.com/v1
export LINIOT_AI_MODEL=gpt-4o-mini

# 或 DeepSeek
export LINIOT_AI_BASE_URL=https://api.deepseek.com/v1
export LINIOT_AI_MODEL=deepseek-chat

# 或本地 Ollama
export LINIOT_AI_BASE_URL=http://127.0.0.1:11434/v1
export LINIOT_AI_MODEL=qwen2.5:7b
```

## 工作流程

```
用户消息 ──▶ 组装设备快照（名称/在线/属性/最新指标，脱敏为白名单字段）
             │
             ├─ LLM 模式：System Prompt 注入快照 + 最近 8 轮对话
             │            要求只输出 {"reply": ..., "actions": [...]}
             │            ▶ 解析失败/网络失败 → 自动降级本地模式
             │
             └─ 逐条校验 actions（见安全边界）
                ▶ 合法动作调用 DeviceService.set_properties() 落库下发
                ▶ 非法动作拒绝，原因写入响应 results 可审计
```

System Prompt 全文见 `liniot/ai.py` 的 `SYSTEM_PROMPT`；模型温度 0.2 保证输出稳定。

## 安全边界

1. **动作白名单**：AI 只能调用 `set_property`，不能执行任意代码、读文件或访问网络。
2. **属性白名单**：只能修改设备 `properties` 中已存在的属性名（设备注册时声明），
   杜绝"幻觉属性"写入。
3. **值类型约束**：只接受标量（bool/int/float/str）。
4. **全量审计**：每条命令写入 `commands` 表，`results` 中逐条返回执行结果；
   真实生效仍需设备回报确认，AI 只是"发起"，不是"直接改状态"。

## API

`POST /api/ai/chat`

```json
{ "message": "打开所有的灯", "history": [{"role": "user", "content": "..."}] }
```

响应：

```json
{
  "reply": "好的，已下发命令：打开 智能灯-1、智能灯-2。",
  "actions": [{"tool": "set_property", "device": "light-1",
               "property": "power", "value": true}],
  "results": [{"ok": true, "detail": "已下发：light-1.power = true"}],
  "mode": "llm"
}
```

`history` 可选（最多取最近 8 轮），用于多轮对话。

## 本地回退模式支持的意图

| 示例说法 | 行为 |
|---|---|
| 「温度是多少」「湿度情况」 | 汇报所有设备最新温/湿度与数据时间 |
| 「打开所有的灯」「关闭智能灯-1」 | 对含 `power`/`on` 属性的设备下发命令 |
| 「设备概览」「所有设备状态」 | 列出每台设备的属性与最新指标 |
| 「你能做什么」 | 返回能力说明 |

## 告警智能诊断

配置 LLM Key 后，每条告警（阈值规则触发或异常检测触发）都会由后台协程
`diagnosis_worker` 异步生成诊断：把该设备的实时快照与告警内容交给模型，
要求不超过三句中文给出「可能原因 + 处置建议」，回写 `alerts.diagnosis`
并通过 WebSocket `alert_diagnosis` 事件推送到仪表盘。

- 异步执行，不阻塞数据面；失败静默降级（告警本身不受影响）
- **按设备冷却**：同一设备在冷却窗口内只诊断一次（默认 300 秒），
  避免传感器抖动时重复调用 LLM（Issue #10）；
  `LINIOT_AI_DIAGNOSIS_COOLDOWN=600` 可调整秒数
- `LINIOT_AI_DIAGNOSIS=off` 可整体关闭
- 未配置 Key 时 `diagnosis` 保持 `null`，界面不展示诊断栏

## 测试

LLM 路径在测试中 mock（`tests/test_ai.py`），覆盖：结构化输出解析、
非法动作拒绝、命令真实落库、无 Key 自动降级。对 `extract_json` 单独测试了
代码块围栏、前后缀文本等真实模型常见输出形态。
