"""隐私视角：按角色裁剪个人信息。

策略矩阵
--------
* admin        可见全部字段（管理端）。
* grid_worker  可见车主姓名，隐藏车主电话/证件号、上报人电话。
* owner        仅能查看本人事件；隐藏其他上报人身份（举报保护），
               可见证据内容与处置依据，便于行使申诉权。
"""

from __future__ import annotations

from typing import Optional

SENSITIVE_OWNER = ("owner_phone", "owner_id_no")
REPORTER_FIELDS = ("reporter_id", "reporter_name", "reporter_phone")


def mask_phone(value: Optional[str]) -> Optional[str]:
    if not value:
        return value
    v = str(value)
    if len(v) >= 7:
        return v[:3] + "*" * (len(v) - 5) + v[-2:]
    return "*" * len(v)


def mask_id_no(value: Optional[str]) -> Optional[str]:
    if not value:
        return value
    v = str(value)
    if len(v) >= 6:
        return v[:3] + "*" * (len(v) - 5) + v[-2:]
    return "**"


def _scrub_owner(obj: dict, role: str) -> dict:
    if role == "admin":
        return obj
    if role == "grid_worker":
        for f in SENSITIVE_OWNER:
            if obj.get(f):
                obj[f] = "***"
        return obj
    # owner 视角：本人信息可见
    return obj


def _scrub_reporter(obj: dict, role: str) -> dict:
    if role == "admin":
        return obj
    if role == "grid_worker":
        if obj.get("reporter_phone"):
            obj["reporter_phone"] = mask_phone(obj["reporter_phone"])
        return obj
    # owner：隐藏其他上报人的身份
    for f in REPORTER_FIELDS:
        obj.pop(f, None)
    return obj


def report_view(report: dict, role: str) -> dict:
    d = dict(report)
    d.pop("payload_json", None)
    return _scrub_reporter(d, role)


def evidence_view(ev: dict, role: str) -> dict:
    d = dict(ev)
    d.pop("report_id", None)
    return _scrub_reporter(d, role)


def disposal_view(d: dict, role: str) -> dict:
    # 处置记录不含举报型 PII，三种角色均可见其依据与经办人
    return dict(d)


def appeal_view(a: dict, role: str, *, is_owner: bool = False) -> dict:
    d = dict(a)
    if role == "owner" and not is_owner:
        d.pop("contact", None)
    if role == "grid_worker":
        if d.get("contact"):
            d["contact"] = mask_phone(d["contact"])
    return d


def chain_view(item: dict, role: str) -> dict:
    d = dict(item)
    detail = d.get("detail") or {}
    if role != "admin":
        for f in REPORTER_FIELDS:
            detail.pop(f, None)
        if role == "owner":
            # 车主视角不暴露内部经办人之外的上报人
            for f in ("owner_phone", "owner_id_no"):
                detail.pop(f, None)
    d["detail"] = detail
    return d


def incident_view(inc: dict, role: str) -> dict:
    d = dict(inc)
    return _scrub_owner(d, role)


def incident_detail(inc: dict, *, reports, evidences, disposals, appeals,
                    chain, role: str, is_owner: bool = False) -> dict:
    view = incident_view(inc, role)
    view["reports"] = [report_view(r, role) for r in reports]
    view["evidences"] = [evidence_view(e, role) for e in evidences]
    view["disposals"] = [disposal_view(d, role) for d in disposals]
    view["appeals"] = [appeal_view(a, role, is_owner=is_owner) for a in appeals]
    view["chain"] = [chain_view(c, role) for c in chain]
    return view
