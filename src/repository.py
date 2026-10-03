from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        dispatch_statuses = ",".join(
            "'" + s.replace("'", "''") + "'" for s in
            ('queued', 'dispatched', 'released', 'completed'))
        receipt_statuses = ",".join(
            "'" + s.replace("'", "''") + "'" for s in
            ('pending', 'reconciled', 'short', 'write_failed'))
        line_statuses = ",".join(
            "'" + s.replace("'", "''") + "'" for s in
            ('pending', 'held', 'released'))
        assignment_statuses = ",".join(
            "'" + s.replace("'", "''") + "'" for s in
            ('pending', 'assigned', 'released'))
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS teams (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    capacity INTEGER NOT NULL DEFAULT 1 CHECK(capacity >= 1),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS materials (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    sku TEXT NOT NULL UNIQUE,
                    stock REAL NOT NULL DEFAULT 0 CHECK(stock >= 0),
                    unit TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dispatch_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dispatch_no TEXT NOT NULL UNIQUE,
                    item_id INTEGER NOT NULL REFERENCES items(id),
                    team_id INTEGER REFERENCES teams(id),
                    status TEXT NOT NULL CHECK(status IN ({dispatch_statuses})),
                    gap TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dispatch_lines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dispatch_no TEXT NOT NULL
                        REFERENCES dispatch_orders(dispatch_no) ON DELETE CASCADE,
                    material_id INTEGER NOT NULL REFERENCES materials(id),
                    quantity REAL NOT NULL CHECK(quantity > 0),
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ({line_statuses})),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(dispatch_no, material_id)
                );
                CREATE TABLE IF NOT EXISTS team_assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dispatch_no TEXT NOT NULL
                        REFERENCES dispatch_orders(dispatch_no) ON DELETE CASCADE,
                    team_id INTEGER NOT NULL REFERENCES teams(id),
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ({assignment_statuses})),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(dispatch_no, team_id)
                );
                CREATE TABLE IF NOT EXISTS outbound_receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_no TEXT NOT NULL UNIQUE,
                    dispatch_no TEXT NOT NULL
                        REFERENCES dispatch_orders(dispatch_no),
                    status TEXT NOT NULL CHECK(status IN ({receipt_statuses})),
                    lines TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    @staticmethod
    def _team(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    @staticmethod
    def _material(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    @staticmethod
    def _dispatch_order(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        if result.get("gap"):
            result["gap"] = json.loads(result["gap"])
        return result

    @staticmethod
    def _outbound_receipt(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["lines"] = json.loads(result["lines"])
        return result

    # ---- 应急班组 ----
    def create_team(self, name: str, capacity: int) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO teams(name, capacity, created_at) VALUES(?,?,?)",
                (name, capacity, now),
            )
            team_id = int(cur.lastrowid)
        return self.get_team(team_id)

    def get_team(self, team_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone()
        if row is None:
            raise NotFoundError("班组不存在")
        return self._team(row)

    def list_teams(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM teams ORDER BY id").fetchall()
        return [self._team(row) for row in rows]

    def count_active_assignments(self, team_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM team_assignments "
                "WHERE team_id=? AND status='assigned'",
                (team_id,),
            ).fetchone()
        return int(row["n"])

    # ---- 应急物资 ----
    def create_material(self, name: str, sku: str, stock: float,
                        unit: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    "INSERT INTO materials(name, sku, stock, unit, created_at) "
                    "VALUES(?,?,?,?,?)",
                    (name, sku, stock, unit, now),
                )
                material_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("物资编码已存在") from exc
        return self.get_material(material_id)

    def get_material(self, material_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM materials WHERE id=?", (material_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("物资不存在")
        return self._material(row)

    def list_materials(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM materials ORDER BY id").fetchall()
        return [self._material(row) for row in rows]

    def held_quantity(self, material_id: int) -> float:
        """当前已预占（held）未释放的物资数量。"""
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(quantity),0) AS n FROM dispatch_lines "
                "WHERE material_id=? AND status='held'",
                (material_id,),
            ).fetchone()
        return float(row["n"])

    def list_dispatch_lines(self, dispatch_no: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM dispatch_lines WHERE dispatch_no=? ORDER BY id",
                (dispatch_no,),
            ).fetchall()
        return [dict(row) for row in rows]

    def deduct_material_stock(self, material_id: int, quantity: float) -> None:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE materials SET stock=stock-? WHERE id=?",
                (quantity, material_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("物资不存在")

    # ---- 派工调度 ----
    def create_dispatch_order(self, dispatch_no: str, item_id: int,
                              team_id: Optional[int],
                              materials: List[tuple], actor: str) -> Dict[str, Any]:
        """原子地校验容量并预占物资/班组。

        容量检查与预占在同一把锁、同一个事务内完成，避免并发超卖。
        容量不足时单据进入 queued 并写明缺口，不做任何预占。
        """
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT 1 FROM dispatch_orders WHERE dispatch_no=?",
                (dispatch_no,),
            ).fetchone()
            if row is not None:
                raise ConflictError("派工号已存在，不能重复派工")
            item = self.conn.execute(
                "SELECT id FROM items WHERE id=?", (item_id,)
            ).fetchone()
            if item is None:
                raise NotFoundError("项目不存在")
            team = None
            if team_id is not None:
                team = self.conn.execute(
                    "SELECT * FROM teams WHERE id=?", (team_id,)
                ).fetchone()
                if team is None:
                    raise NotFoundError("班组不存在")
            mat_rows = []
            for material_id, qty in materials:
                mat = self.conn.execute(
                    "SELECT * FROM materials WHERE id=?", (material_id,)
                ).fetchone()
                if mat is None:
                    raise NotFoundError("物资不存在")
                mat_rows.append((mat, float(qty)))
            gap: Dict[str, Any] = {"team": None, "materials": []}
            if team is not None:
                active = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM team_assignments "
                    "WHERE team_id=? AND status='assigned'",
                    (team_id,),
                ).fetchone()["n"]
                if int(active) >= team["capacity"]:
                    gap["team"] = {
                        "required": 1,
                        "available": max(0, team["capacity"] - int(active)),
                        "short": 1,
                    }
            for mat, qty in mat_rows:
                held = self.conn.execute(
                    "SELECT COALESCE(SUM(quantity),0) AS n FROM dispatch_lines "
                    "WHERE material_id=? AND status='held'",
                    (mat["id"],),
                ).fetchone()["n"]
                available = float(mat["stock"]) - float(held)
                if available + 1e-9 < qty:
                    gap["materials"].append({
                        "material_id": mat["id"],
                        "required": qty,
                        "available": max(0.0, available),
                        "short": qty - available,
                    })
            has_gap = gap["team"] is not None or len(gap["materials"]) > 0
            status = "queued" if has_gap else "dispatched"
            cur = self.conn.execute(
                """INSERT INTO dispatch_orders(dispatch_no, item_id, team_id, status,
                   gap, version, created_by, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (dispatch_no, item_id, team_id, status,
                 json.dumps(gap, ensure_ascii=False) if has_gap else None,
                 1, actor, now, now),
            )
            order_id = int(cur.lastrowid)
            line_status = "pending" if has_gap else "held"
            for mat, qty in mat_rows:
                self.conn.execute(
                    """INSERT INTO dispatch_lines(dispatch_no, material_id, quantity,
                       status, created_at, updated_at) VALUES(?,?,?,?,?,?)""",
                    (dispatch_no, mat["id"], qty, line_status, now, now),
                )
            if team_id is not None:
                self.conn.execute(
                    """INSERT INTO team_assignments(dispatch_no, team_id, status,
                       created_at, updated_at) VALUES(?,?,?,?,?)""",
                    (dispatch_no, team_id,
                     "pending" if has_gap else "assigned", now, now),
                )
            row = self.conn.execute(
                "SELECT * FROM dispatch_orders WHERE id=?", (order_id,)
            ).fetchone()
            return self._dispatch_order(row)

    def get_dispatch_order(self, dispatch_no: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dispatch_orders WHERE dispatch_no=?", (dispatch_no,)
            ).fetchone()
        if row is None:
            raise NotFoundError("派工单不存在")
        return self._dispatch_order(row)

    def list_dispatch_orders(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM dispatch_orders"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._dispatch_order(row) for row in rows]

    def retry_dispatch(self, dispatch_no: str) -> Dict[str, Any]:
        """按原派工号重新校验容量并预占。

        复用已有派工单与明细行：把 pending/released 的行重新置为 held，
        已 held 的行不重复增加，因此重试不会重复占料。
        """
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM dispatch_orders WHERE dispatch_no=?", (dispatch_no,)
            ).fetchone()
            if row is None:
                raise NotFoundError("派工单不存在")
            order = self._dispatch_order(row)
            if order["status"] not in ("queued", "released"):
                raise ConflictError("当前派工状态不可重试")
            team_id = order["team_id"]
            team = None
            if team_id is not None:
                team = self.conn.execute(
                    "SELECT * FROM teams WHERE id=?", (team_id,)
                ).fetchone()
                if team is None:
                    raise NotFoundError("班组不存在")
            lines = self.conn.execute(
                "SELECT * FROM dispatch_lines WHERE dispatch_no=? ORDER BY id",
                (dispatch_no,),
            ).fetchall()
            gap: Dict[str, Any] = {"team": None, "materials": []}
            if team is not None:
                active = self.conn.execute(
                    "SELECT COUNT(*) AS n FROM team_assignments "
                    "WHERE team_id=? AND status='assigned'",
                    (team_id,),
                ).fetchone()["n"]
                if int(active) >= team["capacity"]:
                    gap["team"] = {
                        "required": 1,
                        "available": max(0, team["capacity"] - int(active)),
                        "short": 1,
                    }
            for line in lines:
                mat = self.conn.execute(
                    "SELECT * FROM materials WHERE id=?", (line["material_id"],)
                ).fetchone()
                if mat is None:
                    raise NotFoundError("物资不存在")
                held = self.conn.execute(
                    "SELECT COALESCE(SUM(quantity),0) AS n FROM dispatch_lines "
                    "WHERE material_id=? AND status='held' AND dispatch_no<>?",
                    (mat["id"], dispatch_no),
                ).fetchone()["n"]
                available = float(mat["stock"]) - float(held)
                if available + 1e-9 < float(line["quantity"]):
                    gap["materials"].append({
                        "material_id": mat["id"],
                        "required": float(line["quantity"]),
                        "available": max(0.0, available),
                        "short": float(line["quantity"]) - available,
                    })
            has_gap = gap["team"] is not None or len(gap["materials"]) > 0
            status = "queued" if has_gap else "dispatched"
            for line in lines:
                self.conn.execute(
                    "UPDATE dispatch_lines SET status=?, updated_at=? "
                    "WHERE id=?",
                    ("pending" if has_gap else "held", now, line["id"]),
                )
            if team_id is not None:
                self.conn.execute(
                    "UPDATE team_assignments SET status=?, updated_at=? "
                    "WHERE dispatch_no=?",
                    ("pending" if has_gap else "assigned", now, dispatch_no),
                )
            self.conn.execute(
                "UPDATE dispatch_orders SET status=?, gap=?, version=version+1, "
                "updated_at=? WHERE dispatch_no=?",
                (status,
                 json.dumps(gap, ensure_ascii=False) if has_gap else None,
                 now, dispatch_no),
            )
            row = self.conn.execute(
                "SELECT * FROM dispatch_orders WHERE dispatch_no=?", (dispatch_no,)
            ).fetchone()
            return self._dispatch_order(row)

    def release_dispatch(self, dispatch_no: str) -> Dict[str, Any]:
        """释放本次预占：明细行置 released、班组置 released、单据置 released。"""
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT id FROM dispatch_orders WHERE dispatch_no=?", (dispatch_no,)
            ).fetchone()
            if row is None:
                raise NotFoundError("派工单不存在")
            self.conn.execute(
                "UPDATE dispatch_lines SET status='released', updated_at=? "
                "WHERE dispatch_no=?",
                (now, dispatch_no),
            )
            self.conn.execute(
                "UPDATE team_assignments SET status='released', updated_at=? "
                "WHERE dispatch_no=?",
                (now, dispatch_no),
            )
            self.conn.execute(
                "UPDATE dispatch_orders SET status='released', version=version+1, "
                "updated_at=? WHERE dispatch_no=?",
                (now, dispatch_no),
            )
            row = self.conn.execute(
                "SELECT * FROM dispatch_orders WHERE dispatch_no=?", (dispatch_no,)
            ).fetchone()
            return self._dispatch_order(row)

    def complete_dispatch(self, dispatch_no: str) -> Dict[str, Any]:
        """出库全额对账后：扣减库存、释放预占、班组释放、单据置 completed。"""
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT id FROM dispatch_orders WHERE dispatch_no=?", (dispatch_no,)
            ).fetchone()
            if row is None:
                raise NotFoundError("派工单不存在")
            lines = self.conn.execute(
                "SELECT * FROM dispatch_lines WHERE dispatch_no=? AND status='held'",
                (dispatch_no,),
            ).fetchall()
            for line in lines:
                self.conn.execute(
                    "UPDATE materials SET stock=stock-? WHERE id=?",
                    (float(line["quantity"]), line["material_id"]),
                )
            self.conn.execute(
                "UPDATE dispatch_lines SET status='released', updated_at=? "
                "WHERE dispatch_no=?",
                (now, dispatch_no),
            )
            self.conn.execute(
                "UPDATE team_assignments SET status='released', updated_at=? "
                "WHERE dispatch_no=?",
                (now, dispatch_no),
            )
            self.conn.execute(
                "UPDATE dispatch_orders SET status='completed', version=version+1, "
                "updated_at=? WHERE dispatch_no=?",
                (now, dispatch_no),
            )
            row = self.conn.execute(
                "SELECT * FROM dispatch_orders WHERE dispatch_no=?", (dispatch_no,)
            ).fetchone()
            return self._dispatch_order(row)

    # ---- 出库回执 ----
    def create_outbound_receipt(self, receipt_no: str, dispatch_no: str,
                                lines: List[Dict[str, Any]], status: str,
                                actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO outbound_receipts(receipt_no, dispatch_no, status,
                       lines, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (receipt_no, dispatch_no, status,
                     json.dumps(lines, ensure_ascii=False), actor, now, now),
                )
                receipt_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("出库回执号已存在") from exc
        return self.get_outbound_receipt(receipt_no)

    def get_outbound_receipt(self, receipt_no: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM outbound_receipts WHERE receipt_no=?", (receipt_no,)
            ).fetchone()
        if row is None:
            raise NotFoundError("出库回执不存在")
        return self._outbound_receipt(row)

    def get_latest_outbound_receipt(self, dispatch_no: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM outbound_receipts WHERE dispatch_no=? "
                "ORDER BY id DESC LIMIT 1",
                (dispatch_no,),
            ).fetchone()
        return self._outbound_receipt(row) if row else None

    def count_outbound_receipts(self, dispatch_no: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM outbound_receipts WHERE dispatch_no=?",
                (dispatch_no,),
            ).fetchone()
        return int(row["n"])

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
