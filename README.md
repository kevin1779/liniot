# LinIoT

**中文** | [English](#english)

轻量级 AIoT 开源平台：MQTT/HTTP 设备接入、时序存储、规则告警、实时仪表盘，以及可用自然语言查询和控制设备的 LLM 智能助手。

<p align="center">
  <img src="docs/images/dashboard.png" alt="LinIoT 仪表盘" width="820">
</p>

| | |
|---|---|
| CI | [![CI](https://github.com/kevin1779/liniot/actions/workflows/ci.yml/badge.svg)](https://github.com/kevin1779/liniot/actions/workflows/ci.yml) |
| 测试 | 57 个用例（存储 / 规则 / 异常检测 / 场景 / API / MQTT / AI / 总线 / 模拟器） |
| 依赖 | FastAPI + SQLite + paho-mqtt，前端零构建 |
| 协议 | MIT |

## 特性

- **设备接入**：MQTT（任意标准 Broker）、HTTP、Modbus TCP（工业设备，可选依赖）三通道，语义一致
- **时序存储**：SQLite 单文件存储，WAL 并发，按设备环形保留，自动清理
- **规则引擎**：阈值规则（6 种比较符），上升沿触发一次告警，按「规则 × 设备」独立判定，支持全局规则与 Webhook 通知
- **统计异常检测**：EWMA 基线 + 3σ 判定，无需配置规则即可发现指标突变
- **场景联动**：「温度 > 28 则开灯」式自动化，冷却窗口防止命令震荡
- **实时仪表盘**：原生 JS 零构建，WebSocket 推送（断线自动降级轮询），实时曲线、属性控制、规则/场景管理、告警中心
- **AI 助手**：自然语言查设备 / 下控制命令；兼容任意 OpenAI 风格 API（OpenAI / DeepSeek / Ollama / vLLM…），未配 Key 自动降级本地规则模式；**告警自动生成 LLM 诊断**（原因分析 + 处置建议）；AI 动作白名单校验 + 全量落库审计
- **设备模拟器**：无需任何硬件，3 条命令跑通全流程；附 ESP32 固件示例

## 快速开始

```bash
# 1. 安装（Python ≥ 3.10）
pip install -e .

# 2. 启动平台
liniot run                     # http://127.0.0.1:8000

# 3. 另开终端，启动虚拟设备
liniot simulate                # 2 台温湿度传感器 + 2 台智能灯
```

> **安全提示**：平台支持访问令牌鉴权——设置 `LINIOT_AUTH_TOKEN` 后，所有 API 与 WebSocket 需携带令牌（仪表盘会弹出验证框）。**生产 / 公网环境务必设置**，或改用防火墙 / 反向代理限制来源；仅本机体验可不设置。MQTT 通道建议直接使用 Broker 自带的用户名密码。

打开 http://127.0.0.1:8000 —— 概览页实时曲线、设备控制、规则告警、AI 对话全部可用。

启用 AI 大模型（可选，兼容任意 OpenAI 风格服务）：

```bash
export LINIOT_AI_API_KEY=sk-xxx
export LINIOT_AI_BASE_URL=https://api.openai.com/v1   # 或 https://api.deepseek.com/v1 等
export LINIOT_AI_MODEL=gpt-4o-mini
liniot run
```

接入 MQTT（可选，任选一种 Broker）：

```bash
export LINIOT_MQTT_HOST=broker.emqx.io    # 或自建 mosquitto，见 docker-compose.yml
liniot run
```

## 30 秒体验（不装任何东西）

```bash
git clone https://github.com/kevin1779/liniot.git && cd liniot
pip install -e .
liniot run &            # 平台
liniot simulate &       # 虚拟设备
bash examples/curl-demo.sh   # 纯 API 演示：注册→上报→规则→告警→AI
```

## 仪表盘

| 概览 | 设备管理 | AI 助手 |
|---|---|---|
| ![](docs/images/dashboard.png) | ![](docs/images/devices.png) | ![](docs/images/ai-assistant.png) |

- **概览**：设备在线数、数据点、告警统计 + 每设备每指标实时曲线（WebSocket 推送）
- **设备管理**：注册 / 删除设备，进入详情后可切换布尔属性（如灯开关）、下发烧写数值属性
- **规则与告警**：可视化配置阈值规则，告警历史实时滚动
- **AI 助手**：「温度是多少？」「打开所有的灯」直接可用

## 配置

全部通过环境变量，均有默认值：

| 变量 | 默认 | 说明 |
|---|---|---|
| `LINIOT_DB_PATH` | `data/liniot.db` | SQLite 文件路径 |
| `LINIOT_RETENTION` | `20000` | 每设备遥测保留条数 |
| `LINIOT_ONLINE_TIMEOUT` | `90` | 超过该秒数无上报视为离线 |
| `LINIOT_AUTH_TOKEN` | 空 | 访问令牌；设置后所有 API 与 WebSocket 需携带（仪表盘弹验证框） |
| `LINIOT_WEBHOOK_URL` | 空 | 告警回调地址（POST JSON） |
| `LINIOT_ALERT_WEBHOOKS` | 空 | 群机器人推送，分号分隔多个；按 URL 自动识别钉钉/飞书/企业微信，其他地址原样透传 JSON |
| `LINIOT_MQTT_HOST` | 空 | Broker 地址，空 = 禁用 MQTT |
| `LINIOT_MQTT_PORT` | `1883` | Broker 端口 |
| `LINIOT_MQTT_USERNAME` / `LINIOT_MQTT_PASSWORD` | 空 | Broker 凭据 |
| `LINIOT_AI_API_KEY` | 空 | LLM Key，空 = 本地规则模式 |
| `LINIOT_AI_BASE_URL` | `https://api.openai.com/v1` | 任意 OpenAI 兼容地址 |
| `LINIOT_AI_MODEL` | `gpt-4o-mini` | 模型名 |
| `LINIOT_MODBUS_HOST` | 空 | Modbus TCP 从站地址，空 = 禁用（需 `pip install ".[modbus]"`） |
| `LINIOT_MODBUS_PORT` | `502` | 从站端口 |
| `LINIOT_MODBUS_POLL` | `10` | 轮询间隔（秒） |
| `LINIOT_MODBUS_MAP` | 空 | 寄存器映射，见 [Modbus 指南](docs/modbus.md) |

## 文档

- [系统架构](docs/architecture.md) — 模块划分、线程模型、数据流
- [HTTP API](docs/api.md) — REST + WebSocket 全量端点与示例
- [MQTT 协议](docs/mqtt-protocol.md) — 主题约定、报文格式、遗嘱消息
- [AI 助手](docs/ai-assistant.md) — 两种模式、Prompt 设计、安全边界
- [Modbus 接入](docs/modbus.md) — 工业设备（PLC/电表）寄存器映射与轮询
- [二次开发](CONTRIBUTING.md)

## 硬件接入

`firmware/esp32/` 提供 Arduino 示例：

- `liniot_device_http` — ESP32 通过 HTTP 上报 + 轮询命令（无需 Broker）
- `liniot_device_mqtt` — ESP32 通过 MQTT 上报 + 订阅命令

其他任意能发 HTTP/MQTT 的设备（树莓派、现成传感器模块）按 [MQTT 协议](docs/mqtt-protocol.md) 三行主题约定即可接入。

## 测试

```bash
pip install -e ".[dev]"
pytest -v          # 57 passed
```

## Docker

```bash
docker compose up -d    # 同时启动 mosquitto + liniot，MQTT 通道自动接通
```

## Roadmap

见 [ROADMAP.md](ROADMAP.md)——其中标注 *good first issue* 的任务适合首次贡献。

## 参与贡献

欢迎任何人参与，Issue / PR 均可：

- **第一次贡献？** 看[你的第一个 PR：十分钟上手指南](docs/first-pr.md)——无需硬件，照着命令走即可；
- **想上手写代码**：从 [ROADMAP](ROADMAP.md) 挑一个 *good first issue*，按 [CONTRIBUTING](CONTRIBUTING.md) 的流程提交 PR；
- **想提需求/报 Bug**：使用 [Issue 模板](.github/ISSUE_TEMPLATE)；
- 所有参与者请遵守[行为准则](CODE_OF_CONDUCT.md)。

## License

[MIT](LICENSE)

---

<a name="english"></a>

# LinIoT (English)

A lightweight open-source AIoT platform: device connectivity over MQTT/HTTP, time-series storage, threshold rules with alerting, a real-time dashboard, and an LLM assistant that queries and controls devices in natural language.

<p align="center">
  <img src="docs/images/dashboard.png" alt="LinIoT dashboard" width="820">
</p>

## Features

- **Connectivity** — MQTT (any standard broker, offline detection via LWT) and HTTP channels with identical semantics
- **Storage** — single-file SQLite with WAL concurrency and per-device ring retention
- **Rules** — threshold rules with rising-edge deduplication (per rule × device), global rules, webhook notifications
- **Dashboard** — zero-build vanilla JS, WebSocket push with polling fallback, live charts, property control, rule management, alert center
- **AI assistant** — natural-language device queries and control; works with any OpenAI-compatible API (OpenAI / DeepSeek / Ollama / vLLM…); **alerts get automatic LLM diagnosis** (root cause + suggested action); falls back to a local rule-based intent engine without a key; AI actions are whitelisted and fully audited
- **Anomaly detection** — EWMA baseline with 3σ decision, catches metric spikes without any rule configuration
- **Scene automation** — "when temperature > 28 then turn on the light" with cooldown anti-oscillation
- **Simulator** — full end-to-end demo without any hardware; ESP32 firmware examples included

## Quick start

```bash
pip install -e .
liniot run          # platform at http://127.0.0.1:8000
liniot simulate     # in another terminal: 2 sensors + 2 smart lights
```

Optional LLM support:

```bash
export LINIOT_AI_API_KEY=sk-xxx
export LINIOT_AI_MODEL=gpt-4o-mini
```

Optional MQTT:

```bash
export LINIOT_MQTT_HOST=broker.emqx.io
```

> **Security note**: set `LINIOT_AUTH_TOKEN` to require an access token on every API call and the WebSocket (the dashboard shows a login box). Without it the platform is open by design — keep it inside a trusted LAN or restrict access via firewall / reverse proxy for production.

See the Chinese section above for full configuration, API docs, MQTT protocol, and hardware guides — code comments and API responses are largely language-neutral, and doc files are being translated progressively.

## Testing

```bash
pip install -e ".[dev]" && pytest -v
```

## License

[MIT](LICENSE)
