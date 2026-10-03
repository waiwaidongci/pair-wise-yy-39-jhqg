from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from .domain import (ConflictError, NotFoundError, ValidationError,
                     ensure_role, normalize_severity, require_number,
                     require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, DISPATCH_ROLES, ENTITY,
                    MATERIAL_MANAGE_ROLES, RECORD_ROLES, TEAM_MANAGE_ROLES,
                    TITLE, VIEW_ROLES, completion_blockers,
                    escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
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

    # ---- 应急班组 / 物资 ----
    def create_team(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, TEAM_MANAGE_ROLES)
        require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 100)
        capacity = int(require_number(payload.get("capacity", 1), "capacity"))
        if capacity < 1:
            raise ValidationError("capacity不能小于1")
        return self.repository.create_team(name, capacity)

    def list_teams(self, role: str) -> list:
        self._view(role)
        return self.repository.list_teams()

    def create_material(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, MATERIAL_MANAGE_ROLES)
        require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 100)
        sku = require_text(payload.get("sku"), "sku", 100)
        stock = require_number(payload.get("stock", 0), "stock")
        unit = require_text(payload.get("unit", ""), "unit", 20) if payload.get("unit") else ""
        return self.repository.create_material(name, sku, stock, unit)

    def list_materials(self, role: str) -> list:
        self._view(role)
        return self.repository.list_materials()

    # ---- 派工调度 ----
    def dispatch(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        dispatch_no = require_text(payload.get("dispatch_no"), "dispatch_no", 100)
        item_id = int(require_number(payload.get("item_id"), "item_id"))
        team_id = payload.get("team_id")
        if team_id is not None:
            team_id = int(require_number(team_id, "team_id"))
        raw_materials = payload.get("materials", [])
        if not isinstance(raw_materials, list):
            raise ValidationError("materials必须是数组")
        materials: List[tuple] = []
        for entry in raw_materials:
            if not isinstance(entry, dict):
                raise ValidationError("materials每项必须是对象")
            material_id = int(require_number(entry.get("material_id"), "material_id"))
            qty = require_number(entry.get("qty"), "qty", minimum=0.000001)
            materials.append((material_id, qty))
        # 预校验存在性（容量校验在仓储层原子完成）
        self.repository.get_item(item_id)
        if team_id is not None:
            self.repository.get_team(team_id)
        for material_id, _ in materials:
            self.repository.get_material(material_id)
        order = self.repository.create_dispatch_order(
            dispatch_no, item_id, team_id, materials, actor)
        self.repository.append_audit("dispatch", "派工单", order["id"], actor, {
            "dispatch_no": dispatch_no, "item_id": item_id, "team_id": team_id,
            "status": order["status"], "gap": order["gap"],
        })
        return self._enrich_dispatch(order)

    def get_dispatch(self, dispatch_no: str, role: str) -> Dict[str, Any]:
        self._view(role)
        return self._enrich_dispatch(self.repository.get_dispatch_order(dispatch_no))

    def list_dispatch(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self._enrich_dispatch(o)
                for o in self.repository.list_dispatch_orders(status)]

    def get_outbound_receipt(self, dispatch_no: str, role: str) -> Optional[Dict[str, Any]]:
        self._view(role)
        self.repository.get_dispatch_order(dispatch_no)
        return self.repository.get_latest_outbound_receipt(dispatch_no)

    def retry_dispatch(self, dispatch_no: str, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        order = self.repository.get_dispatch_order(dispatch_no)
        retried = self.repository.retry_dispatch(dispatch_no)
        self.repository.append_audit("retry", "派工单", order["id"], actor, {
            "dispatch_no": dispatch_no, "from": order["status"],
            "to": retried["status"], "gap": retried["gap"],
        })
        return self._enrich_dispatch(retried)

    def create_outbound_receipt(self, dispatch_no: str, payload: Dict[str, Any],
                                actor: str, role: str) -> Dict[str, Any]:
        """出库回执逐项对账；实发不足或写入失败时释放预占并按原派工号重试。"""
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        order = self.repository.get_dispatch_order(dispatch_no)
        if order["status"] != "dispatched":
            raise ConflictError("只有已派工（已预占）的单据可以出库")
        write_fail = bool(payload.get("write_fail", False))
        raw_lines = payload.get("lines", [])
        if not isinstance(raw_lines, list):
            raise ValidationError("lines必须是数组")
        shipped_map: Dict[int, float] = {}
        for entry in raw_lines:
            if not isinstance(entry, dict):
                raise ValidationError("lines每项必须是对象")
            material_id = int(require_number(entry.get("material_id"), "material_id"))
            shipped_map[material_id] = require_number(
                entry.get("shipped_qty"), "shipped_qty", minimum=0.0)
        requested = self.repository.list_dispatch_lines(dispatch_no)
        receipt_lines: List[Dict[str, Any]] = []
        has_short = False
        for line in requested:
            mat_id = int(line["material_id"])
            required = float(line["quantity"])
            shipped = float(shipped_map.get(mat_id, 0.0))
            line_status = "ok" if shipped + 1e-9 >= required else "short"
            if line_status == "short":
                has_short = True
            receipt_lines.append({
                "material_id": mat_id,
                "requested": required,
                "shipped": shipped,
                "status": line_status,
            })
        if write_fail:
            receipt_status = "write_failed"
        elif has_short:
            receipt_status = "short"
        else:
            receipt_status = "reconciled"
        receipt_no = f"R-{dispatch_no}-{uuid.uuid4().hex[:8]}"
        receipt = self.repository.create_outbound_receipt(
            receipt_no, dispatch_no, receipt_lines, receipt_status, actor)
        self.repository.append_audit("receipt", "出库回执", receipt["id"], actor, {
            "receipt_no": receipt_no, "dispatch_no": dispatch_no,
            "status": receipt_status, "lines": receipt_lines,
        })
        retried = None
        if write_fail or has_short:
            # 释放本次预占，再按原派工号重试；重试复用明细行，不重复占料
            self.repository.release_dispatch(dispatch_no)
            self.repository.append_audit("release", "派工单", order["id"], actor, {
                "dispatch_no": dispatch_no, "reason": receipt_status,
            })
            retried = self.repository.retry_dispatch(dispatch_no)
            self.repository.append_audit("retry", "派工单", order["id"], actor, {
                "dispatch_no": dispatch_no, "from": "released",
                "to": retried["status"], "gap": retried["gap"],
                "reason": receipt_status,
            })
        else:
            self.repository.complete_dispatch(dispatch_no)
            self.repository.append_audit("complete", "派工单", order["id"], actor, {
                "dispatch_no": dispatch_no, "receipt_no": receipt_no,
            })
        result = {
            "receipt": receipt,
            "dispatch": self._enrich_dispatch(
                self.repository.get_dispatch_order(dispatch_no)),
            "retried": retried is not None,
        }
        if retried is not None:
            result["retried_dispatch"] = self._enrich_dispatch(retried)
        return result

    def _enrich_dispatch(self, order: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(order)
        result["lines"] = self.repository.list_dispatch_lines(order["dispatch_no"])
        if order.get("team_id") is not None:
            try:
                result["team"] = self.repository.get_team(order["team_id"])
            except NotFoundError:
                result["team"] = None
        else:
            result["team"] = None
        return result

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
