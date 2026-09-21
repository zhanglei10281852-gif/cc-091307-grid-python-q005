"""SQLite 持久化层。

所有写操作在同一个连接 + 进程内锁上串行提交；
服务层需要跨多条语句原子完成时使用 ``transaction()``。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional


SCHEMA_VERSION = "1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS incidents (
    id                       TEXT PRIMARY KEY,
    subject_type             TEXT NOT NULL,
    subject_id               TEXT NOT NULL,
    location_key             TEXT NOT NULL,
    location_name            TEXT,
    lat                      REAL,
    lng                      REAL,
    owner_status             TEXT NOT NULL,
    stage                    TEXT NOT NULL,
    first_collected_at       TEXT NOT NULL,
    last_collected_at        TEXT NOT NULL,
    owner_id                 TEXT,
    owner_name               TEXT,
    owner_phone              TEXT,
    owner_id_no              TEXT,
    report_count             INTEGER NOT NULL DEFAULT 0,
    active_disposal_count    INTEGER NOT NULL DEFAULT 0,
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    closed_at                TEXT,
    version                  INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_incidents_dedupe
    ON incidents(subject_type, subject_id, location_key, last_collected_at);
CREATE INDEX IF NOT EXISTS idx_incidents_stage ON incidents(stage);

CREATE TABLE IF NOT EXISTS reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id   TEXT NOT NULL REFERENCES incidents(id),
    collected_at  TEXT NOT NULL,
    reporter_id   TEXT,
    reporter_name TEXT,
    reporter_phone TEXT,
    channel       TEXT,
    payload_json  TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_incident ON reports(incident_id, collected_at);

CREATE TABLE IF NOT EXISTS evidences (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id    TEXT NOT NULL REFERENCES incidents(id),
    report_id      INTEGER REFERENCES reports(id),
    kind           TEXT NOT NULL,
    content        TEXT,
    attachment_uri TEXT,
    source         TEXT,
    reporter_id    TEXT,
    reporter_name  TEXT,
    reporter_phone TEXT,
    collected_at   TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ev_incident ON evidences(incident_id);

CREATE TABLE IF NOT EXISTS disposals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id    TEXT NOT NULL REFERENCES incidents(id),
    kind           TEXT NOT NULL,
    basis_version  TEXT NOT NULL,
    handler_id     TEXT NOT NULL,
    handler_name   TEXT,
    decided_at     TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active',
    payload_json   TEXT,
    revoke_reason  TEXT,
    revoked_by     TEXT,
    revoked_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_disposals_incident ON disposals(incident_id);

CREATE TABLE IF NOT EXISTS appeals (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id            TEXT NOT NULL REFERENCES incidents(id),
    reason                 TEXT NOT NULL,
    filed_by               TEXT NOT NULL,
    contact                TEXT,
    filed_at               TEXT NOT NULL,
    prior_stage            TEXT NOT NULL,
    status                 TEXT NOT NULL,
    decision_basis_version TEXT,
    reviewed_by            TEXT,
    reviewed_at            TEXT,
    review_note            TEXT
);
CREATE INDEX IF NOT EXISTS idx_appeals_status ON appeals(status);
CREATE INDEX IF NOT EXISTS idx_appeals_incident ON appeals(incident_id);

CREATE TABLE IF NOT EXISTS chain (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id  TEXT NOT NULL REFERENCES incidents(id),
    seq          INTEGER NOT NULL,
    action       TEXT NOT NULL,
    actor_id     TEXT NOT NULL,
    actor_role   TEXT NOT NULL,
    actor_name   TEXT,
    at           TEXT NOT NULL,
    basis_version TEXT,
    reason       TEXT,
    detail_json  TEXT,
    UNIQUE(incident_id, seq)
);
"""


def _row_to_incident(row: sqlite3.Row) -> dict:
    d = dict(row)
    return d


def _numeric_tail_max(conn: sqlite3.Connection) -> int:
    """从事件 ID 的数字尾部恢复发号下界（ID 形如 INC-000001）。"""
    rows = conn.execute("SELECT id FROM incidents").fetchall()
    used = 0
    for (id_,) in rows:
        tail = re.findall(r"\d+", str(id_))
        if tail:
            used = max(used, int(tail[-1]))
    return used


class Repository:
    def __init__(self, path: str | Path = ":memory:"):
        self._path = str(path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self._path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.RLock()
        self._in_tx = False
        self._init_schema()
        self.reconcile()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- 事务 ---------------------------------------------------------------

    @contextmanager
    def transaction(self):
        """跨多条 SQL 的原子单元。嵌套事务退化为重入。"""
        with self._lock:
            if self._in_tx:
                yield
                return
            self._in_tx = True
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            finally:
                self._in_tx = False

    def _commit(self) -> None:
        # isolation_level=None：每条写语句隐式开启事务，需显式提交；
        # 若处于显式 transaction() 中则由其统一 COMMIT。
        if not self._in_tx:
            self._conn.commit()

    # -- 初始化与自愈 -------------------------------------------------------

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (SCHEMA_VERSION,),
                )
            for name in ("incident_seq",):
                self._conn.execute(
                    "INSERT OR IGNORE INTO counters(name, value) VALUES(?, 0)", (name,)
                )
            self._conn.commit()

    def reconcile(self) -> dict:
        """重启后依据权威明细表重算派生计数，并校正发号器。

        Returns:
            修正项摘要 ``{"report_count_fixed": n, ...}``。
        """
        fixed = {"report_count_fixed": 0, "active_disposal_fixed": 0,
                 "counter_fixed": 0}
        with self.transaction():
            cur = self._conn.execute(
                """
                UPDATE incidents
                   SET report_count = COALESCE((
                        SELECT COUNT(*) FROM reports r
                         WHERE r.incident_id = incidents.id), 0)
                 WHERE report_count != COALESCE((
                        SELECT COUNT(*) FROM reports r
                         WHERE r.incident_id = incidents.id), 0)
                """
            )
            fixed["report_count_fixed"] = cur.rowcount
            cur = self._conn.execute(
                """
                UPDATE incidents
                   SET active_disposal_count = COALESCE((
                        SELECT COUNT(*) FROM disposals d
                         WHERE d.incident_id = incidents.id
                           AND d.status='active'), 0)
                 WHERE active_disposal_count != COALESCE((
                        SELECT COUNT(*) FROM disposals d
                         WHERE d.incident_id = incidents.id
                           AND d.status='active'), 0)
                """
            )
            fixed["active_disposal_fixed"] = cur.rowcount
            # 发号器只增不减（历史数据可能被手工归档）
            used = _numeric_tail_max(self._conn)
            row = self._conn.execute(
                "SELECT value FROM counters WHERE name='incident_seq'"
            ).fetchone()
            if row is None or row[0] < used:
                self._conn.execute(
                    "INSERT INTO counters(name, value) VALUES('incident_seq', ?) "
                    "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                    (used,),
                )
                fixed["counter_fixed"] += 1
        return fixed

    # -- 发号 ---------------------------------------------------------------

    def next_incident_seq(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "UPDATE counters SET value=value+1 WHERE name='incident_seq' "
                "RETURNING value"
            ).fetchone()
            self._commit()
            return int(row[0])

    # -- 事件 ---------------------------------------------------------------

    def insert_incident(self, data: dict) -> None:
        cols = (
            "id", "subject_type", "subject_id", "location_key", "location_name",
            "lat", "lng", "owner_status", "stage", "first_collected_at",
            "last_collected_at", "owner_id", "owner_name", "owner_phone",
            "owner_id_no", "report_count", "active_disposal_count",
            "created_at", "updated_at", "closed_at", "version",
        )
        placeholders = ",".join("?" for _ in cols)
        with self._lock:
            self._conn.execute(
                f"INSERT INTO incidents({','.join(cols)}) VALUES({placeholders})",
                tuple(data.get(c) for c in cols),
            )
            self._commit()

    def get_incident(self, incident_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM incidents WHERE id=?", (incident_id,)
            ).fetchone()
            return _row_to_incident(row) if row else None

    def update_incident(self, incident_id: str, fields: dict,
                        expected_version: Optional[int] = None) -> None:
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        values: list[Any] = list(fields.values())
        sql = f"UPDATE incidents SET {sets} WHERE id=?"
        if expected_version is not None:
            sql += " AND version=?"
            values.append(expected_version)
        with self._lock:
            cur = self._conn.execute(sql, (*values, incident_id))
            if cur.rowcount == 0:
                if expected_version is not None and self.get_incident(incident_id):
                    from .models import ConflictError
                    raise ConflictError("事件版本已变化，请刷新后重试",
                                        code="version_conflict")
            self._commit()

    def latest_incident_for(self, subject_type: str, subject_id: str,
                            location_key: str) -> Optional[dict]:
        """返回同 车辆/设备 + 位置 最近的一条事件（不论阶段）。"""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM incidents
                 WHERE subject_type=? AND subject_id=? AND location_key=?
                 ORDER BY last_collected_at DESC, created_at DESC
                 LIMIT 1
                """,
                (subject_type, subject_id, location_key),
            ).fetchone()
            return _row_to_incident(row) if row else None

    def list_incidents(self, *, stage: Optional[str] = None,
                       subject_type: Optional[str] = None,
                       subject_id: Optional[str] = None,
                       limit: int = 100, offset: int = 0) -> list[dict]:
        where, params = [], []
        if stage:
            where.append("stage=?")
            params.append(stage)
        if subject_type:
            where.append("subject_type=?")
            params.append(subject_type)
        if subject_id:
            where.append("subject_id=?")
            params.append(subject_id)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM incidents{clause} "
                "ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                params,
            ).fetchall()
            return [_row_to_incident(r) for r in rows]

    # -- 上报 / 证据 --------------------------------------------------------

    def insert_report(self, incident_id: str, collected_at: str,
                      reporter: dict, channel: Optional[str],
                      payload: Optional[dict], created_at: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO reports(incident_id, collected_at, reporter_id,
                                    reporter_name, reporter_phone, channel,
                                    payload_json, created_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (incident_id, collected_at, reporter.get("id"),
                 reporter.get("name"), reporter.get("phone"), channel,
                 json.dumps(payload, ensure_ascii=False) if payload else None,
                 created_at),
            )
            self._conn.execute(
                "UPDATE incidents SET report_count=report_count+1 WHERE id=?",
                (incident_id,),
            )
            self._commit()
            return int(cur.lastrowid)

    def list_reports(self, incident_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM reports WHERE incident_id=? ORDER BY collected_at, id",
                (incident_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def insert_evidence(self, incident_id: str, data: dict,
                        created_at: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO evidences(incident_id, report_id, kind, content,
                                      attachment_uri, source, reporter_id,
                                      reporter_name, reporter_phone,
                                      collected_at, created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (incident_id, data.get("report_id"), data["kind"],
                 data.get("content"), data.get("attachment_uri"),
                 data.get("source"), data.get("reporter_id"),
                 data.get("reporter_name"), data.get("reporter_phone"),
                 data["collected_at"], created_at),
            )
            self._commit()
            return int(cur.lastrowid)

    def list_evidences(self, incident_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM evidences WHERE incident_id=? ORDER BY collected_at, id",
                (incident_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    # -- 处置 ---------------------------------------------------------------

    def insert_disposal(self, d: dict) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO disposals(incident_id, kind, basis_version, handler_id,
                                      handler_name, decided_at, status, payload_json)
                VALUES(?,?,?,?,?,?,'active',?)
                """,
                (d["incident_id"], d["kind"], d["basis_version"],
                 d["handler_id"], d.get("handler_name"), d["decided_at"],
                 json.dumps(d.get("payload") or {}, ensure_ascii=False)),
            )
            self._conn.execute(
                "UPDATE incidents SET active_disposal_count=active_disposal_count+1 "
                "WHERE id=?",
                (d["incident_id"],),
            )
            self._commit()
            return int(cur.lastrowid)

    def list_disposals(self, incident_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM disposals WHERE incident_id=? ORDER BY decided_at, id",
                (incident_id,),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["payload"] = json.loads(d.pop("payload_json") or "{}")
                out.append(d)
            return out

    def get_disposal(self, disposal_id: int) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM disposals WHERE id=?", (disposal_id,)
            ).fetchone()
            if row is None:
                return None
            d = dict(row)
            d["payload"] = json.loads(d.pop("payload_json") or "{}")
            return d

    def get_active_disposals(self, incident_id: str) -> list[dict]:
        return [d for d in self.list_disposals(incident_id) if d["status"] == "active"]

    def revoke_disposal(self, disposal_id: int, reason: str, revoked_by: str,
                        revoked_at: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM disposals WHERE id=?", (disposal_id,)
            ).fetchone()
            if row is None:
                return None
            if row["status"] != "active":
                return dict(row)
            self._conn.execute(
                "UPDATE disposals SET status='revoked', revoke_reason=?, "
                "revoked_by=?, revoked_at=? WHERE id=?",
                (reason, revoked_by, revoked_at, disposal_id),
            )
            self._conn.execute(
                "UPDATE incidents SET active_disposal_count=active_disposal_count-1 "
                "WHERE id=? AND active_disposal_count>0",
                (row["incident_id"],),
            )
            self._commit()
            return dict(self._conn.execute(
                "SELECT * FROM disposals WHERE id=?", (disposal_id,)
            ).fetchone())

    # -- 申诉 ---------------------------------------------------------------

    def insert_appeal(self, a: dict) -> int:
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO appeals(incident_id, reason, filed_by, contact,
                                    filed_at, prior_stage, status)
                VALUES(?,?,?,?,?,?,'pending')
                """,
                (a["incident_id"], a["reason"], a["filed_by"],
                 a.get("contact"), a["filed_at"], a["prior_stage"]),
            )
            self._commit()
            return int(cur.lastrowid)

    def get_pending_appeal(self, incident_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM appeals WHERE incident_id=? AND status='pending' "
                "ORDER BY id DESC LIMIT 1",
                (incident_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_appeals(self, *, status: Optional[str] = None,
                     incident_id: Optional[str] = None) -> list[dict]:
        where, params = [], []
        if status:
            where.append("status=?")
            params.append(status)
        if incident_id:
            where.append("incident_id=?")
            params.append(incident_id)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM appeals{clause} ORDER BY filed_at, id", params
            ).fetchall()
            return [dict(r) for r in rows]

    def resolve_appeal(self, appeal_id: int, status: str, basis_version: str,
                       reviewed_by: str, reviewed_at: str,
                       review_note: Optional[str]) -> Optional[dict]:
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE appeals SET status=?, decision_basis_version=?,
                       reviewed_by=?, reviewed_at=?, review_note=?
                 WHERE id=? AND status='pending'
                """,
                (status, basis_version, reviewed_by, reviewed_at,
                 review_note, appeal_id),
            )
            self._commit()
            if cur.rowcount == 0:
                return None
            row = self._conn.execute(
                "SELECT * FROM appeals WHERE id=?", (appeal_id,)
            ).fetchone()
            return dict(row)

    # -- 事件链 -------------------------------------------------------------

    def append_chain(self, incident_id: str, action: str, actor: dict,
                     at: str, *, basis_version: Optional[str] = None,
                     reason: Optional[str] = None,
                     detail: Optional[dict] = None) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS next_seq FROM chain "
                "WHERE incident_id=?",
                (incident_id,),
            ).fetchone()
            seq = int(row["next_seq"])
            self._conn.execute(
                """
                INSERT INTO chain(incident_id, seq, action, actor_id, actor_role,
                                  actor_name, at, basis_version, reason, detail_json)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (incident_id, seq, action, actor["id"], actor["role"],
                 actor.get("name"), at, basis_version, reason,
                 json.dumps(detail or {}, ensure_ascii=False)),
            )
            self._commit()
            return seq

    def list_chain(self, incident_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM chain WHERE incident_id=? ORDER BY seq",
                (incident_id,),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["detail"] = json.loads(d.pop("detail_json") or "{}")
                out.append(d)
            return out

    # -- 统计 ---------------------------------------------------------------

    def stage_counts(self) -> dict:
        with self._lock:
            rows = self._conn.execute(
                "SELECT stage, COUNT(*) FROM incidents GROUP BY stage"
            ).fetchall()
            return {r[0]: r[1] for r in rows}
