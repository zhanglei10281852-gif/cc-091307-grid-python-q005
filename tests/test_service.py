"""核心业务规则测试。"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from src.errors import ConflictError, StageError, ValidationError
from src.service import OrderService
from src.storage import JsonStore
from src.views import serialize_event

BASE = datetime(2026, 9, 21, 17, 30, 0, tzinfo=timezone(timedelta(hours=8)))


class Clock:
    def __init__(self, start=BASE):
        self.t = start

    def __call__(self):
        return self.t.isoformat(timespec="seconds")

    def at(self, minute_offset):
        return (self.t + timedelta(minutes=minute_offset)).isoformat(
            timespec="seconds"
        )


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "orderdb.json")
        self.clock = Clock()
        self.svc = OrderService(
            JsonStore(self.db), merge_window_minutes=30, clock=self.clock
        )

    def tearDown(self):
        self.tmp.cleanup()

    def restart(self):
        """模拟进程重启：用同一数据文件重建服务。"""
        self.svc = OrderService(
            JsonStore(self.db), merge_window_minutes=30, clock=self.clock
        )
        return self.svc


class EventCreationTests(ServiceTestBase):
    def test_create_with_plate_normalizes_id(self):
        event, created = self.svc.create_or_report({
            "subject_type": "plate",
            "subject_id": "沪a 12345",
            "location": "3 号楼消防通道",
            "location_code": "B3-FIRE",
            "reporter": "网格员甲",
            "source_chat": "晚高峰巡查群",
        })
        self.assertTrue(created)
        self.assertEqual(event.subject_id, "沪A12345")
        self.assertEqual(event.stage, "collecting")
        self.assertEqual(len(event.reports), 1)

    def test_duplicate_same_car_same_spot_within_window_is_merged(self):
        payload = {
            "subject_type": "plate", "subject_id": "沪A12345",
            "location_code": "B3-FIRE", "location": "消防通道",
            "reporter": "网格员甲",
        }
        first, created1 = self.svc.create_or_report(
            {**payload, "reported_at": self.clock.at(0)})
        # 不同群聊、十分钟后、同车同点的重复上报
        second, created2 = self.svc.create_or_report({
            **payload, "reporter": "网格员乙",
            "source_chat": "物业群", "reported_at": self.clock.at(10),
        })
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(first.event_id, second.event_id)
        self.assertEqual(len(second.reports), 2)
        self.assertEqual(self.svc.stats()["events_total"], 1)
        self.assertEqual(self.svc.stats()["reports_total"], 2)

    def test_different_spot_creates_new_event(self):
        common = {"subject_type": "plate", "subject_id": "沪A12345",
                  "reporter": "网格员甲", "reported_at": self.clock.at(0)}
        e1, _ = self.svc.create_or_report({**common, "location_code": "A"})
        e2, created = self.svc.create_or_report({**common, "location_code": "B"})
        self.assertTrue(created)
        self.assertNotEqual(e1.event_id, e2.event_id)

    def test_outside_window_creates_new_event(self):
        common = {"subject_type": "plate", "subject_id": "沪A12345",
                  "location_code": "A", "reporter": "网格员甲"}
        self.svc.create_or_report({**common, "reported_at": self.clock.at(0)})
        e2, created = self.svc.create_or_report(
            {**common, "reported_at": self.clock.at(45)})
        self.assertTrue(created)

    def test_evidence_from_different_chats_collects_on_one_event(self):
        common = {"subject_type": "plate", "subject_id": "沪A12345",
                  "location_code": "A", "reporter": "网格员甲"}
        self.svc.create_or_report({
            **common, "reported_at": self.clock.at(0),
            "source_chat": "巡查群",
            "evidence": [{"kind": "photo", "uri": "chat1://p1.jpg"}],
        })
        event, created = self.svc.create_or_report({
            **common, "reported_at": self.clock.at(5),
            "source_chat": "物业群",
            "evidence": [{"kind": "photo", "uri": "chat2://p2.jpg"}],
        })
        self.assertFalse(created)
        self.assertEqual(len(event.evidence), 2)
        self.assertEqual({e.source_chat for e in event.evidence},
                         {"巡查群", "物业群"})


class AnonymousClueTests(ServiceTestBase):
    def test_unidentified_subject_becomes_anonymous_clue(self):
        event, created = self.svc.create_or_report({
            "location_code": "A", "location": "消防通道",
            "reporter": "网格员甲", "anonymous": True,
            "evidence": [{"kind": "photo", "uri": "blob://blur.jpg"}],
        })
        self.assertTrue(created)
        self.assertEqual(event.stage, "clue")
        self.assertIsNone(event.subject_id)
        self.assertTrue(event.is_anonymous)

    def test_missing_identifier_requires_explicit_anonymous(self):
        with self.assertRaises(ValidationError):
            self.svc.create_or_report({
                "subject_type": "plate", "subject_id": "  ",
                "location_code": "A", "reporter": "网格员甲",
            })

    def test_clue_cannot_be_notified_enforced_or_appealed(self):
        event, _ = self.svc.create_or_report({
            "location_code": "A", "reporter": "网格员甲", "anonymous": True})
        body = {"operator": "甲", "basis_version": "规约-2026-v1"}
        with self.assertRaises(StageError):
            self.svc.notify(event.event_id, body)
        with self.assertRaises(StageError):
            self.svc.enforce(event.event_id, {**body, "measure": "拖移"})
        with self.assertRaises(StageError):
            self.svc.file_appeal(event.event_id, {
                "reason": "x", "appellant": "x", "contact": "13800000000"})

    def test_identify_clue_converts_to_formal_event(self):
        event, _ = self.svc.create_or_report({
            "location_code": "A", "reported_at": self.clock.at(0),
            "reporter": "网格员甲", "anonymous": True})
        converted, merged = self.svc.identify_clue(event.event_id, {
            "subject_type": "plate", "subject_id": "沪A12345",
            "owner_name": "张三", "operator": "网格员甲",
        })
        self.assertFalse(merged)
        self.assertEqual(converted.stage, "collecting")
        self.assertEqual(converted.subject_id, "沪A12345")

    def test_identified_clue_merges_into_existing_event(self):
        # 同一辆已建事件的车，先有无主照片线索
        formal, _ = self.svc.create_or_report({
            "subject_type": "plate", "subject_id": "沪A12345",
            "location_code": "A", "reported_at": self.clock.at(0),
            "reporter": "网格员甲"})
        clue, _ = self.svc.create_or_report({
            "location_code": "A", "reported_at": self.clock.at(5),
            "reporter": "网格员乙", "anonymous": True})
        target, merged = self.svc.identify_clue(clue.event_id, {
            "subject_type": "plate", "subject_id": "沪A12345",
            "operator": "网格员乙"})
        self.assertTrue(merged)
        self.assertEqual(target.event_id, formal.event_id)
        self.assertEqual(len(target.reports), 2)
        self.assertIn(clue.event_id, target.merged_clue_ids)
        # 线索本身被标记为已并入
        self.assertEqual(self.svc.get_event(clue.event_id).stage, "closed")


class WorkflowTests(ServiceTestBase):
    def _full_event(self):
        event, _ = self.svc.create_or_report({
            "subject_type": "plate", "subject_id": "沪A12345",
            "location_code": "A", "reporter": "网格员甲",
            "reported_at": self.clock.at(0)})
        return event

    def test_notify_requires_basis_version(self):
        event = self._full_event()
        with self.assertRaises(ValidationError):
            self.svc.notify(event.event_id, {"operator": "甲"})

    def test_full_lifecycle_notify_enforce_close(self):
        event = self._full_event()
        self.svc.notify(event.event_id,
                        {"operator": "甲", "basis_version": "规约-2026-v1"})
        self.assertEqual(event.stage, "notified")
        # 未告知不能处置由状态机保证（另一辆车直接验证）
        fresh, _ = self.svc.create_or_report({
            "subject_type": "plate", "subject_id": "沪B00000",
            "location_code": "B", "reporter": "网格员丙"})
        with self.assertRaises(StageError):
            self.svc.enforce(fresh.event_id, {
                "operator": "乙", "basis_version": "消防法-2021-v1",
                "measure": "罚款"})
        self.svc.enforce(event.event_id, {
            "operator": "乙", "basis_version": "消防法-2021-v1",
            "measure": "现场搬离+罚款"})
        self.assertEqual(event.stage, "enforced")
        self.svc.close(event.event_id,
                       {"operator": "主管", "basis_version": "规约-2026-v1"})
        self.assertEqual(event.stage, "closed")
        # 每个动作都有依据版本、经办人、时间
        for action in event.actions:
            self.assertTrue(action.operator)
            self.assertTrue(action.at)
            self.assertTrue(action.basis_version)

    def test_double_enforcement_is_blocked(self):
        event = self._full_event()
        body = {"operator": "甲", "basis_version": "v1"}
        self.svc.notify(event.event_id, body)
        self.svc.enforce(event.event_id, {**body, "measure": "罚款"})
        with self.assertRaises(ConflictError):
            self.svc.enforce(event.event_id, {**body, "measure": "再次罚款"})

    def test_appeal_freezes_enforcement(self):
        event = self._full_event()
        self.svc.notify(event.event_id,
                        {"operator": "甲", "basis_version": "v1"})
        self.svc.file_appeal(event.event_id, {
            "reason": "当时车辆借予他人且已挪车",
            "appellant": "张三", "contact": "13800000000"})
        self.assertEqual(event.stage, "appealing")
        with self.assertRaises(StageError):
            self.svc.enforce(event.event_id,
                             {"operator": "甲", "basis_version": "v1",
                              "measure": "罚款"})
        with self.assertRaises(StageError):
            self.svc.close(event.event_id,
                           {"operator": "甲", "basis_version": "v1"})
        self.assertEqual(len(self.svc.pending_appeals()), 1)

    def test_appeal_rejected_resumes_stage(self):
        event = self._full_event()
        self.svc.notify(event.event_id,
                        {"operator": "甲", "basis_version": "v1"})
        appeal = self.svc.file_appeal(event.event_id, {
            "reason": "照片模糊", "appellant": "张三",
            "contact": "13800000000"})
        result = self.svc.review_appeal(event.event_id, appeal.appeal_id, {
            "operator": "复核员", "basis_version": "v2",
            "decision": "rejected", "comment": "证据充分"})
        self.assertEqual(result.status, "rejected")
        self.assertEqual(event.stage, "notified")
        self.assertEqual(self.svc.pending_appeals(), [])
        # 恢复后可以正常处置
        self.svc.enforce(event.event_id,
                         {"operator": "甲", "basis_version": "v1",
                          "measure": "罚款"})

    def test_appeal_upheld_after_enforcement_revokes_with_reason(self):
        event = self._full_event()
        self.svc.notify(event.event_id,
                        {"operator": "甲", "basis_version": "v1"})
        self.svc.enforce(event.event_id,
                         {"operator": "甲", "basis_version": "v1",
                          "measure": "罚款"})
        appeal = self.svc.file_appeal(event.event_id, {
            "reason": "车牌被套牌", "appellant": "张三",
            "contact": "13800000000"})
        self.svc.review_appeal(event.event_id, appeal.appeal_id, {
            "operator": "复核员", "basis_version": "v2", "decision": "upheld"})
        self.assertEqual(event.stage, "revoked")
        revocation = event.revocation
        self.assertIsNotNone(revocation)
        self.assertIn("申诉成立", revocation.detail["reason"])
        self.assertIn("车牌被套牌", revocation.detail["reason"])
        self.assertEqual(revocation.basis_version, "v2")
        # 撤销后不再计入有效处置
        self.assertEqual(self.svc.stats()["enforced_active"], 0)

    def test_manual_revoke_requires_reason(self):
        event = self._full_event()
        self.svc.notify(event.event_id,
                        {"operator": "甲", "basis_version": "v1"})
        self.svc.enforce(event.event_id,
                         {"operator": "甲", "basis_version": "v1",
                          "measure": "罚款"})
        with self.assertRaises(ValidationError):
            self.svc.revoke(event.event_id,
                            {"operator": "主管", "basis_version": "v1"})
        self.svc.revoke(event.event_id, {
            "operator": "主管", "basis_version": "v1",
            "reason": "核实为系统重复录入"})
        self.assertEqual(event.stage, "revoked")

    def test_duplicate_appeal_blocked(self):
        event = self._full_event()
        body = {"reason": "r", "appellant": "张三", "contact": "138"}
        self.svc.file_appeal(event.event_id, body)
        with self.assertRaises(ConflictError):
            self.svc.file_appeal(event.event_id, body)

    def test_review_already_reviewed_fails(self):
        event = self._full_event()
        appeal = self.svc.file_appeal(event.event_id, {
            "reason": "r", "appellant": "张三", "contact": "138"})
        decision = {"operator": "复核员", "basis_version": "v1",
                    "decision": "rejected"}
        self.svc.review_appeal(event.event_id, appeal.appeal_id, decision)
        with self.assertRaises(ConflictError):
            self.svc.review_appeal(event.event_id, appeal.appeal_id, decision)


class ChainAndPersistenceTests(ServiceTestBase):
    def test_event_chain_is_time_ordered(self):
        event, _ = self.svc.create_or_report({
            "subject_type": "plate", "subject_id": "沪A12345",
            "location_code": "A", "reporter": "网格员甲",
            "reported_at": self.clock.at(0),
            "evidence": [{"kind": "photo", "uri": "p1.jpg",
                          "collected_at": self.clock.at(1)}]})
        self.clock.t += timedelta(minutes=5)
        self.svc.notify(event.event_id,
                        {"operator": "甲", "basis_version": "v1"})
        chain = self.svc.event_chain(event.event_id)
        kinds = [item["kind"] for item in chain]
        self.assertEqual(kinds, ["report", "evidence", "action"])

    def test_restart_keeps_state_and_consistent_counts(self):
        event, _ = self.svc.create_or_report({
            "subject_type": "plate", "subject_id": "沪A12345",
            "location_code": "A", "reporter": "网格员甲"})
        self.svc.notify(event.event_id,
                        {"operator": "甲", "basis_version": "v1"})
        appeal = self.svc.file_appeal(event.event_id, {
            "reason": "r", "appellant": "张三", "contact": "138"})
        before = self.svc.stats()

        svc2 = self.restart()
        restored = svc2.get_event(event.event_id)
        self.assertEqual(restored.stage, "appealing")
        self.assertEqual(restored.stage_before_appeal, "notified")
        self.assertIsNotNone(restored.open_appeal)
        self.assertEqual(restored.appeals[0].appeal_id, appeal.appeal_id)
        self.assertEqual(svc2.stats(), before)
        self.assertEqual(len(svc2.pending_appeals()), 1)
        # 重启后流程可继续，且申诉期间处置仍被冻结
        with self.assertRaises(StageError):
            svc2.enforce(event.event_id,
                         {"operator": "甲", "basis_version": "v1",
                          "measure": "罚款"})
        # 序列号也持续递增，不会与已有 ID 冲突
        ev2, created = svc2.create_or_report({
            "subject_type": "plate", "subject_id": "沪B99999",
            "location_code": "Z", "reporter": "甲"})
        self.assertTrue(created)
        self.assertNotEqual(ev2.event_id, event.event_id)


class RoleViewTests(ServiceTestBase):
    def _event(self):
        event, _ = self.svc.create_or_report({
            "subject_type": "plate", "subject_id": "沪A12345",
            "location_code": "A", "reporter": "网格员甲",
            "source_chat": "巡查群",
            "evidence": [{"kind": "photo", "uri": "p1.jpg"}],
        })
        event.owner_name = "张三丰"
        event.owner_contact = "13812345678"
        return event

    def test_reviewer_sees_full_pii(self):
        event = self._event()
        view = serialize_event(event, "reviewer")
        self.assertEqual(view["owner_name"], "张三丰")
        self.assertEqual(view["owner_contact"], "13812345678")
        self.assertEqual(view["reports"][0]["reporter"], "网格员甲")

    def test_owner_view_masks_staff_info(self):
        event = self._event()
        view = serialize_event(event, "owner")
        self.assertTrue(view["owner_name"].startswith("张"))
        self.assertIn("*", view["owner_name"])
        self.assertTrue(view["owner_contact"].endswith("5678"))
        self.assertNotIn("网格员甲", str(view["reports"]))
        self.assertNotIn("巡查群", str(view))

    def test_anonymous_view_minimal(self):
        event = self._event()
        view = serialize_event(event, "anonymous")
        self.assertNotIn("owner_name", view)
        self.assertNotIn("owner_contact", view)
        self.assertIn("reports_count", view)
        self.assertNotIn("reports", view)


if __name__ == "__main__":
    unittest.main()
