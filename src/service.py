"""社区停车充电秩序事件服务（领域核心）。

围绕单个秩序事件的生命周期组织：

    上报 ─► 受理(OPEN) ─► 告知(NOTIFIED) ─► 处置(ENFORCED) ─► 结案(CLOSED)
      │                                  ▲          │
      └─ 无法确认车主 ─► 匿名线索(LEAD) ──┘      申诉(APPEALED，处置冻结)
                                                   ├─ 驳回 → 恢复处置
                                                   └─ 成立 → 撤销(REVOKED)

关键规则
========
* 同一车辆/设备在同一位置、短时间窗口（默认 2 小时）内重复上报，
  只关联为新的上报与证据，不新建事件，从源头避免重复处罚。
* 无法确认车主时只生成匿名线索（LEAD），线索阶段不得处置、不得告知车主。
* 申诉期间（APPEALED）冻结所有新增处置；申诉需经管理员复核。
* 每项处置记录依据版本、经办人、时间；撤销必须填写原因并保留原记录。
* 重启后由存储层按明细表重算计数，状态以持久化阶段字段为准。
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, Optional

from . import views
from .models import (
    Actor,
    ConflictError,
    NotFoundError,
    PermissionError_,
    Role,
    Stage,
    ValidationError,
    now_utc,
    parse_dt,
    to_iso,
)
from .storage import Repository

# 依据版本格式，例如 XZCF-2026.1
_BASIS_RE = re.compile(r"^[A-Za-z0-9一-鿿._-]{2,40}$")
_PLATE_RE = re.compile(r"^[0-9A-Za-z一-鿿·-]{4,20}$")
_DEVICE_RE = re.compile(r"^[A-Za-z0-9:_-]{4,64}$")

# 同车同点重复上报的关联窗口
DEFAULT_DEDUP_WINDOW = timedelta(hours=2)

VALID_EVIDENCE_KINDS = {"photo", "video", "note", "chat_record"}
VALID_DISPOSAL_KINDS = {
    "warning",          # 警告/劝离
    "fine",             # 罚款
    "tow",              # 拖移
    "rectify_notice",   # 责令整改
}


class OrderEventService:
    """秩序事件领域服务。

    Parameters
    ----------
    db_path:
        SQLite 文件路径；``:memory:`` 用于测试。
    dedup_window:
        同车同点重复上报的关联时间窗口。
    """

    def __init__(self, db_path: str = ":memory:",
                 dedup_window: timedelta = DEFAULT_DEDUP_WINDOW):
        self.repo = Repository(db_path)
        self.dedup_window = dedup_window
        self.ready = True

    def shutdown(self) -> None:
        self.repo.close()
        self.ready = False

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _require(actor: Actor, *roles: Role) -> None:
        if actor.role not in {r.value for r in roles}:
            raise PermissionError_(
                f"角色 {actor.role} 无权执行该操作"
            )

    @staticmethod
    def _basis(value: Any, field: str = "依据版本") -> str:
        if value is None or not str(value).strip():
            raise ValidationError(f"缺少{field}（basis_version）")
        text = str(value).strip()
        if not _BASIS_RE.match(text):
            raise ValidationError(f"{field}格式不合法: {text}")
        return text

    @staticmethod
    def _location(payload: dict) -> tuple[str, Optional[str],
                                          Optional[float], Optional[float]]:
        loc = payload.get("location")
        if isinstance(loc, dict):
            key = loc.get("location_key")
            name = loc.get("name")
            lat, lng = loc.get("lat"), loc.get("lng")
        else:
            key, name = loc, payload.get("location_name")
            lat, lng = payload.get("lat"), payload.get("lng")
        if not key or not str(key).strip():
            raise ValidationError("缺少位置标识 location.location_key")
        lat = _opt_float(lat)
        lng = _opt_float(lng)
        if lat is not None and not _valid_lat(lat):
            raise ValidationError("纬度超出范围")
        if lng is not None and not _valid_lng(lng):
            raise ValidationError("经度超出范围")
        return str(key).strip(), name, lat, lng

    @staticmethod
    def _subject(payload: dict) -> tuple[str, str]:
        subject_type = payload.get("subject_type", "plate")
        if subject_type not in ("plate", "device"):
            raise ValidationError("subject_type 仅支持 plate / device")
        sid = payload.get("subject_id")
        if not sid or not str(sid).strip():
            raise ValidationError("缺少车牌或设备标识 subject_id")
        sid = str(sid).strip().upper() if subject_type == "plate" \
            else str(sid).strip()
        pattern = _PLATE_RE if subject_type == "plate" else _DEVICE_RE
        if not pattern.match(sid):
            raise ValidationError(f"{subject_type} 标识格式不合法: {sid}")
        return subject_type, sid

    @staticmethod
    def _owner(payload: dict) -> tuple[str, Optional[dict]]:
        """返回 (owner_status, owner_info)。"""
        owner = payload.get("owner")
        if owner in (None, False, "", {}):
            return "anonymous", None
        if not isinstance(owner, dict):
            raise ValidationError("owner 必须为对象或置空（匿名线索）")
        if not owner.get("id"):
            raise ValidationError("确认车主必须提供 owner.id；"
                                  "无法确认时请省略 owner 以生成匿名线索")
        return "confirmed", {
            "owner_id": str(owner["id"]).strip(),
            "owner_name": owner.get("name"),
            "owner_phone": owner.get("phone"),
            "owner_id_no": owner.get("id_no"),
        }

    def _get_owned(self, incident_id: str, actor: Actor) -> dict:
        inc = self.repo.get_incident(incident_id)
        if inc is None:
            raise NotFoundError(f"事件不存在: {incident_id}")
        if actor.role == Role.OWNER.value:
            if inc["owner_status"] != "confirmed" or \
                    inc.get("owner_id") != actor.id:
                # 不暴露事件是否存在于他人名下
                raise NotFoundError(f"事件不存在: {incident_id}")
        return inc

    # ------------------------------------------------------------------
    # 1. 上报建事件 / 关联重复上报
    # ------------------------------------------------------------------

    def report(self, payload: dict, actor: Actor,
               collected_at: Any = None) -> dict:
        """网格员上报秩序问题。

        同车（同设备）同点且采集时间距该事件最近一次采集在窗口内，
        关联为既有事件的追加上报，不新建、不触发新的处置。
        """
        self._require(actor, Role.GRID_WORKER, Role.ADMIN)
        subject_type, subject_id = self._subject(payload)
        location_key, loc_name, lat, lng = self._location(payload)
        collected = to_iso(parse_dt(collected_at) if collected_at
                           else now_utc())
        owner_status, owner = self._owner(payload)
        ev_items = _validate_evidences(payload.get("evidences"), collected)

        ts = to_iso(now_utc())
        with self.repo.transaction():
            existing = self.repo.latest_incident_for(
                subject_type, subject_id, location_key
            )
            related = False
            if existing is not None:
                gap = parse_dt(collected) - parse_dt(existing["last_collected_at"])
                # 采集时间早于既有事件时取绝对值，允许晚到/补报
                if abs(gap) <= self.dedup_window and \
                        existing["stage"] != Stage.CLOSED.value:
                    related = True
            if related:
                inc = existing
                report_id = self._add_report_locked(
                    inc, collected, actor, payload, ev_items, ts,
                    channel=payload.get("channel"))
                inc = self.repo.get_incident(inc["id"])
                self.repo.append_chain(
                    inc["id"], "report_linked", _actor_dict(actor), ts,
                    detail={"report_id": report_id,
                            "dedup_window_seconds":
                                int(self.dedup_window.total_seconds()),
                            "evidence_count": len(ev_items)})
                return {"merged": True, "incident_id": inc["id"],
                        "report_id": report_id,
                        "incident": self.repo.get_incident(inc["id"])}

            seq = self.repo.next_incident_seq()
            incident_id = f"INC-{seq:06d}"
            stage = Stage.LEAD.value if owner_status == "anonymous" \
                else Stage.OPEN.value
            inc = {
                "id": incident_id,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "location_key": location_key,
                "location_name": loc_name,
                "lat": lat,
                "lng": lng,
                "owner_status": owner_status,
                "stage": stage,
                "first_collected_at": collected,
                "last_collected_at": collected,
                "owner_id": owner["owner_id"] if owner else None,
                "owner_name": owner["owner_name"] if owner else None,
                "owner_phone": owner["owner_phone"] if owner else None,
                "owner_id_no": owner["owner_id_no"] if owner else None,
                "report_count": 0,
                "active_disposal_count": 0,
                "created_at": ts,
                "updated_at": ts,
                "closed_at": None,
                "version": 1,
            }
            self.repo.insert_incident(inc)
            report_id = self._add_report_locked(
                inc, collected, actor, payload, ev_items, ts,
                channel=payload.get("channel"), first=True)
            self.repo.append_chain(
                incident_id, "created", _actor_dict(actor), ts,
                detail={"stage": stage, "owner_status": owner_status,
                        "report_id": report_id})
            return {"merged": False, "incident_id": incident_id,
                    "report_id": report_id,
                    "incident": self.repo.get_incident(incident_id)}

    def _add_report_locked(self, inc: dict, collected: str, actor: Actor,
                           payload: dict, ev_items: list[dict], ts: str,
                           *, channel: Optional[str] = None,
                           first: bool = False) -> int:
        reporter = {
            "id": actor.id,
            "name": actor.name or payload.get("reporter_name"),
            "phone": payload.get("reporter_phone"),
        }
        report_id = self.repo.insert_report(
            inc["id"], collected, reporter, channel,
            payload.get("report_payload"), ts)
        for ev in ev_items:
            ev = dict(ev)
            ev.setdefault("reporter_id", reporter["id"])
            ev.setdefault("reporter_name", reporter["name"])
            ev.setdefault("reporter_phone", reporter["phone"])
            ev.setdefault("collected_at", collected)
            ev.setdefault("source", channel)
            ev["report_id"] = report_id
            self.repo.insert_evidence(inc["id"], ev, ts)
        fields = {"last_collected_at": collected, "updated_at": ts,
                  "version": inc["version"] + 1}
        self.repo.update_incident(inc["id"], fields)
        return report_id

    # ------------------------------------------------------------------
    # 2. 证据追加
    # ------------------------------------------------------------------

    def add_evidence(self, incident_id: str, payload: dict, actor: Actor,
                     collected_at: Any = None) -> dict:
        self._require(actor, Role.GRID_WORKER, Role.ADMIN)
        inc = self.repo.get_incident(incident_id)
        if inc is None:
            raise NotFoundError(f"事件不存在: {incident_id}")
        if inc["stage"] == Stage.CLOSED.value:
            raise ConflictError("事件已结案，证据请通过复核/重开流程追加")
        collected = to_iso(parse_dt(collected_at) if collected_at
                           else now_utc())
        items = _validate_evidences(payload.get("evidences")
                                    if payload.get("evidences") is not None
                                    else [payload], collected)
        ts = to_iso(now_utc())
        ids = []
        with self.repo.transaction():
            for ev in items:
                ev = dict(ev)
                ev.setdefault("reporter_id", actor.id)
                ev.setdefault("reporter_name", actor.name)
                ev.setdefault("reporter_phone", payload.get("reporter_phone"))
                ev["collected_at"] = collected
                ev["report_id"] = None
                ids.append(self.repo.insert_evidence(inc["id"], ev, ts))
            self.repo.update_incident(inc["id"],
                                      {"updated_at": ts,
                                       "version": inc["version"] + 1})
            self.repo.append_chain(
                incident_id, "evidence_added", _actor_dict(actor), ts,
                detail={"evidence_ids": ids, "count": len(ids)})
        return {"incident_id": incident_id, "evidence_ids": ids}

    # ------------------------------------------------------------------
    # 3. 车主确认 / 告知
    # ------------------------------------------------------------------

    def identify_owner(self, incident_id: str, payload: dict,
                       actor: Actor) -> dict:
        """匿名线索经核实确认车主后，转为可告知的受理事件。"""
        self._require(actor, Role.GRID_WORKER, Role.ADMIN)
        inc = self._require_incident(incident_id)
        if inc["stage"] != Stage.LEAD.value:
            raise ConflictError(f"当前阶段 {inc['stage']} 无需再确认车主")
        owner = payload.get("owner")
        if not isinstance(owner, dict) or not owner.get("id"):
            raise ValidationError("确认车主需提供 owner.id/name 等信息")
        ts = to_iso(now_utc())
        fields = {
            "owner_status": "confirmed",
            "owner_id": str(owner["id"]).strip(),
            "owner_name": owner.get("name"),
            "owner_phone": owner.get("phone"),
            "owner_id_no": owner.get("id_no"),
            "stage": Stage.OPEN.value,
            "updated_at": ts,
            "version": inc["version"] + 1,
        }
        with self.repo.transaction():
            self.repo.update_incident(incident_id, fields)
            self.repo.append_chain(
                incident_id, "owner_identified", _actor_dict(actor), ts,
                detail={"owner_name": owner.get("name")})
        return {"incident_id": incident_id, "stage": Stage.OPEN.value}

    def notify_owner(self, incident_id: str, payload: dict,
                     actor: Actor) -> dict:
        """送达告知（处罚前告知）。须记录依据版本与送达方式。"""
        self._require(actor, Role.GRID_WORKER, Role.ADMIN)
        inc = self._require_incident(incident_id)
        basis = self._basis(payload.get("basis_version"), "告知依据版本")
        if inc["owner_status"] != "confirmed":
            raise ConflictError("匿名线索不得向车主告知，请先确认车主")
        if inc["stage"] == Stage.CLOSED.value:
            raise ConflictError("事件已结案")
        if inc["stage"] == Stage.APPEALED.value:
            raise ConflictError("申诉处理期间暂停告知")
        method = payload.get("method", "onsite")
        if method not in ("onsite", "sms", "app", "paper"):
            raise ValidationError("告知方式不支持")
        ts = to_iso(now_utc())
        with self.repo.transaction():
            self.repo.update_incident(
                incident_id,
                {"stage": Stage.NOTIFIED.value, "updated_at": ts,
                 "version": inc["version"] + 1})
            self.repo.append_chain(
                incident_id, "notified", _actor_dict(actor), ts,
                basis_version=basis,
                detail={"method": method,
                        "notify_basis_version": basis,
                        "note": payload.get("note")})
        return {"incident_id": incident_id, "stage": Stage.NOTIFIED.value,
                "basis_version": basis}

    # ------------------------------------------------------------------
    # 4. 处置（每项留痕：依据版本/经办人/时间）
    # ------------------------------------------------------------------

    def dispose(self, incident_id: str, payload: dict, actor: Actor) -> dict:
        self._require(actor, Role.ADMIN)
        inc = self._require_incident(incident_id)
        kind = payload.get("kind")
        if kind not in VALID_DISPOSAL_KINDS:
            raise ValidationError(
                f"处置类型不支持: {kind}，可选 {sorted(VALID_DISPOSAL_KINDS)}")
        basis = self._basis(payload.get("basis_version"))
        if inc["owner_status"] != "confirmed":
            raise ConflictError("匿名线索不得实施处置，请先确认车主并告知")
        if inc["stage"] == Stage.APPEALED.value:
            raise ConflictError("申诉处理期间不得新增处置（处置冻结）")
        if inc["stage"] == Stage.CLOSED.value:
            raise ConflictError("事件已结案")
        if inc["stage"] not in (Stage.NOTIFIED.value, Stage.ENFORCED.value,
                                Stage.REVOKED.value):
            raise ConflictError(
                f"当前阶段 {inc['stage']} 须先完成告知才能处置")
        # 同类型生效处置不重复出具，避免重复处罚
        for active in self.repo.get_active_disposals(incident_id):
            if active["kind"] == kind:
                raise ConflictError(
                    f"已存在生效的同类处置（{kind}），"
                    "同一事件不得重复执行；如情况变化请先撤销原处置")
        ts = to_iso(now_utc())
        disposal = {
            "incident_id": incident_id,
            "kind": kind,
            "basis_version": basis,
            "handler_id": actor.id,
            "handler_name": actor.name,
            "decided_at": ts,
            "payload": {
                "amount": payload.get("amount"),
                "content": payload.get("content"),
                "due_at": payload.get("due_at"),
            },
        }
        with self.repo.transaction():
            did = self.repo.insert_disposal(disposal)
            self.repo.update_incident(
                incident_id,
                {"stage": Stage.ENFORCED.value, "updated_at": ts,
                 "version": inc["version"] + 1})
            self.repo.append_chain(
                incident_id, "disposed", _actor_dict(actor), ts,
                basis_version=basis,
                detail={"disposal_id": did, "kind": kind,
                        "amount": payload.get("amount")})
        return {"incident_id": incident_id, "disposal_id": did,
                "stage": Stage.ENFORCED.value}

    def revoke_disposal(self, incident_id: str, disposal_id: int,
                        payload: dict, actor: Actor) -> dict:
        """撤销处置：必须填写原因，原处置记录保留并标记 revoked。"""
        self._require(actor, Role.ADMIN)
        inc = self._require_incident(incident_id)
        reason = (payload or {}).get("reason")
        if not reason or not str(reason).strip():
            raise ValidationError("撤销处置必须填写原因 reason")
        basis = self._basis(payload.get("basis_version"), "撤销依据版本")
        ts = to_iso(now_utc())
        revoked_ids: list[int] = []
        with self.repo.transaction():
            target = self.repo.get_disposal(int(disposal_id))
            if target is None:
                raise NotFoundError(f"处置记录不存在: {disposal_id}")
            if target["incident_id"] != incident_id:
                raise ValidationError("处置记录与事件不匹配")
            if target["status"] != "active":
                raise ConflictError("该处置此前已撤销")
            record = self.repo.revoke_disposal(
                int(disposal_id), str(reason).strip(), actor.id, ts)
            active = self.repo.get_active_disposals(incident_id)
            new_stage = Stage.REVOKED.value if not active else inc["stage"]
            self.repo.update_incident(
                incident_id,
                {"stage": new_stage, "updated_at": ts,
                 "version": inc["version"] + 1})
            self.repo.append_chain(
                incident_id, "disposal_revoked", _actor_dict(actor), ts,
                basis_version=basis, reason=str(reason).strip(),
                detail={"disposal_id": int(disposal_id),
                        "kind": record["kind"]})
        return {"incident_id": incident_id,
                "disposal_id": int(disposal_id), "stage": new_stage}

    # ------------------------------------------------------------------
    # 5. 申诉与复核（申诉期间冻结处置）
    # ------------------------------------------------------------------

    def appeal(self, incident_id: str, payload: dict, actor: Actor) -> dict:
        """车主提起申诉。可由车主本人或网格员代为登记。"""
        self._require(actor, Role.OWNER, Role.GRID_WORKER, Role.ADMIN)
        inc = self._get_owned(incident_id, actor)
        reason = (payload or {}).get("reason")
        if not reason or not str(reason).strip():
            raise ValidationError("申诉必须说明理由 reason")
        if inc["stage"] == Stage.CLOSED.value:
            raise ConflictError("已结案事件请走申诉重开通道")
        if inc["owner_status"] != "confirmed":
            raise ConflictError("匿名线索无法提起申诉")
        if self.repo.get_pending_appeal(incident_id) is not None:
            raise ConflictError("已有待复核申诉，请勿重复提交")
        if inc["stage"] == Stage.APPEALED.value:
            raise ConflictError("申诉处理中")
        # 申诉归属车主本人；网格员/管理员仅代登记
        filed_by = inc["owner_id"]
        if actor.role == Role.OWNER.value:
            if inc["owner_id"] != actor.id:
                raise PermissionError_("仅车主本人可申诉")
        elif payload.get("owner_id") and \
                str(payload["owner_id"]) != inc["owner_id"]:
            raise ValidationError("代登记车主与事件登记车主不一致")
        contact = payload.get("contact")
        prior_stage = inc["stage"]
        ts = to_iso(now_utc())
        with self.repo.transaction():
            aid = self.repo.insert_appeal({
                "incident_id": incident_id,
                "reason": str(reason).strip(),
                "filed_by": filed_by,
                "contact": contact,
                "filed_at": ts,
                "prior_stage": prior_stage,
            })
            self.repo.update_incident(
                incident_id,
                {"stage": Stage.APPEALED.value, "updated_at": ts,
                 "version": inc["version"] + 1})
            self.repo.append_chain(
                incident_id, "appealed", _actor_dict(actor), ts,
                detail={"appeal_id": aid, "prior_stage": prior_stage,
                        "contact": contact})
        return {"incident_id": incident_id, "appeal_id": aid,
                "stage": Stage.APPEALED.value}

    def review_appeal(self, incident_id: str, appeal_id: int,
                      payload: dict, actor: Actor) -> dict:
        """管理端复核：upheld（成立→撤销生效处置）或 rejected（驳回→恢复阶段）。"""
        self._require(actor, Role.ADMIN)
        inc = self._require_incident(incident_id)
        decision = (payload or {}).get("decision")
        if decision not in ("upheld", "rejected"):
            raise ValidationError("decision 须为 upheld / rejected")
        basis = self._basis(payload.get("basis_version"), "复核依据版本")
        note = payload.get("note")
        pending = self.repo.get_pending_appeal(incident_id)
        if pending is None or pending["id"] != int(appeal_id):
            raise ConflictError("该申诉不存在或已复核")
        if inc["stage"] != Stage.APPEALED.value:
            raise ConflictError(f"事件当前阶段 {inc['stage']} 无法复核申诉")
        ts = to_iso(now_utc())
        with self.repo.transaction():
            resolved = self.repo.resolve_appeal(
                int(appeal_id), decision, basis, actor.id, ts, note)
            if resolved is None:
                raise ConflictError("申诉已被处理")
            revoked_ids: list[int] = []
            if decision == "upheld":
                # 申诉成立：撤销全部生效处置，逐条保留撤销原因
                for d in self.repo.get_active_disposals(incident_id):
                    self.repo.revoke_disposal(
                        d["id"],
                        f"申诉复核成立：{note or resolved['reason']}",
                        actor.id, ts)
                    revoked_ids.append(d["id"])
                new_stage = Stage.REVOKED.value
            else:
                # 驳回：恢复申诉前阶段（处置仍生效）
                new_stage = pending["prior_stage"] \
                    if pending["prior_stage"] != Stage.APPEALED.value \
                    else Stage.ENFORCED.value
            self.repo.update_incident(
                incident_id,
                {"stage": new_stage, "updated_at": ts,
                 "version": inc["version"] + 1})
            self.repo.append_chain(
                incident_id, "appeal_reviewed", _actor_dict(actor), ts,
                basis_version=basis,
                reason=note,
                detail={"appeal_id": int(appeal_id), "decision": decision,
                        "restored_stage": new_stage,
                        "revoked_disposal_ids": revoked_ids})
        return {"incident_id": incident_id, "appeal_id": int(appeal_id),
                "decision": decision, "stage": new_stage}

    # ------------------------------------------------------------------
    # 6. 结案
    # ------------------------------------------------------------------

    def close(self, incident_id: str, payload: dict, actor: Actor) -> dict:
        self._require(actor, Role.ADMIN)
        inc = self._require_incident(incident_id)
        if inc["stage"] == Stage.CLOSED.value:
            raise ConflictError("事件已结案")
        if inc["stage"] == Stage.APPEALED.value:
            raise ConflictError("存在待复核申诉，不得结案")
        basis = self._basis(payload.get("basis_version"), "结案依据版本")
        ts = to_iso(now_utc())
        with self.repo.transaction():
            self.repo.update_incident(
                incident_id,
                {"stage": Stage.CLOSED.value, "closed_at": ts,
                 "updated_at": ts, "version": inc["version"] + 1})
            self.repo.append_chain(
                incident_id, "closed", _actor_dict(actor), ts,
                basis_version=basis,
                detail={"note": (payload or {}).get("note")})
        return {"incident_id": incident_id, "stage": Stage.CLOSED.value}

    # ------------------------------------------------------------------
    # 7. 查询：管理端工作台 / 事件链 / 详情
    # ------------------------------------------------------------------

    def pending_appeals(self, actor: Actor) -> list[dict]:
        """管理端：待复核申诉列表（含当前阶段、摘要）。"""
        self._require(actor, Role.ADMIN)
        rows = self.repo.list_appeals(status="pending")
        out = []
        for a in rows:
            inc = self.repo.get_incident(a["incident_id"])
            out.append({
                "appeal": views.appeal_view(a, Role.ADMIN.value),
                "incident": views.incident_view(inc, Role.ADMIN.value),
            })
        return out

    def stage_board(self, actor: Actor) -> dict:
        """管理端：各处置阶段计数与明细列表。"""
        self._require(actor, Role.ADMIN)
        counts = self.repo.stage_counts()
        # 确保所有阶段键都在
        board = {s.value: counts.get(s.value, 0) for s in Stage}
        return {"stage_counts": board,
                "total": sum(board.values())}

    def list_incidents(self, actor: Actor, *, stage: Optional[str] = None,
                       limit: int = 100, offset: int = 0) -> dict:
        self._require(actor, Role.ADMIN, Role.GRID_WORKER)
        if stage and stage not in {s.value for s in Stage}:
            raise ValidationError(f"未知阶段: {stage}")
        role = Role.ADMIN if actor.role == Role.ADMIN.value else Role.GRID_WORKER
        rows = self.repo.list_incidents(stage=stage, limit=limit,
                                        offset=offset)
        return {"incidents": [views.incident_view(r, role.value)
                              for r in rows]}

    def get_incident_detail(self, incident_id: str, actor: Actor) -> dict:
        inc = self._get_owned(incident_id, actor)
        role = actor.role
        is_owner = role == Role.OWNER.value
        reports = self.repo.list_reports(incident_id)
        evidences = self.repo.list_evidences(incident_id)
        disposals = self.repo.list_disposals(incident_id)
        appeals = self.repo.list_appeals(incident_id=incident_id)
        chain = self.repo.list_chain(incident_id)
        # 匿名线索对非管理员不暴露任何残留 PII
        if is_owner:
            appeals = [a for a in appeals if a["filed_by"] == actor.id]
        return views.incident_detail(
            inc, reports=reports, evidences=evidences,
            disposals=disposals, appeals=appeals, chain=chain,
            role=role, is_owner=is_owner)

    def get_chain(self, incident_id: str, actor: Actor) -> list[dict]:
        inc = self._get_owned(incident_id, actor)
        return [views.chain_view(c, actor.role)
                for c in self.repo.list_chain(inc["id"])]

    def reconcile(self) -> dict:
        """显式触发重启后的计数自愈（构造时已自动执行一次）。"""
        return self.repo.reconcile()

    # ------------------------------------------------------------------

    def _require_incident(self, incident_id: str) -> dict:
        inc = self.repo.get_incident(incident_id)
        if inc is None:
            raise NotFoundError(f"事件不存在: {incident_id}")
        return inc


# ----------------------------------------------------------------------
# 入参校验小工具
# ----------------------------------------------------------------------

def _actor_dict(actor: Actor) -> dict:
    return {"id": actor.id, "role": actor.role, "name": actor.name}


def _opt_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError) as exc:
        raise ValidationError("坐标必须是数字") from exc


def _valid_lat(v: float) -> bool:
    return -90.0 <= v <= 90.0


def _valid_lng(v: float) -> bool:
    return -180.0 <= v <= 180.0


def _validate_evidences(raw: Any, collected: str) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValidationError("evidences 必须是数组")
    out: list[dict] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValidationError(f"证据 #{i} 必须是对象")
        kind = item.get("kind", "photo")
        if kind not in VALID_EVIDENCE_KINDS:
            raise ValidationError(
                f"证据 #{i} 类型不支持: {kind}")
        if not (item.get("content") or item.get("attachment_uri")):
            raise ValidationError(
                f"证据 #{i} 需提供 content 或 attachment_uri")
        ev = {
            "kind": kind,
            "content": item.get("content"),
            "attachment_uri": item.get("attachment_uri"),
            "source": item.get("source"),
            "reporter_id": item.get("reporter_id"),
            "reporter_name": item.get("reporter_name"),
            "reporter_phone": item.get("reporter_phone"),
            "collected_at": to_iso(parse_dt(item["collected_at"]))
            if item.get("collected_at") else collected,
        }
        out.append(ev)
    return out
