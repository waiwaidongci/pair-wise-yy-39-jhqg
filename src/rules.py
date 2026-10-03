from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='大坝巡检、缺陷与应急管理'; ENTITY='大坝缺陷'; ID_PREFIX='DS'
SEVERITIES=['observation', 'minor', 'major', 'emergency']; STATES=['planned', 'inspected', 'defect_confirmed', 'repair', 'verified', 'closed']; TRANSITIONS={'planned': ['inspected'], 'inspected': ['defect_confirmed'], 'defect_confirmed': ['repair'], 'repair': ['verified'], 'verified': ['closed'], 'closed': []}; TRANSITION_ROLES={'inspected': ['inspector'], 'defect_confirmed': ['dam_engineer'], 'repair': ['dam_engineer'], 'verified': ['inspector'], 'closed': ['emergency_manager']}
CREATE_ROLES=set(['inspector']); RECORD_ROLES=set(['inspector', 'dam_engineer']); AUDIT_ROLES=set(['emergency_manager', 'viewer']); VIEW_ROLES=set(['inspector', 'dam_engineer', 'emergency_manager', 'viewer'])
SEVERITY_WEIGHT={'observation': 1.0, 'minor': 3.0, 'major': 6.0, 'emergency': 9.0}; DEADLINE_HOURS={'observation': 72, 'minor': 24, 'major': 8, 'emergency': 4}; TERMINAL_STATES=set(['closed'])
# 派工（调度）状态机：排队 -> 已派工 -> 已完成；已派工可释放后按原派工号重试
DISPATCH_STATUSES=['queued', 'dispatched', 'released', 'completed']
DISPATCH_ROLES=set(['emergency_manager', 'dam_engineer'])
TEAM_MANAGE_ROLES=set(['emergency_manager'])
MATERIAL_MANAGE_ROLES=set(['emergency_manager', 'dam_engineer'])
# 出库回执状态：待处理 -> 已对账；实发不足 -> 短少；写入失败 -> 写入失败
RECEIPT_STATUSES=['pending', 'reconciled', 'short', 'write_failed']
RESERVATION_STATUSES=['held', 'released']; ASSIGNMENT_STATUSES=['assigned', 'released']
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
def team_gap(required, available):
    """班组容量缺口：返回缺口描述或None。"""
    required=float(required); available=float(available)
    if available+1e-9 >= required: return None
    short=required-available
    return {"required": required, "available": max(0.0, available), "short": short}
def material_gap(required, available):
    """物资库存缺口：返回缺口描述或None。"""
    required=float(required); available=float(available)
    if available+1e-9 >= required: return None
    short=required-available
    return {"required": required, "available": max(0.0, available), "short": short}
def has_gap(gap):
    return bool(gap) and (gap.get("team") is not None or len(gap.get("materials", [])) > 0)
