"""领域模型、枚举与异常定义。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# 时间工具：统一以 UTC ISO-8601 字符串落库，保证字典序即可按时间排序
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: Any) -> datetime:
    """接受 datetime 或 ISO-8601 字符串；朴素时间视为 UTC。"""
    if value is None:
        raise ValidationError("缺少时间字段")
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError(f"时间格式无法解析: {value}") from exc
    else:
        raise ValidationError(f"时间类型不支持: {type(value).__name__}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_iso(dt: datetime) -> str:
    return parse_dt(dt).isoformat(timespec="microseconds")


# ---------------------------------------------------------------------------
# 角色与事件阶段
# ---------------------------------------------------------------------------

class Role(str, Enum):
    GRID_WORKER = "grid_worker"   # 网格员：上报、证据追加、告知
    ADMIN = "admin"              # 管理端：复核、处置、撤销、结案
    OWNER = "owner"              # 车主：申诉、查看本人事件

    @classmethod
    def of(cls, value: Any) -> "Role":
        try:
            return cls(value)
        except ValueError as exc:
            raise PermissionError_("未知角色") from exc


class Stage(str, Enum):
    LEAD = "LEAD"            # 匿名线索（车主无法确认）
    OPEN = "OPEN"            # 已受理，待告知
    NOTIFIED = "NOTIFIED"    # 已告知
    ENFORCED = "ENFORCED"    # 已有生效处置
    APPEALED = "APPEALED"    # 申诉中（处置冻结）
    REVOKED = "REVOKED"      # 处置已撤销
    CLOSED = "CLOSED"        # 已结案


# ---------------------------------------------------------------------------
# 异常：携带错误码与 HTTP 状态，接口层统一映射
# ---------------------------------------------------------------------------

class OrderServiceError(Exception):
    code = "internal_error"
    http_status = 500

    def __init__(self, message: str, *, code: Optional[str] = None,
                 http_status: Optional[int] = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if http_status:
            self.http_status = http_status

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message}


class ValidationError(OrderServiceError):
    code = "invalid_request"
    http_status = 400


class NotFoundError(OrderServiceError):
    code = "not_found"
    http_status = 404


class PermissionError_(OrderServiceError):
    code = "forbidden"
    http_status = 403


class ConflictError(OrderServiceError):
    code = "conflict"
    http_status = 409


# ---------------------------------------------------------------------------
# 数据载体
# ---------------------------------------------------------------------------

@dataclass
class Actor:
    """经办人 / 操作人。"""
    id: str
    role: str
    name: Optional[str] = None

    def __post_init__(self):
        if not self.id or not str(self.id).strip():
            raise PermissionError_("缺少经办人身份（X-Actor-Id）")
        self.role = Role.of(self.role).value
        self.id = str(self.id).strip()


@dataclass
class EvidenceItem:
    kind: str = "photo"                 # photo / video / note / chat_record
    content: Optional[str] = None
    attachment_uri: Optional[str] = None
    source: Optional[str] = None        # 来源，例如所在群聊
    reporter_id: Optional[str] = None
    reporter_name: Optional[str] = None
    reporter_phone: Optional[str] = None
    collected_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class Incident:
    id: str
    subject_type: str                   # plate / device
    subject_id: str
    location_key: str
    location_name: Optional[str]
    lat: Optional[float]
    lng: Optional[float]
    owner_status: str                   # confirmed / anonymous
    stage: str
    first_collected_at: str
    last_collected_at: str
    created_at: str
    updated_at: str
    owner_id: Optional[str] = None
    owner_name: Optional[str] = None
    owner_phone: Optional[str] = None
    owner_id_no: Optional[str] = None
    closed_at: Optional[str] = None
    version: int = 1

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Disposal:
    """单项处置记录：依据版本、经办人、时间三要素齐备。"""
    id: int
    incident_id: str
    kind: str
    basis_version: str
    handler_id: str
    handler_name: Optional[str]
    decided_at: str
    status: str                         # active / revoked
    payload: dict = field(default_factory=dict)
    revoke_reason: Optional[str] = None
    revoked_by: Optional[str] = None
    revoked_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "incident_id": self.incident_id,
            "kind": self.kind,
            "basis_version": self.basis_version,
            "handler_id": self.handler_id,
            "handler_name": self.handler_name,
            "decided_at": self.decided_at,
            "status": self.status,
            "payload": self.payload,
            "revoke_reason": self.revoke_reason,
            "revoked_by": self.revoked_by,
            "revoked_at": self.revoked_at,
        }


@dataclass
class Appeal:
    incident_id: str
    reason: str
    filed_by: str
    filed_at: str
    prior_stage: str
    status: str                         # pending / rejected / upheld
    contact: Optional[str] = None
    decision_basis_version: Optional[str] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[str] = None
    review_note: Optional[str] = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class ChainEvent:
    """事件链上的一环，只追加、不修改。"""
    id: int
    incident_id: str
    seq: int
    action: str
    actor_id: str
    actor_role: str
    actor_name: Optional[str]
    at: str
    basis_version: Optional[str] = None
    reason: Optional[str] = None
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "incident_id": self.incident_id,
            "seq": self.seq,
            "action": self.action,
            "actor_id": self.actor_id,
            "actor_role": self.actor_role,
            "actor_name": self.actor_name,
            "at": self.at,
            "basis_version": self.basis_version,
            "reason": self.reason,
            "detail": self.detail,
        }
