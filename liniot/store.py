"""SQLite 存储层：设备、遥测、规则、告警、命令队列。

设计要点：
- 单连接 + 线程锁（FastAPI 线程池与 MQTT 线程共享），WAL 模式提升并发读写；
- 遥测表按 (device_id, metric, ts) 建索引，按设备做环形保留（retention）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    type       TEXT NOT NULL DEFAULT 'generic',
    meta       TEXT NOT NULL DEFAULT '{}',
    properties TEXT NOT NULL DEFAULT '{}',
    status     TEXT NOT NULL DEFAULT 'offline',
    last_seen  REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS telemetry (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    metric    TEXT NOT NULL,
    value     REAL NOT NULL,
    ts        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tel_dev_metric_ts ON telemetry(device_id, metric, ts);
CREATE TABLE IF NOT EXISTS rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT,
    metric     TEXT NOT NULL,
    op         TEXT NOT NULL,
    threshold  REAL NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    note       TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id   INTEGER,
    device_id TEXT,
    message   TEXT NOT NULL,
    level     TEXT NOT NULL DEFAULT 'warning',
    ts        REAL NOT NULL,
    diagnosis TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);
CREATE TABLE IF NOT EXISTS scenes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    enabled          INTEGER NOT NULL DEFAULT 1,
    source_device_id TEXT,
    metric           TEXT NOT NULL,
    op               TEXT NOT NULL,
    threshold        REAL NOT NULL,
    action_device_id TEXT NOT NULL,
    action_property  TEXT NOT NULL,
    action_value     TEXT NOT NULL,
    cooldown         INTEGER NOT NULL DEFAULT 60,
    last_fired       REAL NOT NULL DEFAULT 0,
    created_at       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS commands (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_at  REAL NOT NULL,
    delivered_at REAL
);
"""


def _row_to_device(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "type": row["type"],
        "meta": json.loads(row["meta"]),
        "properties": json.loads(row["properties"]),
        "status": row["status"],
        "last_seen": row["last_seen"],
        "created_at": row["created_at"],
    }


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._inserts_since_prune = 0
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            # 旧库迁移：v0.1 的 alerts 表没有 diagnosis 列
            columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(alerts)")}
            if "diagnosis" not in columns:
                self.conn.execute("ALTER TABLE alerts ADD COLUMN diagnosis TEXT")
            self.conn.commit()

    # ------------------------------------------------------------------ 设备

    def upsert_device(self, device_id: str, name: str, type_: str,
                      meta: dict[str, str]) -> tuple[dict[str, Any], bool]:
        """注册设备；id 已存在时更新名称/类型/元数据并返回 (设备, created=False)。"""
        now = time.time()
        with self.lock:
            row = self.conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO devices(id,name,type,meta,properties,status,last_seen,created_at)"
                    " VALUES(?,?,?,?,?,'offline',0,?)",
                    (device_id, name, type_, json.dumps(meta, ensure_ascii=False), "{}", now),
                )
                self.conn.commit()
                created = True
            else:
                self.conn.execute(
                    "UPDATE devices SET name=?, type=?, meta=? WHERE id=?",
                    (name, type_, json.dumps(meta, ensure_ascii=False), device_id),
                )
                self.conn.commit()
                created = False
            return self.get_device(device_id), created  # type: ignore[return-value]

    def get_device(self, device_id: str) -> Optional[dict[str, Any]]:
        with self.lock:
            row = self.conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        return _row_to_device(row) if row else None

    def list_devices(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM devices ORDER BY created_at").fetchall()
        return [_row_to_device(r) for r in rows]

    def delete_device(self, device_id: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM devices WHERE id=?", (device_id,))
            self.conn.execute("DELETE FROM telemetry WHERE device_id=?", (device_id,))
            self.conn.execute("DELETE FROM rules WHERE device_id=?", (device_id,))
            self.conn.execute("DELETE FROM commands WHERE device_id=?", (device_id,))
            self.conn.commit()

    def set_status(self, device_id: str, status: str,
                   properties_patch: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
        with self.lock:
            row = self.conn.execute("SELECT properties FROM devices WHERE id=?", (device_id,)).fetchone()
            if row is None:
                return None
            props = json.loads(row["properties"])
            if properties_patch:
                props.update(properties_patch)
            self.conn.execute(
                "UPDATE devices SET status=?, properties=?, last_seen=? WHERE id=?",
                (status, json.dumps(props, ensure_ascii=False), time.time(), device_id),
            )
            self.conn.commit()
        return self.get_device(device_id)

    def touch(self, device_id: str) -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE devices SET last_seen=?, status='online' WHERE id=?",
                (time.time(), device_id),
            )
            self.conn.commit()

    # ------------------------------------------------------------------ 遥测

    def insert_telemetry(self, device_id: str, points: list[dict[str, Any]]) -> int:
        now = time.time()
        rows = [(device_id, p["metric"], float(p["value"]), float(p.get("ts") or now))
                for p in points]
        with self.lock:
            self.conn.executemany(
                "INSERT INTO telemetry(device_id,metric,value,ts) VALUES(?,?,?,?)", rows
            )
            self.conn.commit()
            self._inserts_since_prune += len(rows)
            if self._inserts_since_prune >= 200:
                self._inserts_since_prune = 0
                self._prune()
        return len(rows)

    def _prune(self) -> None:
        self.conn.execute(
            "DELETE FROM telemetry WHERE id IN ("
            "  SELECT id FROM ("
            "    SELECT id, ROW_NUMBER() OVER (PARTITION BY device_id ORDER BY id DESC) AS rn"
            "    FROM telemetry) WHERE rn > 20000)"
        )
        self.conn.commit()

    def prune(self, retention: int) -> None:
        """按设备保留最近 retention 条记录（供测试与运维显式调用）。"""
        with self.lock:
            self.conn.execute(
                "DELETE FROM telemetry WHERE id IN ("
                "  SELECT id FROM ("
                "    SELECT id, ROW_NUMBER() OVER (PARTITION BY device_id ORDER BY id DESC) AS rn"
                "    FROM telemetry) WHERE rn > ?)",
                (retention,),
            )
            self.conn.commit()

    def query_telemetry(self, device_id: str, metric: Optional[str] = None,
                        limit: int = 200) -> list[dict[str, Any]]:
        """按时间升序返回遥测序列，供图表绘制。"""
        sql = "SELECT metric,value,ts FROM telemetry WHERE device_id=?"
        params: list[Any] = [device_id]
        if metric:
            sql += " AND metric=?"
            params.append(metric)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(limit)
        with self.lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [{"metric": r["metric"], "value": r["value"], "ts": r["ts"]} for r in reversed(rows)]

    def iter_telemetry(self, device_id: str, metric: Optional[str] = None,
                       limit: Optional[int] = None,
                       batch: int = 1000) -> Iterator[dict[str, Any]]:
        """流式迭代遥测：keyset 分页逐批查询，供 CSV 导出等大结果集场景。

        与 query_telemetry 一次性取全量不同，这里每批查完立即释放锁再产出，
        导出 10 万行也不会把结果集整个拉进内存。limit 给定时只导出最近 limit 条
        （通过 OFFSET 定位起点，仍按 id 升序产出）。
        """
        last_id = 0
        if limit is not None and limit > 0:
            sql = "SELECT id FROM telemetry WHERE device_id=?"
            params: list[Any] = [device_id]
            if metric:
                sql += " AND metric=?"
                params.append(metric)
            sql += " ORDER BY id DESC LIMIT 1 OFFSET ?"
            params.append(limit - 1)
            with self.lock:
                row = self.conn.execute(sql, params).fetchone()
            if row is not None:
                last_id = row["id"] - 1  # 其后恰为"最近 limit 条"
        while True:
            sql = "SELECT id,metric,value,ts FROM telemetry WHERE device_id=? AND id>?"
            params = [device_id, last_id]
            if metric:
                sql += " AND metric=?"
                params.append(metric)
            sql += " ORDER BY id LIMIT ?"
            params.append(batch)
            with self.lock:
                rows = self.conn.execute(sql, params).fetchall()
            if not rows:
                return
            for r in rows:
                yield {"metric": r["metric"], "value": r["value"], "ts": r["ts"]}
            last_id = rows[-1]["id"]

    def latest_metrics(self) -> dict[tuple[str, str], dict[str, float]]:
        """每个 (device, metric) 的最新值，供 AI 快照与概览使用。"""
        with self.lock:
            rows = self.conn.execute(
                "SELECT device_id, metric, value, ts FROM telemetry"
                " WHERE id IN (SELECT MAX(id) FROM telemetry GROUP BY device_id, metric)"
            ).fetchall()
        return {(r["device_id"], r["metric"]): {"value": r["value"], "ts": r["ts"]} for r in rows}

    def metrics_of(self, device_id: str) -> list[str]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT DISTINCT metric FROM telemetry WHERE device_id=? ORDER BY metric",
                (device_id,),
            ).fetchall()
        return [r["metric"] for r in rows]

    def count_telemetry(self) -> int:
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0]

    # ------------------------------------------------------------------ 规则

    def create_rule(self, device_id: Optional[str], metric: str, op: str,
                    threshold: float, note: str = "", enabled: bool = True) -> dict[str, Any]:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO rules(device_id,metric,op,threshold,enabled,note,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (device_id, metric, op, threshold, int(enabled), note, time.time()),
            )
            self.conn.commit()
            return self.get_rule(cur.lastrowid)  # type: ignore[return-value]

    def get_rule(self, rule_id: int) -> Optional[dict[str, Any]]:
        with self.lock:
            row = self.conn.execute("SELECT * FROM rules WHERE id=?", (rule_id,)).fetchone()
        return dict(row) if row else None

    def list_rules(self, only_enabled: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM rules" + (" WHERE enabled=1" if only_enabled else "") + " ORDER BY id"
        with self.lock:
            rows = self.conn.execute(sql).fetchall()
        return [dict(r) for r in rows]

    def update_rule(self, rule_id: int, **changes: Any) -> Optional[dict[str, Any]]:
        allowed = {"enabled", "note", "threshold"}
        sets, params = [], []
        for key, value in changes.items():
            if key in allowed and value is not None:
                sets.append(f"{key}=?")
                params.append(value if not isinstance(value, bool) else int(value))
        if not sets:
            return self.get_rule(rule_id)
        with self.lock:
            self.conn.execute(f"UPDATE rules SET {', '.join(sets)} WHERE id=?", (*params, rule_id))
            self.conn.commit()
        return self.get_rule(rule_id)

    def delete_rule(self, rule_id: int) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM rules WHERE id=?", (rule_id,))
            self.conn.commit()

    # ------------------------------------------------------------------ 告警

    def insert_alert(self, rule_id: Optional[int], device_id: Optional[str],
                     message: str, level: str = "warning") -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO alerts(rule_id,device_id,message,level,ts) VALUES(?,?,?,?,?)",
                (rule_id, device_id, message, level, time.time()),
            )
            self.conn.commit()
        return int(cur.lastrowid)

    def list_alerts(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def set_alert_diagnosis(self, alert_id: int, diagnosis: str) -> None:
        with self.lock:
            self.conn.execute("UPDATE alerts SET diagnosis=? WHERE id=?",
                              (diagnosis, alert_id))
            self.conn.commit()

    # ------------------------------------------------------------------ 场景

    def create_scene(self, name: str, source_device_id: Optional[str], metric: str,
                     op: str, threshold: float, action_device_id: str,
                     action_property: str, action_value: str,
                     cooldown: int = 60) -> dict[str, Any]:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO scenes(name,enabled,source_device_id,metric,op,threshold,"
                "action_device_id,action_property,action_value,cooldown,created_at)"
                " VALUES(?,1,?,?,?,?,?,?,?,?,?)",
                (name, source_device_id, metric, op, threshold,
                 action_device_id, action_property, action_value, cooldown, time.time()),
            )
            self.conn.commit()
            return self.get_scene(cur.lastrowid)  # type: ignore[return-value]

    def get_scene(self, scene_id: int) -> Optional[dict[str, Any]]:
        with self.lock:
            row = self.conn.execute("SELECT * FROM scenes WHERE id=?", (scene_id,)).fetchone()
        return dict(row) if row else None

    def list_scenes(self, only_enabled: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM scenes" + (" WHERE enabled=1" if only_enabled else "") + " ORDER BY id"
        with self.lock:
            rows = self.conn.execute(sql).fetchall()
        return [dict(r) for r in rows]

    def update_scene(self, scene_id: int, **changes: Any) -> Optional[dict[str, Any]]:
        allowed = {"enabled", "name", "threshold", "cooldown"}
        sets, params = [], []
        for key, value in changes.items():
            if key in allowed and value is not None:
                sets.append(f"{key}=?")
                params.append(value if not isinstance(value, bool) else int(value))
        if not sets:
            return self.get_scene(scene_id)
        with self.lock:
            self.conn.execute(f"UPDATE scenes SET {', '.join(sets)} WHERE id=?",
                              (*params, scene_id))
            self.conn.commit()
        return self.get_scene(scene_id)

    def delete_scene(self, scene_id: int) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM scenes WHERE id=?", (scene_id,))
            self.conn.commit()

    def touch_scene(self, scene_id: int, ts: float) -> None:
        with self.lock:
            self.conn.execute("UPDATE scenes SET last_fired=? WHERE id=?", (ts, scene_id))
            self.conn.commit()

    def count_alerts_since(self, since: float) -> int:
        with self.lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM alerts WHERE ts>=?", (since,)
            ).fetchone()[0]

    # ------------------------------------------------------------------ 命令

    def queue_command(self, device_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO commands(device_id,payload,created_at) VALUES(?,?,?)",
                (device_id, json.dumps(payload, ensure_ascii=False), now),
            )
            self.conn.commit()
            return {"id": cur.lastrowid, "device_id": device_id,
                    "payload": payload, "created_at": now, "delivered_at": None}

    def pending_commands(self, device_id: str) -> list[dict[str, Any]]:
        """取走待下发命令并标记为已下发（HTTP 轮询通道）。"""
        now = time.time()
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM commands WHERE device_id=? AND delivered_at IS NULL ORDER BY id",
                (device_id,),
            ).fetchall()
            if rows:
                self.conn.execute(
                    "UPDATE commands SET delivered_at=? WHERE id IN (%s)"
                    % ",".join("?" * len(rows)),
                    (now, *(r["id"] for r in rows)),
                )
                self.conn.commit()
        return [{"id": r["id"], "payload": json.loads(r["payload"]),
                 "created_at": r["created_at"]} for r in rows]
