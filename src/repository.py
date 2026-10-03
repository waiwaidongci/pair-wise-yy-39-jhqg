from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import DISPATCH_PREFIX, ID_PREFIX, STATES


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
                CREATE TABLE IF NOT EXISTS crews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    capacity INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS materials (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    unit TEXT NOT NULL,
                    stock REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS dispatches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dispatch_no TEXT NOT NULL UNIQUE,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    crew_id INTEGER NOT NULL REFERENCES crews(id),
                    status TEXT NOT NULL DEFAULT 'queued'
                        CHECK(status IN ('queued','dispatched','receipted')),
                    attempt INTEGER NOT NULL DEFAULT 0,
                    gaps TEXT,
                    idempotency_key TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_dispatch_active_item
                    ON dispatches(item_id) WHERE status IN ('queued','dispatched');
                CREATE UNIQUE INDEX IF NOT EXISTS ux_dispatch_idem
                    ON dispatches(idempotency_key) WHERE idempotency_key IS NOT NULL;
                CREATE TABLE IF NOT EXISTS dispatch_materials (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dispatch_id INTEGER NOT NULL REFERENCES dispatches(id) ON DELETE CASCADE,
                    material_id INTEGER NOT NULL REFERENCES materials(id),
                    request_qty REAL NOT NULL,
                    issued_qty REAL NOT NULL DEFAULT 0,
                    UNIQUE(dispatch_id, material_id)
                );
                CREATE TABLE IF NOT EXISTS crew_reservations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dispatch_id INTEGER NOT NULL REFERENCES dispatches(id) ON DELETE CASCADE,
                    crew_id INTEGER NOT NULL REFERENCES crews(id),
                    slots INTEGER NOT NULL,
                    UNIQUE(dispatch_id, crew_id)
                );
                CREATE TABLE IF NOT EXISTS material_reservations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dispatch_id INTEGER NOT NULL REFERENCES dispatches(id) ON DELETE CASCADE,
                    material_id INTEGER NOT NULL REFERENCES materials(id),
                    qty REAL NOT NULL,
                    UNIQUE(dispatch_id, material_id)
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_no TEXT NOT NULL UNIQUE,
                    dispatch_id INTEGER NOT NULL REFERENCES dispatches(id),
                    result TEXT NOT NULL CHECK(result IN ('full','short')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receipt_lines (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_id INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
                    material_id INTEGER NOT NULL REFERENCES materials(id),
                    issued_qty REAL NOT NULL
                );
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

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

    # ---- 应急班组、堵漏物资与派工调度 ----

    def create_crew(self, name: str, capacity: int) -> Dict[str, Any]:
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    "INSERT INTO crews(name, capacity) VALUES(?,?)", (name, capacity))
                crew_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("班组名称已存在") from exc
        return self.get_crew(crew_id)

    def get_crew(self, crew_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM crews WHERE id=?", (crew_id,)).fetchone()
        if row is None:
            raise NotFoundError("应急班组不存在")
        crew = dict(row)
        crew["available_slots"] = self.crew_available(crew_id)
        return crew

    def crew_available(self, crew_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                """SELECT c.capacity - COALESCE((
                       SELECT SUM(cr.slots) FROM crew_reservations cr
                       JOIN dispatches d ON d.id=cr.dispatch_id
                       WHERE cr.crew_id=c.id AND d.status='dispatched'),0) AS free
                   FROM crews c WHERE c.id=?""", (crew_id,)).fetchone()
        if row is None:
            raise NotFoundError("应急班组不存在")
        return int(row["free"])
    def list_crews(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM crews ORDER BY id").fetchall()
        result = []
        for row in rows:
            crew = dict(row)
            crew["available_slots"] = int(row["capacity"]) - int(self.conn.execute(
                """SELECT COALESCE(SUM(cr.slots),0) FROM crew_reservations cr
                   JOIN dispatches d ON d.id=cr.dispatch_id
                   WHERE cr.crew_id=? AND d.status='dispatched'""", (row["id"],)).fetchone()[0])
            result.append(crew)
        return result

    def create_material(self, name: str, unit: str, stock: float) -> Dict[str, Any]:
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    "INSERT INTO materials(name, unit, stock) VALUES(?,?,?)",
                    (name, unit, stock))
                material_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("物资名称已存在") from exc
        return self.get_material(material_id)

    def get_material(self, material_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
        if row is None:
            raise NotFoundError("物资不存在")
        material = dict(row)
        material["available_stock"] = self.material_available(material_id)
        return material

    def material_available(self, material_id: int) -> float:
        with self._lock:
            row = self.conn.execute(
                """SELECT m.stock - COALESCE((
                       SELECT SUM(mr.qty) FROM material_reservations mr
                       JOIN dispatches d ON d.id=mr.dispatch_id
                       WHERE mr.material_id=m.id AND d.status='dispatched'),0) AS free
                   FROM materials m WHERE m.id=?""", (material_id,)).fetchone()
        if row is None:
            raise NotFoundError("物资不存在")
        return float(row["free"])

    def list_materials(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM materials ORDER BY id").fetchall()
        result = []
        for row in rows:
            material = dict(row)
            reserved = float(self.conn.execute(
                """SELECT COALESCE(SUM(mr.qty),0) FROM material_reservations mr
                   JOIN dispatches d ON d.id=mr.dispatch_id
                   WHERE mr.material_id=? AND d.status='dispatched'""", (row["id"],)).fetchone()[0])
            material["available_stock"] = float(row["stock"]) - reserved
            result.append(material)
        return result

    def inbound_material(self, material_id: int, qty: float) -> Dict[str, Any]:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE materials SET stock=stock+? WHERE id=?", (qty, material_id))
            if cur.rowcount == 0:
                raise NotFoundError("物资不存在")
        return self.get_material(material_id)

    def insert_queued_dispatch(self, item_id: int, crew_id: int, lines: List[Dict[str, Any]],
                               idempotency_key: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO dispatches(dispatch_no, item_id, crew_id, status, attempt,
                       gaps, idempotency_key, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    ("", item_id, crew_id, "queued", 0, None, idempotency_key, actor, now, now))
                dispatch_id = int(cur.lastrowid)
                dispatch_no = f"{DISPATCH_PREFIX}-{dispatch_id:06d}"
                self.conn.execute(
                    "UPDATE dispatches SET dispatch_no=? WHERE id=?", (dispatch_no, dispatch_id))
                self.conn.executemany(
                    """INSERT INTO dispatch_materials(dispatch_id, material_id, request_qty)
                       VALUES(?,?,?)""",
                    [(dispatch_id, line["material_id"], line["request_qty"]) for line in lines])
        except sqlite3.IntegrityError as exc:
            active = self.conn.execute(
                """SELECT 1 FROM dispatches WHERE item_id=?
                   AND status IN ('queued','dispatched')""", (item_id,)).fetchone()
            if active is not None:
                raise ConflictError("该缺陷已有进行中的派工") from exc
            if idempotency_key and self.conn.execute(
                    "SELECT 1 FROM dispatches WHERE idempotency_key=?",
                    (idempotency_key,)).fetchone() is not None:
                raise ConflictError("幂等键已使用") from exc
            raise ConflictError("派工写入失败") from exc
        return self.get_dispatch(dispatch_id)

    def get_dispatch(self, dispatch_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
        if row is None:
            raise NotFoundError("派工不存在")
        return dict(row)

    def get_dispatch_by_no(self, dispatch_no: str) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dispatches WHERE dispatch_no=?", (dispatch_no,)).fetchone()
        if row is None:
            raise NotFoundError("派工不存在")
        return dict(row)

    def find_dispatch_by_idem(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dispatches WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        return dict(row) if row else None

    def list_dispatches(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM dispatches"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def dispatch_material_lines(self, dispatch_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                """SELECT dm.*, m.name AS material_name, m.unit AS unit
                   FROM dispatch_materials dm JOIN materials m ON m.id=dm.material_id
                   WHERE dm.dispatch_id=? ORDER BY dm.id""", (dispatch_id,)).fetchall()
        return [dict(row) for row in rows]

    def allocate_dispatch(self, dispatch_id: int, lines: List[Dict[str, Any]]) -> Dict[str, Any]:
        """原子地预占班组与物资；容量不足时排队并记录缺口。返回派工行。"""
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
            if row is None:
                raise NotFoundError("派工不存在")
            if row["status"] != "queued":
                raise ConflictError("只有排队中的派工可以尝试预占")
            crew_id = int(row["crew_id"])
            crew = self.conn.execute(
                "SELECT * FROM crews WHERE id=?", (crew_id,)).fetchone()
            if crew is None:
                raise NotFoundError("应急班组不存在")
            used_slots = self.conn.execute(
                """SELECT COALESCE(SUM(cr.slots),0) AS n FROM crew_reservations cr
                   JOIN dispatches d ON d.id=cr.dispatch_id
                   WHERE cr.crew_id=? AND d.status='dispatched'""", (crew_id,)).fetchone()["n"]
            gaps: List[Dict[str, Any]] = []
            if int(crew["capacity"]) - int(used_slots) < 1:
                gaps.append({"type": "crew", "crew_id": crew_id,
                             "shortage": 1 - (int(crew["capacity"]) - int(used_slots))})
            for line in lines:
                mrow = self.conn.execute(
                    "SELECT * FROM materials WHERE id=?", (line["material_id"],)).fetchone()
                if mrow is None:
                    raise NotFoundError("物资不存在")
                reserved = self.conn.execute(
                    """SELECT COALESCE(SUM(mr.qty),0) AS n FROM material_reservations mr
                       JOIN dispatches d ON d.id=mr.dispatch_id
                       WHERE mr.material_id=? AND d.status='dispatched'""",
                    (line["material_id"],)).fetchone()["n"]
                free = float(mrow["stock"]) - float(reserved)
                if free + 1e-9 < line["request_qty"]:
                    gaps.append({"type": "material", "material_id": line["material_id"],
                                 "material_name": mrow["name"], "need": line["request_qty"],
                                 "available": max(0.0, free),
                                 "shortage": line["request_qty"] - max(0.0, free)})
            if gaps:
                self.conn.execute(
                    """UPDATE dispatches SET status='queued', attempt=attempt+1, gaps=?,
                       updated_at=? WHERE id=? AND status='queued'""",
                    (json.dumps(gaps, ensure_ascii=False), now, dispatch_id))
            else:
                self.conn.execute(
                    """INSERT INTO crew_reservations(dispatch_id, crew_id, slots)
                       VALUES(?,?,1)""", (dispatch_id, crew_id))
                self.conn.executemany(
                    """INSERT INTO material_reservations(dispatch_id, material_id, qty)
                       VALUES(?,?,?)""",
                    [(dispatch_id, line["material_id"], line["request_qty"]) for line in lines])
                self.conn.execute(
                    """UPDATE dispatches SET status='dispatched', attempt=attempt+1, gaps=NULL,
                       updated_at=? WHERE id=? AND status='queued'""", (now, dispatch_id))
        return self.get_dispatch(dispatch_id)

    def release_dispatch_reservations(self, dispatch_id: int) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "DELETE FROM crew_reservations WHERE dispatch_id=?", (dispatch_id,))
            self.conn.execute(
                "DELETE FROM material_reservations WHERE dispatch_id=?", (dispatch_id,))

    def get_receipt(self, receipt_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        if row is None:
            raise NotFoundError("出库回执不存在")
        receipt = dict(row)
        with self._lock:
            rows = self.conn.execute(
                """SELECT rl.material_id, m.name AS material_name, m.unit AS unit, rl.issued_qty
                   FROM receipt_lines rl JOIN materials m ON m.id=rl.material_id
                   WHERE rl.receipt_id=? ORDER BY rl.id""", (receipt_id,)).fetchall()
        receipt["lines"] = [dict(r) for r in rows]
        return receipt

    def get_receipt_by_no(self, receipt_no: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM receipts WHERE receipt_no=?", (receipt_no,)).fetchone()
        if row is None:
            return None
        return self.get_receipt(int(row["id"]))

    def accept_full_receipt(self, dispatch_id: int, receipt_no: str,
                            lines: List[Dict[str, Any]], actor: str) -> Dict[str, Any]:
        """回执逐项足量：扣减库存、核销预占、派工完成。回执与状态变更同一事务。"""
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM dispatches WHERE id=? AND status='dispatched'",
                (dispatch_id,)).fetchone()
            if row is None:
                if self.conn.execute("SELECT 1 FROM dispatches WHERE id=?", (dispatch_id,)).fetchone():
                    raise ConflictError("只有已派工的单可以登记出库回执")
                raise NotFoundError("派工不存在")
            cur = self.conn.execute(
                """INSERT INTO receipts(receipt_no, dispatch_id, result, created_by, created_at)
                   VALUES(?,?,?,?,?)""", (receipt_no, dispatch_id, "full", actor, now))
            receipt_id = int(cur.lastrowid)
            for line in lines:
                self.conn.execute(
                    """INSERT INTO receipt_lines(receipt_id, material_id, issued_qty)
                       VALUES(?,?,?)""", (receipt_id, line["material_id"], line["issued_qty"]))
                if line["issued_qty"] <= 0:
                    continue
                mr = self.conn.execute(
                    """SELECT qty FROM material_reservations
                       WHERE dispatch_id=? AND material_id=?""",
                    (dispatch_id, line["material_id"])).fetchone()
                if mr is None:
                    raise ConflictError("存在未预占的物资，无法对账")
                self.conn.execute(
                    "UPDATE materials SET stock=stock-? WHERE id=?",
                    (line["issued_qty"], line["material_id"]))
                self.conn.execute(
                    """UPDATE dispatch_materials SET issued_qty=issued_qty+?
                       WHERE dispatch_id=? AND material_id=?""",
                    (line["issued_qty"], dispatch_id, line["material_id"]))
            self.conn.execute(
                "DELETE FROM material_reservations WHERE dispatch_id=?", (dispatch_id,))
            self.conn.execute(
                "DELETE FROM crew_reservations WHERE dispatch_id=?", (dispatch_id,))
            self.conn.execute(
                """UPDATE dispatches SET status='receipted', updated_at=?
                   WHERE id=? AND status='dispatched'""", (now, dispatch_id))
        return self.get_receipt(receipt_id)

    def accept_short_receipt(self, dispatch_id: int, receipt_no: str,
                             lines: List[Dict[str, Any]], actor: str) -> Dict[str, Any]:
        """实发不足：按实发扣减库存，释放本次全部预占，派工回到排队并写明缺口。"""
        now = utc_now()
        with self._lock, self.conn:
            drow = self.conn.execute(
                "SELECT * FROM dispatches WHERE id=? AND status='dispatched'",
                (dispatch_id,)).fetchone()
            if drow is None:
                if self.conn.execute("SELECT 1 FROM dispatches WHERE id=?", (dispatch_id,)).fetchone():
                    raise ConflictError("只有已派工的单可以登记出库回执")
                raise NotFoundError("派工不存在")
            cur = self.conn.execute(
                """INSERT INTO receipts(receipt_no, dispatch_id, result, created_by, created_at)
                   VALUES(?,?,?,?,?)""", (receipt_no, dispatch_id, "short", actor, now))
            receipt_id = int(cur.lastrowid)
            for line in lines:
                self.conn.execute(
                    """INSERT INTO receipt_lines(receipt_id, material_id, issued_qty)
                       VALUES(?,?,?)""", (receipt_id, line["material_id"], line["issued_qty"]))
                if line["issued_qty"] > 0:
                    self.conn.execute(
                        "UPDATE materials SET stock=stock-? WHERE id=?",
                        (line["issued_qty"], line["material_id"]))
                self.conn.execute(
                    """UPDATE dispatch_materials SET issued_qty=issued_qty+?
                       WHERE dispatch_id=? AND material_id=?""",
                    (line["issued_qty"], dispatch_id, line["material_id"]))
            self.conn.execute(
                "DELETE FROM material_reservations WHERE dispatch_id=?", (dispatch_id,))
            self.conn.execute(
                "DELETE FROM crew_reservations WHERE dispatch_id=?", (dispatch_id,))
            gaps = self._gaps_locked(dispatch_id, crew_id=int(drow["crew_id"]))
            self.conn.execute(
                """UPDATE dispatches SET status='queued', attempt=attempt+1, gaps=?,
                   updated_at=? WHERE id=? AND status='dispatched'""",
                (json.dumps(gaps, ensure_ascii=False), now, dispatch_id))
        return self.get_receipt(receipt_id)

    def abort_dispatch_after_write_failure(self, dispatch_id: int) -> Dict[str, Any]:
        """回执写入失败的补偿：释放本次预占，派工回到排队并写明缺口（无扣库）。"""
        now = utc_now()
        with self._lock, self.conn:
            drow = self.conn.execute("SELECT * FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
            if drow is None:
                raise NotFoundError("派工不存在")
            self.conn.execute(
                "DELETE FROM material_reservations WHERE dispatch_id=?", (dispatch_id,))
            self.conn.execute(
                "DELETE FROM crew_reservations WHERE dispatch_id=?", (dispatch_id,))
            gaps = self._gaps_locked(dispatch_id, crew_id=int(drow["crew_id"]))
            self.conn.execute(
                """UPDATE dispatches SET status='queued', attempt=attempt+1, gaps=?,
                   updated_at=? WHERE id=?""",
                (json.dumps(gaps, ensure_ascii=False), now, dispatch_id))
        return self.get_dispatch(dispatch_id)

    def _gaps_locked(self, dispatch_id: int, crew_id: int) -> List[Dict[str, Any]]:
        """释放预占后，按尚需数量（申请-已发）重新计算缺口。"""
        gaps: List[Dict[str, Any]] = []
        crew = self.conn.execute("SELECT * FROM crews WHERE id=?", (crew_id,)).fetchone()
        used = self.conn.execute(
            """SELECT COALESCE(SUM(cr.slots),0) AS n FROM crew_reservations cr
               JOIN dispatches d ON d.id=cr.dispatch_id
               WHERE cr.crew_id=? AND d.status='dispatched'""", (crew_id,)).fetchone()["n"]
        free_slots = int(crew["capacity"]) - int(used)
        if free_slots < 1:
            gaps.append({"type": "crew", "crew_id": crew_id, "shortage": 1 - free_slots})
        rows = self.conn.execute(
            "SELECT * FROM dispatch_materials WHERE dispatch_id=?", (dispatch_id,)).fetchall()
        for row in rows:
            remaining = float(row["request_qty"]) - float(row["issued_qty"])
            if remaining <= 1e-9:
                continue
            mrow = self.conn.execute(
                "SELECT * FROM materials WHERE id=?", (row["material_id"],)).fetchone()
            reserved = self.conn.execute(
                """SELECT COALESCE(SUM(mr.qty),0) AS n FROM material_reservations mr
                   JOIN dispatches d ON d.id=mr.dispatch_id
                   WHERE mr.material_id=? AND d.status='dispatched'""",
                (row["material_id"],)).fetchone()["n"]
            free = float(mrow["stock"]) - float(reserved)
            if free + 1e-9 < remaining:
                gaps.append({"type": "material", "material_id": row["material_id"],
                             "material_name": mrow["name"], "need": remaining,
                             "available": max(0.0, free),
                             "shortage": remaining - max(0.0, free)})
        return gaps

    def close(self) -> None:
        with self._lock:
            self.conn.close()
