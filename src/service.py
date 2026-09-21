"""社区停车充电秩序事件服务。

核心规则：
- 以车牌或设备标识 + 位置 + 采集时间建立事件；
- 同一车辆（设备）短时同点重复上报关联到既有事件，而不是新建；
- 无法确认车主时只能生成匿名线索，线索确认车主后转为正式事件；
- 支持证据追加、告知、申诉、复核、撤销、结案；
- 申诉待复核期间冻结处置，已处置事件不得重复处罚；
- 每项处置记录依据版本、经办人、时间；撤销必须填写原因；
- 计数不单独持久化，加载时由事件链重算，重启后状态与计数一致。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from .errors import (
    ConflictError,
    NotFoundError,
    StageError,
    ValidationError,
)
from .models import (
    Action,
    Appeal,
    CLOSED_STAGES,
    Evidence,
    Event,
    Report,
    STAGE_CLUE,
    STAGE_COLLECTING,
    STAGE_NOTIFIED,
    STAGE_REVOKED,
)
from .storage import JsonStore

_WS_RE = re.compile(r"\s+")

# 正式处置动作必须登记依据版本
_DISPOSITION_TYPES = {"notify", "enforce", "review", "revoke", "close"}


def _default_clock() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_dt(value: str) -> datetime:
    """解析 ISO 时间，统一成 naive UTC，便于跨时区比较。"""

    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def norm_location(location: str, location_code: Optional[str] = None) -> str:
    if location_code:
        return f"code:{location_code.strip()}"
    return "loc:" + _WS_RE.sub("", location).lower()


def norm_subject_id(subject_type: str, subject_id: str) -> str:
    value = subject_id.strip()
    if subject_type == "plate":
        # 车牌统一大写，去除间隔符
        value = value.upper().replace(" ", "").replace("-", "")
    return value


class OrderService:
    """秩序事件领域服务。"""

    def __init__(
        self,
        store: JsonStore,
        *,
        merge_window_minutes: int = 30,
        clock: Optional[Callable[[], str]] = None,
    ):
        self.store = store
        self.merge_window = timedelta(minutes=merge_window_minutes)
        self.clock = clock or _default_clock
        self.events: dict[str, Event] = {
            eid: Event.from_dict(data) for eid, data in store.events_raw.items()
        }

    # ------------------------------------------------------------------ 工具

    def _now(self) -> str:
        return self.clock()

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}-{self.store.next_seq():06d}"

    def _save(self, event: Event) -> None:
        self.store.events_raw[event.event_id] = event.to_dict()
        self.store.save()

    def _get(self, event_id: str) -> Event:
        event = self.events.get(event_id)
        if event is None:
            raise NotFoundError(f"事件不存在: {event_id}")
        return event

    def _require_text(self, payload: dict[str, Any], key: str, label: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{label}不能为空")
        return value.strip()

    def _add_action(
        self,
        event: Event,
        type_: str,
        operator: str,
        *,
        basis_version: str = "",
        detail: Optional[dict[str, Any]] = None,
        at: Optional[str] = None,
    ) -> Action:
        if type_ in _DISPOSITION_TYPES and not basis_version:
            raise ValidationError("处置动作必须登记依据版本")
        action = Action(
            action_id=self._new_id("act"),
            type=type_,
            operator=operator,
            at=at or self._now(),
            basis_version=basis_version,
            detail=detail or {},
        )
        event.actions.append(action)
        return action

    def _all_events(self) -> list[Event]:
        return list(self.events.values())

    def _find_merge_target(
        self,
        *,
        subject_key: Optional[str],
        location_key: str,
        when: datetime,
    ) -> Optional[Event]:
        """短时同点重复上报的关联目标。"""

        for event in self._all_events():
            if event.stage in CLOSED_STAGES:
                continue
            if event.location_norm != location_key:
                continue
            if subject_key is not None:
                if event.subject_key != subject_key:
                    continue
            else:
                # 匿名线索只与同为匿名的线索合并
                if event.subject_key is not None:
                    continue
            try:
                first = parse_dt(event.first_reported_at)
                last = parse_dt(event.last_reported_at)
            except ValueError:
                continue
            if first - self.merge_window <= when <= last + self.merge_window:
                return event
        return None

    # ---------------------------------------------------------- 上报/建事件

    def create_or_report(self, payload: dict[str, Any]) -> tuple[Event, bool]:
        """上报一条秩序问题。命中短时同点规则时追加到既有事件。

        返回 (事件, 是否为新建)。
        """

        location_raw = payload.get("location")
        location_code = payload.get("location_code")
        if not isinstance(location_raw, str) or not location_raw.strip():
            location_raw = location_code  # 只给位置编码时以编码作为展示名
        if not isinstance(location_raw, str) or not location_raw.strip():
            raise ValidationError("位置（location 或 location_code）不能为空")
        location = location_raw.strip()
        reporter = self._require_text(payload, "reporter", "上报人")
        if location_code is not None and not isinstance(location_code, str):
            raise ValidationError("location_code 必须是字符串")
        when_raw = payload.get("reported_at") or self._now()
        try:
            when = parse_dt(when_raw)
        except (ValueError, TypeError):
            raise ValidationError("reported_at 时间格式不正确")

        subject_type = payload.get("subject_type")
        subject_id = payload.get("subject_id")
        if subject_type is not None:
            if subject_type not in ("plate", "device"):
                raise ValidationError("subject_type 只能是 plate 或 device")
            if not isinstance(subject_id, str) or not subject_id.strip():
                # 有标识类型但给不出有效标识：按无法确认车主处理，
                # 必须显式传 anonymous=true，避免误建匿名线索。
                if payload.get("anonymous") is not True:
                    raise ValidationError("缺少有效车牌/设备标识；如无法确认车主请声明 anonymous=true")
                subject_type = None
                subject_id = None
            else:
                subject_id = norm_subject_id(subject_type, subject_id)

        owner_name = payload.get("owner_name")
        owner_contact = payload.get("owner_contact")
        location_key = norm_location(location, location_code)
        subject_key = (
            f"{subject_type}:{subject_id}" if subject_type is not None else None
        )

        target = self._find_merge_target(
            subject_key=subject_key, location_key=location_key, when=when
        )
        report = Report(
            report_id=self._new_id("rpt"),
            reporter=reporter,
            source_chat=payload.get("source_chat"),
            location=location,
            reported_at=when_raw,
            note=payload.get("note", ""),
        )

        if target is not None:
            target.reports.append(report)
            if parse_dt(when_raw) > parse_dt(target.last_reported_at):
                target.last_reported_at = when_raw
            # 重复上报带来的证据一并归入事件链
            self._ingest_evidence(payload, target, reporter, when_raw)
            self._save(target)
            return target, False

        is_anonymous = subject_type is None or payload.get("anonymous") is True
        event = Event(
            event_id=self._new_id("evt"),
            location=location,
            location_norm=location_key,
            first_reported_at=when_raw,
            last_reported_at=when_raw,
            stage=STAGE_CLUE if is_anonymous else STAGE_COLLECTING,
            subject_type=None if is_anonymous else subject_type,
            subject_id=None if is_anonymous else subject_id,
            owner_name=owner_name if not is_anonymous else None,
            owner_contact=owner_contact if not is_anonymous else None,
        )
        event.reports.append(report)
        self._ingest_evidence(payload, event, reporter, when_raw)
        self.events[event.event_id] = event
        self._save(event)
        return event, True

    def _ingest_evidence(
        self,
        payload: dict[str, Any],
        event: Event,
        default_by: str,
        default_at: str,
    ) -> None:
        items = payload.get("evidence") or []
        if not isinstance(items, list):
            raise ValidationError("evidence 必须是数组")
        for item in items:
            if not isinstance(item, dict):
                raise ValidationError("evidence 项必须是对象")
            kind = item.get("kind") or "photo"
            uri = item.get("uri")
            if not isinstance(uri, str) or not uri.strip():
                raise ValidationError("证据 uri 不能为空")
            event.evidence.append(
                Evidence(
                    evidence_id=self._new_id("evd"),
                    kind=str(kind),
                    uri=uri.strip(),
                    collected_by=item.get("collected_by") or default_by,
                    collected_at=item.get("collected_at") or default_at,
                    source_chat=item.get("source_chat") or payload.get("source_chat"),
                    note=item.get("note", ""),
                )
            )

    # ------------------------------------------------------------ 证据追加

    def add_evidence(self, event_id: str, payload: dict[str, Any]) -> Evidence:
        event = self._get(event_id)
        if event.stage in CLOSED_STAGES:
            raise StageError("事件已终结，不能再追加证据")
        uri = self._require_text(payload, "uri", "证据 uri")
        collected_by = self._require_text(payload, "collected_by", "采集人")
        evidence = Evidence(
            evidence_id=self._new_id("evd"),
            kind=str(payload.get("kind") or "photo"),
            uri=uri,
            collected_by=collected_by,
            collected_at=payload.get("collected_at") or self._now(),
            source_chat=payload.get("source_chat"),
            note=payload.get("note", ""),
        )
        event.evidence.append(evidence)
        self._save(event)
        return evidence

    # ----------------------------------------------------- 匿名线索转正式

    def identify_clue(
        self, event_id: str, payload: dict[str, Any]
    ) -> tuple[Event, bool]:
        """匿名线索确认车主/车辆后转为正式事件。

        若同时段同点已存在该车辆的正式事件，则把线索并入既有事件。
        返回 (事件, 是否发生了合并)。
        """

        event = self._get(event_id)
        if event.stage != STAGE_CLUE:
            raise StageError("只有匿名线索可以确认主体")
        subject_type = self._require_text(payload, "subject_type", "标识类型")
        if subject_type not in ("plate", "device"):
            raise ValidationError("subject_type 只能是 plate 或 device")
        subject_id = norm_subject_id(
            subject_type, self._require_text(payload, "subject_id", "车牌/设备标识")
        )
        subject_key = f"{subject_type}:{subject_id}"

        target = self._find_merge_target(
            subject_key=subject_key,
            location_key=event.location_norm,
            when=parse_dt(event.last_reported_at),
        )

        operator = self._require_text(payload, "operator", "经办人")
        if target is not None and target.event_id != event.event_id:
            # 把线索上的上报记录与证据并入正式事件
            target.reports.extend(event.reports)
            target.evidence.extend(event.evidence)
            target.merged_clue_ids.append(event.event_id)
            if parse_dt(event.last_reported_at) > parse_dt(target.last_reported_at):
                target.last_reported_at = event.last_reported_at
            self._add_action(
                target,
                "merge_clue",
                operator,
                detail={"clue_id": event.event_id},
            )
            event.subject_type = subject_type
            event.subject_id = subject_id
            event.owner_name = payload.get("owner_name")
            event.owner_contact = payload.get("owner_contact")
            from .models import STAGE_CLOSED

            event.stage = STAGE_CLOSED
            self._add_action(
                event,
                "merged_into",
                operator,
                detail={"event_id": target.event_id},
            )
            self._save(event)
            self._save(target)
            return target, True

        event.subject_type = subject_type
        event.subject_id = subject_id
        event.owner_name = payload.get("owner_name")
        event.owner_contact = payload.get("owner_contact")
        event.stage = STAGE_COLLECTING
        self._add_action(event, "identify", operator)
        self._save(event)
        return event, False

    # ------------------------------------------------------------------ 告知

    def notify(self, event_id: str, payload: dict[str, Any]) -> Action:
        event = self._get(event_id)
        operator = self._require_text(payload, "operator", "经办人")
        basis_version = self._require_text(payload, "basis_version", "依据版本")
        if event.stage == STAGE_CLUE:
            raise StageError("匿名线索尚未确认车主，不能告知；请先确认主体")
        if event.open_appeal is not None:
            raise StageError("申诉复核期间不能执行处置")
        if event.stage != STAGE_COLLECTING:
            raise StageError(f"当前阶段 {event.stage} 不能告知")
        action = self._add_action(
            event,
            "notify",
            operator,
            basis_version=basis_version,
            detail={
                "channel": payload.get("channel", "onsite"),
                "message": payload.get("message", ""),
            },
        )
        event.stage = STAGE_NOTIFIED
        self._save(event)
        return action

    # ------------------------------------------------------------------ 申诉

    def file_appeal(self, event_id: str, payload: dict[str, Any]) -> Appeal:
        event = self._get(event_id)
        if event.stage == STAGE_CLUE:
            raise StageError("匿名线索不能申诉")
        if event.stage in CLOSED_STAGES:
            raise StageError("事件已终结，不能申诉")
        if event.open_appeal is not None:
            raise ConflictError("已有待复核申诉，请勿重复提交")
        reason = self._require_text(payload, "reason", "申诉理由")
        appellant = self._require_text(payload, "appellant", "申诉人")
        contact = self._require_text(payload, "contact", "联系方式")
        appeal = Appeal(
            appeal_id=self._new_id("apd"),
            reason=reason,
            appellant=appellant,
            contact=contact,
            created_at=self._now(),
        )
        event.appeals.append(appeal)
        event.stage_before_appeal = event.stage
        from .models import STAGE_APPEALING

        event.stage = STAGE_APPEALING
        self._save(event)
        return appeal

    # ------------------------------------------------------------------ 复核

    def review_appeal(self, event_id: str, appeal_id: str, payload: dict[str, Any]) -> Appeal:
        event = self._get(event_id)
        appeal = next((a for a in event.appeals if a.appeal_id == appeal_id), None)
        if appeal is None:
            raise NotFoundError(f"申诉不存在: {appeal_id}")
        if appeal.status != "pending":
            raise ConflictError("该申诉已复核")
        operator = self._require_text(payload, "operator", "复核人")
        basis_version = self._require_text(payload, "basis_version", "依据版本")
        decision = self._require_text(payload, "decision", "复核结论")
        if decision not in ("upheld", "rejected"):
            raise ValidationError("decision 只能是 upheld（成立）或 rejected（不成立）")
        at = self._now()
        appeal.status = decision
        appeal.review = {
            "operator": operator,
            "at": at,
            "basis_version": basis_version,
            "decision": decision,
            "comment": payload.get("comment", ""),
        }
        self._add_action(
            event,
            "review",
            operator,
            basis_version=basis_version,
            detail={"appeal_id": appeal_id, "decision": decision,
                    "comment": payload.get("comment", "")},
            at=at,
        )
        if decision == "upheld":
            # 申诉成立：撤销已作出的处置/拟处置并终结事件，撤销原因必须留痕
            reason = f"申诉成立：{appeal.reason}"
            if event.revocation is None:
                self._add_action(
                    event,
                    "revoke",
                    operator,
                    basis_version=basis_version,
                    detail={"reason": reason, "appeal_id": appeal_id},
                    at=at,
                )
            event.stage = STAGE_REVOKED
            event.stage_before_appeal = None
        else:
            event.stage = event.stage_before_appeal or STAGE_NOTIFIED
            event.stage_before_appeal = None
        self._save(event)
        return appeal

    # ------------------------------------------------------------------ 处置

    def enforce(self, event_id: str, payload: dict[str, Any]) -> Action:
        event = self._get(event_id)
        operator = self._require_text(payload, "operator", "经办人")
        basis_version = self._require_text(payload, "basis_version", "依据版本")
        measure = self._require_text(payload, "measure", "处置措施")
        if event.stage == STAGE_CLUE:
            raise StageError("匿名线索不能执行处置，请先确认车主")
        if event.open_appeal is not None:
            raise StageError("申诉复核期间不得执行处置")
        if event.stage in CLOSED_STAGES:
            raise StageError("事件已终结，不能再处置")
        if event.is_enforced and event.revocation is None:
            # 防止同一辆车因多群聊证据被重复处罚
            raise ConflictError("该事件已处置且未撤销，不得重复处罚")
        if event.stage != STAGE_NOTIFIED:
            raise StageError("须先告知车主，才能执行处置")
        action = self._add_action(
            event,
            "enforce",
            operator,
            basis_version=basis_version,
            detail={"measure": measure, "note": payload.get("note", "")},
        )
        from .models import STAGE_ENFORCED

        event.stage = STAGE_ENFORCED
        self._save(event)
        return action

    # ------------------------------------------------------------------ 撤销

    def revoke(self, event_id: str, payload: dict[str, Any]) -> Action:
        event = self._get(event_id)
        operator = self._require_text(payload, "operator", "经办人")
        basis_version = self._require_text(payload, "basis_version", "依据版本")
        reason = self._require_text(payload, "reason", "撤销原因")
        if event.stage in CLOSED_STAGES:
            raise StageError("事件已终结")
        if event.open_appeal is not None:
            raise StageError("有待复核申诉，请先走复核流程")
        if not event.is_enforced and event.stage == STAGE_CLUE:
            raise StageError("匿名线索无需撤销")
        action = self._add_action(
            event,
            "revoke",
            operator,
            basis_version=basis_version,
            detail={"reason": reason},
        )
        event.stage = STAGE_REVOKED
        self._save(event)
        return action

    # ------------------------------------------------------------------ 结案

    def close(self, event_id: str, payload: dict[str, Any]) -> Action:
        event = self._get(event_id)
        operator = self._require_text(payload, "operator", "经办人")
        basis_version = self._require_text(payload, "basis_version", "依据版本")
        if event.open_appeal is not None:
            raise StageError("申诉复核期间不能结案")
        if event.stage in CLOSED_STAGES:
            raise StageError("事件已终结")
        if event.stage not in (STAGE_NOTIFIED, "enforced"):
            raise StageError(f"当前阶段 {event.stage} 不能结案")
        action = self._add_action(
            event,
            "close",
            operator,
            basis_version=basis_version,
            detail={"note": payload.get("note", "")},
        )
        from .models import STAGE_CLOSED

        event.stage = STAGE_CLOSED
        self._save(event)
        return action

    # ------------------------------------------------------------------ 查询

    def get_event(self, event_id: str) -> Event:
        return self._get(event_id)

    def list_events(self, stage: Optional[str] = None) -> list[Event]:
        events = self._all_events()
        if stage:
            events = [e for e in events if e.stage == stage]
        events.sort(key=lambda e: e.first_reported_at)
        return events

    def pending_appeals(self) -> list[Event]:
        return [e for e in self._all_events() if e.open_appeal is not None]

    def event_chain(self, event_id: str) -> list[dict[str, Any]]:
        """完整事件链：上报、证据、申诉、处置按时间排列。"""

        event = self._get(event_id)
        chain: list[dict[str, Any]] = []
        for report in event.reports:
            chain.append(
                {"kind": "report", "at": report.reported_at, "data": report.to_dict()}
            )
        for evidence in event.evidence:
            chain.append(
                {"kind": "evidence", "at": evidence.collected_at,
                 "data": evidence.to_dict()}
            )
        for appeal in event.appeals:
            chain.append(
                {"kind": "appeal", "at": appeal.created_at,
                 "data": appeal.to_dict()}
            )
            if appeal.review:
                chain.append(
                    {"kind": "review", "at": appeal.review["at"],
                     "data": {"appeal_id": appeal.appeal_id, **appeal.review}}
                )
        for action in event.actions:
            chain.append(
                {"kind": "action", "at": action.at,
                 "data": action.to_dict()}
            )
        rank = {"report": 0, "evidence": 1, "appeal": 2, "action": 3, "review": 4}
        chain.sort(key=lambda item: (item["at"], rank.get(item["kind"], 9)))
        return chain

    def stats(self) -> dict[str, Any]:
        """计数由事件记录现场重算，重启后天然一致。"""

        by_stage: dict[str, int] = {}
        pending_appeals = 0
        enforced = 0
        total_reports = 0
        for event in self._all_events():
            by_stage[event.stage] = by_stage.get(event.stage, 0) + 1
            if event.open_appeal is not None:
                pending_appeals += 1
            if event.is_enforced and event.revocation is None:
                enforced += 1
            total_reports += len(event.reports)
        return {
            "events_total": len(self.events),
            "reports_total": total_reports,
            "by_stage": by_stage,
            "pending_appeals": pending_appeals,
            "enforced_active": enforced,
        }


# 兼容骨架中的入口名
Service = OrderService
