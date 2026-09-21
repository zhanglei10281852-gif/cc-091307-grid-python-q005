"""按角色脱敏的视图层：不同角色只能看到与自己相关的个人信息。"""

from __future__ import annotations

from typing import Any

from .models import Event

# 角色：reviewer 复核/管理端，grid 网格员，owner 车主，anonymous 未认证查询
STAFF_ROLES = {"reviewer", "grid"}
ALL_ROLES = STAFF_ROLES | {"owner", "anonymous"}


def mask_name(name: str) -> str:
    if not name:
        return name
    if len(name) == 1:
        return name
    return name[0] + "*" * (len(name) - 1)


def mask_contact(contact: str) -> str:
    digits = [c for c in contact if c.isdigit()]
    if len(digits) >= 7:
        tail = "".join(digits[-4:])
        return f"***{tail}"
    return "***"


def mask_plate(plate: str) -> str:
    if len(plate) <= 2:
        return plate[0] + "*" if plate else plate
    return plate[:2] + "*" * (len(plate) - 3) + plate[-1]


def _staff_view(event: Event) -> dict[str, Any]:
    """工作人员可见完整信息。"""

    return event.to_dict()


def _owner_view(event: Event) -> dict[str, Any]:
    """车主视角：隐去上报人、内部群聊来源，经办人姓名脱敏。"""

    data = event.to_dict()
    data["subject_id"] = event.subject_id and mask_plate(event.subject_id) \
        if event.subject_type == "plate" else event.subject_id
    data["owner_name"] = mask_name(event.owner_name) if event.owner_name else None
    data["owner_contact"] = (
        mask_contact(event.owner_contact) if event.owner_contact else None
    )
    data["reports"] = [
        {
            **{k: v for k, v in r.to_dict().items()
               if k not in ("reporter", "source_chat")},
            "reporter": "网格员",
        }
        for r in event.reports
    ]
    data["evidence"] = [
        {**{k: v for k, v in e.to_dict().items() if k != "source_chat"},
         "collected_by": mask_name(e.collected_by)}
        for e in event.evidence
    ]
    data["appeals"] = [
        {
            **a.to_dict(),
            "appellant": mask_name(a.appellant),
            "contact": mask_contact(a.contact),
        }
        for a in event.appeals
    ]
    data["actions"] = [
        {
            **a.to_dict(),
            "operator": mask_name(a.operator),
            # note/message 可能含内部意见，非工作人员不展示
            "detail": {k: v for k, v in a.detail.items()
                       if k not in ("note", "message")},
        }
        for a in event.actions
    ]
    return data


def _anonymous_view(event: Event) -> dict[str, Any]:
    """未认证查询：只保留事件处置进度等公开信息。"""

    return {
        "event_id": event.event_id,
        "stage": event.stage,
        "location": event.location,
        "first_reported_at": event.first_reported_at,
        "last_reported_at": event.last_reported_at,
        "subject_type": event.subject_type,
        "subject_id": (
            mask_plate(event.subject_id) if event.subject_type == "plate"
            else event.subject_id
        ),
        "reports_count": len(event.reports),
        "evidence_count": len(event.evidence),
        "appeals_count": len(event.appeals),
        "actions": [
            {"type": a.type, "at": a.at, "basis_version": a.basis_version}
            for a in event.actions
        ],
    }


def serialize_event(event: Event, role: str) -> dict[str, Any]:
    if role not in ALL_ROLES:
        raise ValueError(f"未知角色: {role}")
    if role in STAFF_ROLES:
        view = _staff_view(event)
    elif role == "owner":
        view = _owner_view(event)
    else:
        view = _anonymous_view(event)
    view["_role"] = role
    return view


def serialize_chain(chain: list[dict[str, Any]], role: str) -> list[dict[str, Any]]:
    if role in STAFF_ROLES:
        return chain
    masked = []
    for item in chain:
        data = dict(item["data"])
        if role == "anonymous":
            data = {"at": data.get("at")}
        else:
            for key in ("reporter", "source_chat", "appellant", "contact"):
                data.pop(key, None)
            if "operator" in data:
                data["operator"] = mask_name(data["operator"])
            if "collected_by" in data:
                data["collected_by"] = mask_name(data["collected_by"])
            if "detail" in data:
                data["detail"] = {
                    k: v for k, v in data["detail"].items()
                    if k not in ("note", "message")
                }
        masked.append({"kind": item["kind"], "at": item["at"], "data": data})
    return masked
