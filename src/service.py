from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

from .domain import (ConflictError, NotFoundError, ensure_role,
                     normalize_severity, require_int, require_number,
                     require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, DISPATCH_ROLES,
                    ENTITY, RECORD_ROLES, RESOURCE_ROLES,
                    TITLE, VIEW_ROLES, completion_blockers, escalation_required,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result

    # ---- 应急班组、堵漏物资与派工调度 ----

    def create_crew(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RESOURCE_ROLES)
        require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 100)
        capacity = require_int(payload.get("capacity"), "capacity")
        crew = self.repository.create_crew(name, capacity)
        self.repository.append_audit("crew_create", "应急班组", crew["id"], actor, {
            "name": name, "capacity": capacity})
        return self.repository.get_crew(crew["id"])

    def list_crews(self, role: str) -> list:
        self._view(role)
        return self.repository.list_crews()

    def create_material(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RESOURCE_ROLES)
        require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 100)
        unit = require_text(payload.get("unit"), "unit", 20)
        stock = require_number(payload.get("stock", 0), "stock")
        material = self.repository.create_material(name, unit, stock)
        self.repository.append_audit("material_create", "堵漏物资", material["id"], actor, {
            "name": name, "unit": unit, "stock": stock})
        return self.repository.get_material(material["id"])

    def list_materials(self, role: str) -> list:
        self._view(role)
        return self.repository.list_materials()

    def inbound_material(self, material_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RESOURCE_ROLES)
        require_text(actor, "actor", 100)
        qty = require_number(payload.get("qty"), "qty", 0.000001)
        material = self.repository.inbound_material(material_id, qty)
        self.repository.append_audit("material_inbound", "堵漏物资", material_id, actor, {
            "qty": qty, "stock": material["stock"]})
        promotions = self._promote_queue()
        material = self.repository.get_material(material_id)
        material["promoted"] = promotions
        return material

    def _parse_material_lines(self, raw: Any) -> List[Dict[str, Any]]:
        if not isinstance(raw, list) or not raw:
            raise ConflictError("materials必须是非空数组")
        lines: List[Dict[str, Any]] = []
        seen = set()
        for entry in raw:
            if not isinstance(entry, dict):
                raise ConflictError("物资项必须是对象")
            material_id = require_int(entry.get("material_id"), "material_id")
            qty = require_number(entry.get("request_qty"), "request_qty", 0.000001)
            if material_id in seen:
                raise ConflictError("同一物资不能重复申请")
            seen.add(material_id)
            self.repository.get_material(material_id)
            lines.append({"material_id": material_id, "request_qty": qty})
        return lines

    @staticmethod
    def _parse_gaps(raw: Optional[str]) -> list:
        if not raw:
            return []
        try:
            gaps = json.loads(raw)
        except (TypeError, ValueError):
            return []
        return gaps if isinstance(gaps, list) else []

    def _enrich_dispatch(self, dispatch: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(dispatch)
        result["gaps"] = self._parse_gaps(dispatch.get("gaps"))
        result.pop("idempotency_key", None)
        result["materials"] = self.repository.dispatch_material_lines(dispatch["id"])
        return result

    def _try_allocate(self, dispatch_id: int, lines: List[Dict[str, Any]]) -> Dict[str, Any]:
        """按尚需数量（申请-已发）预占，避免重试重复占料。"""
        stored = self.repository.dispatch_material_lines(dispatch_id)
        by_id = {row["material_id"]: row for row in stored}
        remaining = []
        for line in lines:
            row = by_id.get(line["material_id"])
            need = float(row["request_qty"]) - float(row["issued_qty"])
            if need > 1e-9:
                remaining.append({"material_id": line["material_id"], "request_qty": need})
        return self.repository.allocate_dispatch(dispatch_id, remaining)

    def submit_dispatch(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        item_id = require_int(payload.get("item_id"), "item_id")
        crew_id = require_int(payload.get("crew_id"), "crew_id")
        lines = self._parse_material_lines(payload.get("materials"))
        self.repository.get_item(item_id)
        self.repository.get_crew(crew_id)
        idem = payload.get("idempotency_key")
        if idem is not None:
            idem = require_text(idem, "idempotency_key", 100)
            existing = self.repository.find_dispatch_by_idem(idem)
            if existing is not None:
                return self._enrich_dispatch(existing)
        dispatch = self.repository.insert_queued_dispatch(item_id, crew_id, lines, idem, actor)
        dispatch = self._try_allocate(dispatch["id"], lines)
        self.repository.append_audit("dispatch_submit", "应急派工", dispatch["id"], actor, {
            "dispatch_no": dispatch["dispatch_no"], "item_id": item_id, "crew_id": crew_id,
            "status": dispatch["status"], "gaps": self._parse_gaps(dispatch.get("gaps"))})
        return self._enrich_dispatch(dispatch)

    def retry_dispatch(self, dispatch_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        dispatch = self.repository.get_dispatch(dispatch_id)
        if dispatch["status"] != "queued":
            raise ConflictError("只有排队中的派工可以重试")
        lines = self.repository.dispatch_material_lines(dispatch_id)
        dispatch = self._try_allocate(dispatch_id, lines)
        self.repository.append_audit("dispatch_retry", "应急派工", dispatch_id, actor, {
            "dispatch_no": dispatch["dispatch_no"], "attempt": dispatch["attempt"],
            "status": dispatch["status"], "gaps": self._parse_gaps(dispatch.get("gaps"))})
        return self._enrich_dispatch(dispatch)

    def get_dispatch(self, dispatch_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self._enrich_dispatch(self.repository.get_dispatch(dispatch_id))

    def list_dispatches(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        if status is not None and status not in ("queued", "dispatched", "receipted"):
            raise ConflictError("未知派工状态")
        return [self._enrich_dispatch(d) for d in self.repository.list_dispatches(status)]

    def _promote_queue(self) -> list:
        """按提交顺序尝试让排队派工重新预占，全部成功则继续，任一仍排队即停止本轮。"""
        promoted = []
        for dispatch in self.repository.list_dispatches("queued"):
            lines = self.repository.dispatch_material_lines(dispatch["id"])
            updated = self._try_allocate(dispatch["id"], lines)
            if updated["status"] == "dispatched":
                promoted.append(updated["dispatch_no"])
                self.repository.append_audit(
                    "dispatch_promote", "应急派工", dispatch["id"], "system", {
                        "dispatch_no": updated["dispatch_no"]})
            else:
                break
        return promoted

    def post_receipt(self, dispatch_id: int, payload: Dict[str, Any],
                     actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        receipt_no = require_text(payload.get("receipt_no"), "receipt_no", 100)
        replay = self.repository.get_receipt_by_no(receipt_no)
        if replay is not None:
            if replay["dispatch_id"] != dispatch_id:
                raise ConflictError("回执号已用于其他派工")
            return {"outcome": "replayed", "dispatch_id": dispatch_id, "receipt": replay}
        raw_lines = payload.get("lines")
        if not isinstance(raw_lines, list) or not raw_lines:
            raise ConflictError("lines必须是非空数组")
        dispatch = self.repository.get_dispatch(dispatch_id)
        if dispatch["status"] != "dispatched":
            raise ConflictError("只有已派工（已预占）的单可以登记出库回执")
        stored = self.repository.dispatch_material_lines(dispatch_id)
        by_id = {row["material_id"]: row for row in stored}
        reconciled: Dict[int, Dict[str, Any]] = {}
        for entry in raw_lines:
            if not isinstance(entry, dict):
                raise ConflictError("回执项必须是对象")
            material_id = require_int(entry.get("material_id"), "material_id")
            issued = require_number(entry.get("issued_qty"), "issued_qty")
            if material_id in reconciled:
                raise ConflictError("同一物资不能重复对账")
            row = by_id.get(material_id)
            if row is None:
                raise ConflictError(f"物资{material_id}不在派工申请中")
            # 逐项对账：本次最多按待发数量发货；已发齐的物资只能以0对账
            outstanding = float(row["request_qty"]) - float(row["issued_qty"])
            if issued > outstanding + 1e-9:
                raise ConflictError(
                    f"物资{material_id}实发{issued}超过待发{outstanding}")
            reconciled[material_id] = {"material_id": material_id, "issued_qty": issued}
        missing = sorted(set(by_id) - set(reconciled))
        if missing:
            raise ConflictError(f"回执缺少物资对账项: {missing}")
        lines = [reconciled[mid] for mid in by_id]
        # 任何一项实发少于待发即为实发不足
        short = any(
            abs(line["issued_qty"] -
                (by_id[line["material_id"]]["request_qty"]
                 - by_id[line["material_id"]]["issued_qty"])) > 1e-9
            for line in lines)
        if short:
            return self._accept_short(dispatch_id, receipt_no, lines, actor)
        return self._accept_full(dispatch_id, receipt_no, lines, actor)

    def _accept_full(self, dispatch_id: int, receipt_no: str, lines: List[Dict[str, Any]],
                     actor: str) -> Dict[str, Any]:
        try:
            receipt = self.repository.accept_full_receipt(dispatch_id, receipt_no, lines, actor)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("回执号已存在或库存写入失败") from exc
        except sqlite3.DatabaseError:
            # 回执事务已整体回滚（未扣库、未核销），释放本次预占后排队重试
            return self._write_failed_compensation(dispatch_id, receipt_no, lines, actor)
        self.repository.append_audit("receipt_full", "应急派工", dispatch_id, actor, {
            "receipt_no": receipt_no, "lines": lines})
        promotions = self._promote_queue()
        return {"outcome": "full", "dispatch_id": dispatch_id, "receipt": receipt,
                "promoted": promotions}

    def _accept_short(self, dispatch_id: int, receipt_no: str, lines: List[Dict[str, Any]],
                      actor: str) -> Dict[str, Any]:
        try:
            receipt = self.repository.accept_short_receipt(dispatch_id, receipt_no, lines, actor)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("回执号已存在或库存写入失败") from exc
        except sqlite3.DatabaseError:
            return self._write_failed_compensation(dispatch_id, receipt_no, lines, actor)
        dispatch = self.repository.get_dispatch(dispatch_id)
        self.repository.append_audit("receipt_short", "应急派工", dispatch_id, actor, {
            "receipt_no": receipt_no, "lines": lines,
            "gaps": self._parse_gaps(dispatch.get("gaps")),
            "note": "已按实发扣库并释放本次预占，按原派工号重试"})
        return {"outcome": "short", "dispatch_id": dispatch_id,
                "dispatch_no": dispatch["dispatch_no"], "receipt": receipt,
                "gaps": self._parse_gaps(dispatch.get("gaps"))}

    def _write_failed_compensation(self, dispatch_id: int, receipt_no: str,
                                   lines: List[Dict[str, Any]], actor: str) -> Dict[str, Any]:
        dispatch = self.repository.abort_dispatch_after_write_failure(dispatch_id)
        self.repository.append_audit("receipt_write_failed", "应急派工", dispatch_id, actor, {
            "receipt_no": receipt_no, "lines": lines,
            "gaps": self._parse_gaps(dispatch.get("gaps")),
            "note": "回执写入失败，已释放本次预占并按原派工号排队重试"})
        return {"outcome": "write_failed", "dispatch_id": dispatch_id,
                "dispatch_no": dispatch["dispatch_no"],
                "gaps": self._parse_gaps(dispatch.get("gaps"))}
