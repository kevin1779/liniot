# Modbus TCP 接入指南

LinIoT 可通过可选的 Modbus TCP 适配器接入工业设备（PLC、电表、温控器等）。
适配器以固定间隔轮询从站寄存器，把读数转为标准遥测——存储、规则、仪表盘、
AI 助手对所有接入方式一视同仁。

## 1. 安装

pymodbus 是**可选依赖**，不影响平台其他功能：

```bash
pip install "liniot[modbus]"        # 源码安装：pip install -e ".[modbus]"
```

## 2. 注册设备

适配器不做自动注册，先通过 API/仪表盘创建设备（与 MQTT 通道一致）：

```bash
curl -X POST http://127.0.0.1:8000/api/devices \
  -H 'Content-Type: application/json' \
  -d '{"id":"sensor-mb","name":"Modbus 温控器","type":"sensor"}'
```

## 3. 配置映射（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `LINIOT_MODBUS_HOST` | 空 | 从站地址，**空 = 禁用 Modbus 通道** |
| `LINIOT_MODBUS_PORT` | `502` | 从站端口 |
| `LINIOT_MODBUS_POLL` | `10` | 轮询间隔（秒） |
| `LINIOT_MODBUS_MAP` | 空 | 寄存器映射（见下） |

映射格式（分号分隔多条）：

```
设备ID:从站地址:寄存器类型:起始地址:指标名[:系数]
```

- 寄存器类型：`holding`（保持寄存器）/ `input`（输入寄存器）/
  `coil`（线圈）/ `discrete`（离散输入）；线圈与离散输入读 0/1
- 系数：原始值 × 系数后入库，默认 1.0（如温度寄存器存 254 表示 25.4℃，
  系数填 0.1）
- 格式非法的条目启动时跳过并记 WARNING，不影响其他条目

完整示例：

```bash
export LINIOT_MODBUS_HOST=192.168.1.50
export LINIOT_MODBUS_POLL=5
export LINIOT_MODBUS_MAP="sensor-mb:1:holding:0:temperature:0.1;sensor-mb:1:input:2:humidity;switch-mb:1:coil:0:relay"
liniot run
```

## 4. 验证

本机无真实从站时，用 libmodbus 的 `diagslave` 模拟：

```bash
diagslave -m tcp -p 1502 -a 1     # 模拟 1 号从站，保持寄存器全 0
# 另一终端按上面配置启动 liniot run（HOST=127.0.0.1 PORT=1502）
```

仪表盘即出现 `sensor-mb` 的实时曲线；改用 pymodbus 内置模拟器亦可
（`pip install "pymodbus[simulator]"` 后运行 `pymodbus.simulator`）。

## 5. 行为细节

- 轮询在独立线程执行；单个条目读取失败只记 WARNING，其余条目与本轮
  其他设备不受影响，下一轮自动重试
- 设备须已注册，未注册设备的读数会被丢弃并记日志
- 断线后 `_connect` 会在下一轮重连，无需手动干预
- 适配器同样接入规则引擎与异常检测——Modbus 数据也能触发告警、场景与 AI 诊断
