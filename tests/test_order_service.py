"""领域服务测试：事件生命周期、去重、申诉冻结、留痕、隐私裁剪、重启一致性。"""

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from src.models import (
    Actor,
    ConflictError,
    NotFoundError,
    PermissionError_,
    ValidationError,
    now_utc,
)
from src.service import OrderEventService

GW = lambda n="王网格": Actor("gw1", "grid_worker", n)        # noqa: E731
ADMIN = Actor("admin1", "admin", "李管理")
OWNER = lambda oid="own1": Actor(oid, "owner", "张三")        # noqa: E731

LOC = {"location_key": "BLDG3-FIRELANE-A",
       "name": "3号楼东侧消防通道", "lat": 31.23, "lng": 121.47}
LOC_B = {"location_key": "BLDG3-GATE-B", "name": "3号楼西门"}


def report_payload(plate="沪A12345", loc=LOC, *, owner=None, evidences=None):
    p = {
        "subject_type": "plate",
        "subject_id": plate,
        "location": loc,
        "channel": "晚高峰巡查群",
        "reporter_phone": "13800001111",
    }
    if owner is not None:
        p["owner"] = owner
    if evidences is not None:
        p["evidences"] = evidences
    else:
        p["evidences"] = [
            {"kind": "photo",
             "attachment_uri": "oss://evidence/ev-bike-1.jpg",
             "content": "电动车占用消防通道"}
        ]
    return p


CONFIRMED_OWNER = {"id": "own1", "name": "张三",
                   "phone": "13911112222", "id_no": "310101199001011234"}


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.svc = OrderEventService(":memory:")

    def tearDown(self):
        self.svc.shutdown()

    def full_flow(self, *, owner=CONFIRMED_OWNER):
        r = self.svc.report(report_payload(owner=owner), GW())
        iid = r["incident_id"]
        self.svc.notify_owner(iid, {"basis_version": "XFMD-2026.1",
                                    "method": "onsite"}, GW())
        d = self.svc.dispose(iid, {"kind": "fine",
                                   "basis_version": "XFMD-2026.1",
                                   "amount": 50}, ADMIN)
        return iid, d["disposal_id"]


class TestReportDedup(ServiceTestBase):
    def test_first_report_creates_incident(self):
        r = self.svc.report(report_payload(owner=CONFIRMED_OWNER), GW())
        self.assertFalse(r["merged"])
        self.assertTrue(r["incident_id"].startswith("INC-"))
        self.assertEqual(r["incident"]["stage"], "OPEN")
        self.assertEqual(r["incident"]["report_count"], 1)

    def test_repeat_report_same_car_same_spot_links(self):
        r1 = self.svc.report(report_payload(), GW())
        r2 = self.svc.report(report_payload(),
                             Actor("gw2", "grid_worker", "赵网格"))
        self.assertTrue(r2["merged"])
        self.assertEqual(r1["incident_id"], r2["incident_id"])
        inc = self.svc.repo.get_incident(r1["incident_id"])
        self.assertEqual(inc["report_count"], 2)
        chain = self.svc.get_chain(r1["incident_id"], ADMIN)
        self.assertEqual([c["action"] for c in chain][-1], "report_linked")
        # 追加的证据也挂在同一事件
        self.assertEqual(len(self.svc.repo.list_evidences(r1["incident_id"])),
                         2)

    def test_different_location_creates_new_incident(self):
        r1 = self.svc.report(report_payload(loc=LOC), GW())
        r2 = self.svc.report(report_payload(loc=LOC_B), GW())
        self.assertFalse(r2["merged"])
        self.assertNotEqual(r1["incident_id"], r2["incident_id"])

    def test_different_subject_creates_new_incident(self):
        r1 = self.svc.report(report_payload(plate="沪A12345"), GW())
        r2 = self.svc.report(report_payload(plate="沪B67890"), GW())
        self.assertFalse(r2["merged"])

    def test_report_outside_window_creates_new(self):
        self.svc = OrderEventService(":memory:",
                                     dedup_window=timedelta(minutes=30))
        t0 = now_utc()
        r1 = self.svc.report(report_payload(), GW(), collected_at=t0)
        r2 = self.svc.report(
            report_payload(), GW(),
            collected_at=t0 + timedelta(minutes=31))
        self.assertFalse(r2["merged"])

    def test_report_within_window_with_late_arrival_links(self):
        self.svc = OrderEventService(":memory:",
                                     dedup_window=timedelta(minutes=30))
        t0 = now_utc()
        r1 = self.svc.report(report_payload(), GW(),
                             collected_at=t0 - timedelta(minutes=40))
        # 第二次采集时间晚 20 分钟
        r2 = self.svc.report(report_payload(), GW(),
                             collected_at=t0 - timedelta(minutes=20))
        self.assertTrue(r2["merged"])

    def test_closed_incident_not_relinked(self):
        iid, _ = self.full_flow()
        self.svc.close(iid, {"basis_version": "XFMD-2026.1"}, ADMIN)
        r = self.svc.report(report_payload(), GW())
        self.assertFalse(r["merged"])
        self.assertNotEqual(iid, r["incident_id"])

    def test_grid_worker_cannot_use_unknown_role(self):
        with self.assertRaises(PermissionError_):
            Actor("x", "superuser")


class TestAnonymousLead(ServiceTestBase):
    def test_no_owner_creates_lead(self):
        r = self.svc.report(report_payload(owner=None), GW())
        self.assertEqual(r["incident"]["stage"], "LEAD")
        self.assertEqual(r["incident"]["owner_status"], "anonymous")

    def test_lead_cannot_notify_or_dispose(self):
        r = self.svc.report(report_payload(owner=None), GW())
        iid = r["incident_id"]
        with self.assertRaises(ConflictError):
            self.svc.notify_owner(iid, {"basis_version": "X-2026.1"}, GW())
        with self.assertRaises(ConflictError):
            self.svc.dispose(iid, {"kind": "fine",
                                   "basis_version": "X-2026.1"}, ADMIN)

    def test_identify_owner_then_flow_proceeds(self):
        r = self.svc.report(report_payload(owner=None), GW())
        iid = r["incident_id"]
        out = self.svc.identify_owner(iid, {"owner": CONFIRMED_OWNER}, GW())
        self.assertEqual(out["stage"], "OPEN")
        # 直接补登记车主再告知，不再产生匿名处罚
        self.svc.notify_owner(iid, {"basis_version": "X-2026.1"}, GW())
        self.assertEqual(self.svc.repo.get_incident(iid)["stage"],
                         "NOTIFIED")

    def test_identify_requires_owner_id(self):
        r = self.svc.report(report_payload(owner=None), GW())
        with self.assertRaises(ValidationError):
            self.svc.identify_owner(r["incident_id"],
                                    {"owner": {"name": "张三"}}, GW())


class TestNotifyAndDispose(ServiceTestBase):
    def test_dispose_requires_notification(self):
        r = self.svc.report(report_payload(owner=CONFIRMED_OWNER), GW())
        with self.assertRaises(ConflictError):
            self.svc.dispose(r["incident_id"],
                             {"kind": "fine",
                              "basis_version": "X-2026.1"}, ADMIN)

    def test_dispose_requires_basis_version(self):
        r = self.svc.report(report_payload(owner=CONFIRMED_OWNER), GW())
        iid = r["incident_id"]
        self.svc.notify_owner(iid, {"basis_version": "X-2026.1"}, GW())
        with self.assertRaises(ValidationError):
            self.svc.dispose(iid, {"kind": "fine"}, ADMIN)

    def test_only_admin_disposes(self):
        r = self.svc.report(report_payload(owner=CONFIRMED_OWNER), GW())
        iid = r["incident_id"]
        self.svc.notify_owner(iid, {"basis_version": "X-2026.1"}, GW())
        with self.assertRaises(PermissionError_):
            self.svc.dispose(iid, {"kind": "fine",
                                   "basis_version": "X-2026.1"}, GW())

    def test_disposal_records_basis_handler_time(self):
        iid, did = self.full_flow()
        d = self.svc.repo.list_disposals(iid)[0]
        self.assertEqual(d["basis_version"], "XFMD-2026.1")
        self.assertEqual(d["handler_id"], "admin1")
        self.assertEqual(d["handler_name"], "李管理")
        self.assertIsNotNone(d["decided_at"])
        self.assertEqual(d["status"], "active")

    def test_duplicate_same_kind_disposal_blocked(self):
        iid, _ = self.full_flow()
        with self.assertRaises(ConflictError):
            self.svc.dispose(iid, {"kind": "fine",
                                   "basis_version": "XFMD-2026.1",
                                   "amount": 100}, ADMIN)
        # 原处罚仍只有一条 —— 不会被重复处罚
        self.assertEqual(
            len([d for d in self.svc.repo.list_disposals(iid)
                 if d["status"] == "active"]), 1)

    def test_different_kind_disposal_allowed(self):
        iid, _ = self.full_flow()
        d2 = self.svc.dispose(iid, {"kind": "warning",
                                    "basis_version": "XFMD-2026.1"}, ADMIN)
        self.assertTrue(d2["disposal_id"])

    def test_invalid_kind_rejected(self):
        r = self.svc.report(report_payload(owner=CONFIRMED_OWNER), GW())
        iid = r["incident_id"]
        self.svc.notify_owner(iid, {"basis_version": "X-2026.1"}, GW())
        with self.assertRaises(ValidationError):
            self.svc.dispose(iid, {"kind": "confiscate",
                                   "basis_version": "X-2026.1"}, ADMIN)


class TestRevoke(ServiceTestBase):
    def test_revoke_requires_reason(self):
        iid, did = self.full_flow()
        with self.assertRaises(ValidationError):
            self.svc.revoke_disposal(iid, did,
                                     {"basis_version": "XFMD-2026.1"}, ADMIN)

    def test_revoke_keeps_record_and_reason(self):
        iid, did = self.full_flow()
        self.svc.revoke_disposal(
            iid, did,
            {"basis_version": "XFMD-2026.2",
             "reason": "证据显示车辆当时已搬离，处罚依据不足"}, ADMIN)
        d = self.svc.repo.list_disposals(iid)[0]
        self.assertEqual(d["status"], "revoked")
        self.assertIn("证据显示", d["revoke_reason"])
        self.assertEqual(d["revoked_by"], "admin1")
        self.assertIsNotNone(d["revoked_at"])
        # 原处置记录仍在（保留 kind/basis/decided_at）
        self.assertEqual(d["kind"], "fine")
        self.assertEqual(self.svc.repo.get_incident(iid)["stage"], "REVOKED")

    def test_double_revoke_conflict(self):
        iid, did = self.full_flow()
        self.svc.revoke_disposal(
            iid, did, {"basis_version": "X-2026.1", "reason": "误录"}, ADMIN)
        with self.assertRaises(ConflictError):
            self.svc.revoke_disposal(
                iid, did, {"basis_version": "X-2026.1", "reason": "再撤"},
                ADMIN)

    def test_revoke_wrong_incident_rejected(self):
        iid, did = self.full_flow()
        other = self.svc.report(
            report_payload(plate="沪F00000", loc=LOC_B,
                           owner=CONFIRMED_OWNER), GW())
        with self.assertRaises(ValidationError):
            self.svc.revoke_disposal(other["incident_id"], did,
                                     {"reason": "x",
                                      "basis_version": "X-2026.1"}, ADMIN)


class TestAppealFreeze(ServiceTestBase):
    def _appealed(self):
        iid, did = self.full_flow()
        a = self.svc.appeal(iid, {"reason": "当晚车辆系他人借用，已搬离",
                                  "contact": "13911112222"}, OWNER())
        return iid, did, a["appeal_id"]

    def test_appeal_sets_stage_and_pending(self):
        iid, _, aid = self._appealed()
        self.assertEqual(self.svc.repo.get_incident(iid)["stage"],
                         "APPEALED")
        pending = self.svc.pending_appeals(ADMIN)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["appeal"]["id"], aid)

    def test_no_dispose_during_appeal(self):
        iid, _, _ = self._appealed()
        with self.assertRaises(ConflictError):
            self.svc.dispose(iid, {"kind": "tow",
                                   "basis_version": "XFMD-2026.1"}, ADMIN)

    def test_no_notify_or_close_during_appeal(self):
        iid, _, _ = self._appealed()
        with self.assertRaises(ConflictError):
            self.svc.notify_owner(iid, {"basis_version": "X-2026.1"}, GW())
        with self.assertRaises(ConflictError):
            self.svc.close(iid, {"basis_version": "X-2026.1"}, ADMIN)

    def test_duplicate_appeal_blocked(self):
        iid, _, _ = self._appealed()
        with self.assertRaises(ConflictError):
            self.svc.appeal(iid, {"reason": "再说一次"}, OWNER())

    def test_non_owner_cannot_appeal_someone_else(self):
        iid, _ = self.full_flow()
        with self.assertRaises(NotFoundError):
            self.svc.appeal(iid, {"reason": "我不服"},
                            Actor("other-person", "owner"))

    def test_anonymous_lead_has_no_appeal(self):
        r = self.svc.report(report_payload(owner=None), GW())
        with self.assertRaises(NotFoundError):
            self.svc.appeal(r["incident_id"], {"reason": "?"}, OWNER())

    def test_review_upheld_revokes_disposals(self):
        iid, did, aid = self._appealed()
        out = self.svc.review_appeal(
            iid, aid,
            {"decision": "upheld", "basis_version": "XFMD-2026.2",
             "note": "监控证实申诉属实"}, ADMIN)
        self.assertEqual(out["stage"], "REVOKED")
        d = self.svc.repo.list_disposals(iid)[0]
        self.assertEqual(d["status"], "revoked")
        self.assertIn("申诉复核成立", d["revoke_reason"])
        self.assertEqual(d["basis_version"], "XFMD-2026.1")  # 原依据保留
        # 复核后可重新处置
        self.svc.notify_owner(iid, {"basis_version": "XFMD-2026.2"}, GW())

    def test_review_rejected_restores_enforced(self):
        iid, did, aid = self._appealed()
        out = self.svc.review_appeal(
            iid, aid,
            {"decision": "rejected", "basis_version": "XFMD-2026.2",
             "note": "照片清晰，申诉不成立"}, ADMIN)
        self.assertEqual(out["stage"], "ENFORCED")
        self.assertEqual(
            len(self.svc.repo.get_active_disposals(iid)), 1)
        # 驳回后冻结解除
        self.svc.close(iid, {"basis_version": "XFMD-2026.1"}, ADMIN)

    def test_only_admin_reviews(self):
        iid, _, aid = self._appealed()
        with self.assertRaises(PermissionError_):
            self.svc.review_appeal(
                iid, aid, {"decision": "upheld",
                           "basis_version": "X-2026.1"}, GW())

    def test_grid_worker_can_file_appeal_on_behalf(self):
        iid, _ = self.full_flow()
        a = self.svc.appeal(iid, {"reason": "车主现场口头申诉，代为登记",
                                  "owner_id": "own1"}, GW())
        self.assertEqual(a["stage"], "APPEALED")

    def test_review_requires_basis_version(self):
        iid, _, aid = self._appealed()
        with self.assertRaises(ValidationError):
            self.svc.review_appeal(iid, aid,
                                   {"decision": "upheld"}, ADMIN)


class TestClose(ServiceTestBase):
    def test_close_flow(self):
        iid, _ = self.full_flow()
        out = self.svc.close(iid, {"basis_version": "XFMD-2026.1",
                                   "note": "罚款已执行"}, ADMIN)
        self.assertEqual(out["stage"], "CLOSED")
        inc = self.svc.repo.get_incident(iid)
        self.assertIsNotNone(inc["closed_at"])

    def test_evidence_after_close_rejected(self):
        iid, _ = self.full_flow()
        self.svc.close(iid, {"basis_version": "X-2026.1"}, ADMIN)
        with self.assertRaises(ConflictError):
            self.svc.add_evidence(
                iid, {"kind": "note", "content": "补充"}, GW())

    def test_append_evidence_before_close(self):
        r = self.svc.report(report_payload(owner=CONFIRMED_OWNER), GW())
        iid = r["incident_id"]
        out = self.svc.add_evidence(
            iid, {"evidences": [
                {"kind": "chat_record", "source": "整改反馈群",
                 "content": "车主已搬离并承诺不再占用"}]},
            Actor("gw3", "grid_worker"))
        self.assertEqual(len(out["evidence_ids"]), 1)
        self.assertEqual(
            len(self.svc.repo.list_evidences(iid)), 2)


class TestPrivacyViews(ServiceTestBase):
    def setUp(self):
        super().setUp()
        self.iid, _ = self.full_flow()
        self.svc.appeal(self.iid,
                        {"reason": "申诉", "contact": "13911112222"},
                        OWNER())

    def test_admin_sees_all(self):
        d = self.svc.get_incident_detail(self.iid, ADMIN)
        self.assertEqual(d["owner_phone"], "13911112222")
        self.assertEqual(d["owner_id_no"], "310101199001011234")
        self.assertEqual(d["reports"][0]["reporter_phone"], "13800001111")

    def test_grid_worker_sees_name_but_not_contact_details(self):
        d = self.svc.get_incident_detail(self.iid, GW())
        self.assertEqual(d["owner_name"], "张三")
        self.assertEqual(d["owner_phone"], "***")
        self.assertEqual(d["owner_id_no"], "***")
        # 上报人电话脱敏
        self.assertNotEqual(d["reports"][0].get("reporter_phone"),
                            "13800001111")

    def test_owner_view_hides_other_reporters(self):
        d = self.svc.get_incident_detail(self.iid, OWNER())
        self.assertEqual(d["owner_phone"], "13911112222")  # 本人信息可见
        self.assertNotIn("reporter_id", d["reports"][0])
        self.assertNotIn("reporter_name", d["reports"][0])
        # 但处置依据、经办人仍可见，支撑其申诉
        self.assertEqual(d["disposals"][0]["basis_version"], "XFMD-2026.1")

    def test_other_owner_gets_not_found(self):
        with self.assertRaises(NotFoundError):
            self.svc.get_incident_detail(self.iid,
                                         Actor("own2", "owner", "李四"))

    def test_owner_cannot_list_all(self):
        with self.assertRaises(PermissionError_):
            self.svc.list_incidents(OWNER())

    def test_grid_worker_cannot_review_board(self):
        with self.assertRaises(PermissionError_):
            self.svc.pending_appeals(GW())


class TestAdminBoard(ServiceTestBase):
    def test_stage_board_counts(self):
        iid, _ = self.full_flow()  # ENFORCED
        self.svc.report(report_payload(plate="沪C11111", owner=None), GW())
        board = self.svc.stage_board(ADMIN)
        self.assertEqual(board["stage_counts"]["ENFORCED"], 1)
        self.assertEqual(board["stage_counts"]["LEAD"], 1)
        self.assertEqual(board["total"], 2)

    def test_chain_ordered_and_append_only(self):
        iid, _ = self.full_flow()
        chain = self.svc.get_chain(iid, ADMIN)
        actions = [c["action"] for c in chain]
        self.assertEqual(actions, ["created", "notified", "disposed"])
        seqs = [c["seq"] for c in chain]
        self.assertEqual(seqs, sorted(seqs))
        # 处置节点三要素
        disposed = chain[-1]
        self.assertEqual(disposed["basis_version"], "XFMD-2026.1")
        self.assertEqual(disposed["actor_id"], "admin1")
        self.assertIsNotNone(disposed["at"])


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "events.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _seed(self):
        svc = OrderEventService(self.db)
        r = svc.report(report_payload(owner=CONFIRMED_OWNER), GW())
        iid = r["incident_id"]
        svc.notify_owner(iid, {"basis_version": "X-2026.1"}, GW())
        svc.dispose(iid, {"kind": "fine", "basis_version": "X-2026.1"},
                    ADMIN)
        svc.report(report_payload(), GW())  # merged second report
        svc.report(report_payload(plate="沪D99999", owner=None), GW())
        svc.shutdown()
        return iid

    def test_restart_preserves_stage_counts_and_chain(self):
        iid = self._seed()
        svc2 = OrderEventService(self.db)
        inc = svc2.repo.get_incident(iid)
        self.assertEqual(inc["stage"], "ENFORCED")
        self.assertEqual(inc["report_count"], 2)
        self.assertEqual(inc["active_disposal_count"], 1)
        chain = svc2.get_chain(iid, ADMIN)
        self.assertEqual([c["action"] for c in chain],
                         ["created", "notified", "disposed",
                          "report_linked"])
        board = svc2.stage_board(ADMIN)
        self.assertEqual(board["stage_counts"]["LEAD"], 1)
        svc2.shutdown()

    def test_restart_reconciles_corrupted_counts(self):
        iid = self._seed()
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE incidents SET report_count=99, "
                     "active_disposal_count=99 WHERE id=?", (iid,))
        conn.commit()
        conn.close()

        svc2 = OrderEventService(self.db)
        inc = svc2.repo.get_incident(iid)
        self.assertEqual(inc["report_count"], 2)
        self.assertEqual(inc["active_disposal_count"], 1)
        svc2.shutdown()

    def test_id_continues_after_restart(self):
        iid = self._seed()
        svc2 = OrderEventService(self.db)
        r = svc2.report(
            report_payload(plate="沪E55555",
                           loc={"location_key": "NEW-SPOT"}), GW())
        # 已有 INC-000001、INC-000002，新事件应为 INC-000003
        self.assertEqual(r["incident_id"], "INC-000003")
        svc2.shutdown()

    def test_explicit_reconcile_idempotent(self):
        self._seed()
        svc2 = OrderEventService(self.db)
        first = svc2.reconcile()
        second = svc2.reconcile()
        self.assertEqual(second["report_count_fixed"], 0)
        self.assertTrue(all(v >= 0 for v in first.values()))
        svc2.shutdown()


class TestValidation(ServiceTestBase):
    def test_missing_subject(self):
        p = report_payload()
        p.pop("subject_id")
        with self.assertRaises(ValidationError):
            self.svc.report(p, GW())

    def test_missing_location(self):
        p = report_payload()
        p.pop("location")
        with self.assertRaises(ValidationError):
            self.svc.report(p, GW())

    def test_bad_plate(self):
        with self.assertRaises(ValidationError):
            self.svc.report(report_payload(plate="x"), GW())

    def test_evidence_needs_content(self):
        p = report_payload()
        p["evidences"] = [{"kind": "photo"}]
        with self.assertRaises(ValidationError):
            self.svc.report(p, GW())

    def test_unknown_incident(self):
        with self.assertRaises(NotFoundError):
            self.svc.notify_owner("INC-000999",
                                  {"basis_version": "X-2026.1"}, GW())


if __name__ == "__main__":
    unittest.main()
