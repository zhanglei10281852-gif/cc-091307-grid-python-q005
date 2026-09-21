"""领域模型：秩序事件及其证据、申诉、处置留痕。"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# 事件处置阶段
STAGE_COLLECTING = "collecting"   # 取证中
STAGE_NOTIFIED = "notified"       # 已告知
STAGE_APPEALING = "appealing"     # 申诉中（待复核）
STAGE_ENFORCED = "enforced"       # 已处置
STAGE_CLOSED = "closed"           # 已结案
STAGE_REVOKED = "revoked"         # 已撤销
STAGE_CLUE = "clue"               # 匿名线索（无法确认车主）

CLOSED_STAGES = {STAGE_CLOSED, STAGE_REVOKED}


@dataclass
class Report:
    """一次上报：晚高峰巡查或群聊转来的线索都算一次上报。"""

    report_id: str
    reporter: str
    source_chat: Optional[str]
    location: str
    reported_at: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Report":
        return cls(**data)


@dataclass
class Evidence:
    """证据材料：现场照片、视频、申诉截图等，可随时追加到同一事件。"""

    evidence_id: str
    kind: str
    uri: str
    collected_by: str
    collected_at: str
    source_chat: Optional[str] = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Evidence":
        return cls(**data)


@dataclass
class Action:
    """每项正式处置动作的留痕：告知、处置、复核、撤销、结案等。

    强制记录依据版本（制度/法规版本）、经办人、时间；
    撤销决定必须带原因，放在 detail["reason"]。
    """

    action_id: str
    type: str
    operator: str
    at: str
    basis_version: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Action":
        return cls(**data)


@dataclass
class Appeal:
    """车主申诉。pending 期间禁止执行处置。"""

    appeal_id: str
    reason: str
    appellant: str
    contact: str
    created_at: str
    status: str = "pending"  # pending / upheld / rejected
    review: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Appeal":
        return cls(**data)


@dataclass
class Event:
    """以车牌或设备标识 + 位置 + 采集时间建立的秩序事件。"""

    event_id: str
    location: str
    location_norm: str
    first_reported_at: str
    last_reported_at: str
    stage: str = STAGE_COLLECTING
    subject_type: Optional[str] = None   # plate / device；匿名线索为 None
    subject_id: Optional[str] = None
    owner_name: Optional[str] = None
    owner_contact: Optional[str] = None
    reports: list[Report] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    appeals: list[Appeal] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    stage_before_appeal: Optional[str] = None
    merged_clue_ids: list[str] = field(default_factory=list)

    @property
    def is_anonymous(self) -> bool:
        return self.subject_type is None

    @property
    def subject_key(self) -> Optional[str]:
        if self.subject_type is None:
            return None
        return f"{self.subject_type}:{self.subject_id}"

    @property
    def open_appeal(self) -> Optional[Appeal]:
        for appeal in reversed(self.appeals):
            if appeal.status == "pending":
                return appeal
        return None

    @property
    def is_enforced(self) -> bool:
        return any(a.type == "enforce" for a in self.actions)

    @property
    def enforcement(self) -> Optional[Action]:
        for action in reversed(self.actions):
            if action.type == "enforce":
                return action
        return None

    @property
    def revocation(self) -> Optional[Action]:
        for action in reversed(self.actions):
            if action.type == "revoke":
                return action
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "stage": self.stage,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "owner_name": self.owner_name,
            "owner_contact": self.owner_contact,
            "location": self.location,
            "location_norm": self.location_norm,
            "first_reported_at": self.first_reported_at,
            "last_reported_at": self.last_reported_at,
            "stage_before_appeal": self.stage_before_appeal,
            "merged_clue_ids": self.merged_clue_ids,
            "reports": [r.to_dict() for r in self.reports],
            "evidence": [e.to_dict() for e in self.evidence],
            "appeals": [a.to_dict() for a in self.appeals],
            "actions": [a.to_dict() for a in self.actions],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        return cls(
            event_id=data["event_id"],
            stage=data["stage"],
            subject_type=data.get("subject_type"),
            subject_id=data.get("subject_id"),
            owner_name=data.get("owner_name"),
            owner_contact=data.get("owner_contact"),
            location=data["location"],
            location_norm=data["location_norm"],
            first_reported_at=data["first_reported_at"],
            last_reported_at=data["last_reported_at"],
            stage_before_appeal=data.get("stage_before_appeal"),
            merged_clue_ids=data.get("merged_clue_ids", []),
            reports=[Report.from_dict(x) for x in data.get("reports", [])],
            evidence=[Evidence.from_dict(x) for x in data.get("evidence", [])],
            appeals=[Appeal.from_dict(x) for x in data.get("appeals", [])],
            actions=[Action.from_dict(x) for x in data.get("actions", [])],
        )
