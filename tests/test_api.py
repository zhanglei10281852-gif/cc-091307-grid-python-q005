"""HTTP 接口与角色权限测试。"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from src.api import build_server

BASE = datetime(2026, 9, 21, 17, 30, tzinfo=timezone(timedelta(hours=8)))


class FakeClock:
    def __init__(self):
        self.t = BASE

    def __call__(self):
        return self.t.isoformat(timespec="seconds")


class ApiTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "db.json")
        self.server = build_server(self.db, port=0, clock=FakeClock())
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def request(self, method, path, body=None, role=None, operator=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None
        headers = {"Content-Type": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if role:
            headers["X-Role"] = role
        if operator:
            headers["X-Operator"] = operator
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    def report(self, **overrides):
        body = {
            "subject_type": "plate",
            "subject_id": "沪A12345",
            "location_code": "B3-FIRE",
            "location": "B3消防通道",
            "reporter": "网格员甲",
        }
        body.update(overrides)
        return self.request("POST", "/v1/events", body, role="grid")


class HttpApiTests(ApiTestBase):
    def test_report_and_merge_via_http(self):
        status1, resp1 = self.report(source_chat="巡查群")
        self.assertEqual(status1, 201)
        self.assertTrue(resp1["created"])
        status2, resp2 = self.report(source_chat="物业群", reporter="网格员乙")
        self.assertEqual(status2, 200)
        self.assertFalse(resp2["created"])
        self.assertEqual(resp1["event"]["event_id"], resp2["event"]["event_id"])

    def test_role_forbidden(self):
        # anonymous 不能上报
        status, _ = self.request("POST", "/v1/events",
                                 {"location": "x", "reporter": "甲"})
        self.assertEqual(status, 403)
        # 网格员不能复核
        status, _ = self.request(
            "POST", "/v1/events/evt-x/appeals/apd-x/review",
            {"decision": "rejected", "basis_version": "v1"}, role="grid")
        self.assertEqual(status, 403)

    def test_pending_appeals_reviewer_only(self):
        status, _ = self.request("GET", "/v1/appeals/pending")
        self.assertEqual(status, 403)
        status, body = self.request("GET", "/v1/appeals/pending", role="reviewer")
        self.assertEqual(status, 200)
        self.assertEqual(body, [])

    def test_full_flow_with_freeze_and_review(self):
        _, resp = self.report()
        eid = resp["event"]["event_id"]

        status, _ = self.request("POST", f"/v1/events/{eid}/notify", {
            "basis_version": "规约-2026-v1", "operator": "网格员甲"}, role="grid")
        self.assertEqual(status, 201)

        # 车主申诉
        status, appeal = self.request("POST", f"/v1/events/{eid}/appeals", {
            "reason": "照片非本人车辆", "appellant": "张三",
            "contact": "13812345678"}, role="owner")
        self.assertEqual(status, 201)

        # 申诉期间网格员处置被冻结
        status, err = self.request("POST", f"/v1/events/{eid}/enforce", {
            "basis_version": "v1", "measure": "罚款", "operator": "网格员甲"},
            role="grid")
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "invalid_stage")

        # 管理端看到待复核申诉与当前阶段
        status, pending = self.request("GET", "/v1/appeals/pending",
                                       role="reviewer")
        self.assertEqual(status, 200)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["stage"], "appealing")

        # 复核：申诉成立 -> 撤销
        status, reviewed = self.request(
            "POST", f"/v1/events/{eid}/appeals/{appeal['appeal_id']}/review",
            {"basis_version": "规约-2026-v2", "decision": "upheld",
             "operator": "复核员A"},
            role="reviewer")
        self.assertEqual(status, 200)
        self.assertEqual(reviewed["status"], "upheld")

        status, detail = self.request("GET", f"/v1/events/{eid}",
                                      role="reviewer")
        self.assertEqual(detail["stage"], "revoked")

        # 事件链可查
        status, chain = self.request("GET", f"/v1/events/{eid}/chain",
                                     role="reviewer")
        self.assertEqual(status, 200)
        kinds = {item["kind"] for item in chain}
        self.assertIn("review", kinds)
        self.assertIn("action", kinds)

    def test_anonymous_clue_flow_and_identify(self):
        status, resp = self.request("POST", "/v1/events", {
            "location_code": "B3-FIRE", "location": "消防通道",
            "reporter": "网格员甲", "anonymous": True,
            "evidence": [{"kind": "photo", "uri": "p.jpg"}]},
            role="grid")
        self.assertEqual(status, 201)
        self.assertEqual(resp["event"]["stage"], "clue")
        clue_id = resp["event"]["event_id"]

        # 未认证视图不暴露个人信息
        status, pub = self.request("GET", f"/v1/events/{clue_id}")
        self.assertEqual(status, 200)
        self.assertNotIn("evidence", pub)

        status, ident = self.request(
            "POST", f"/v1/events/{clue_id}/identify",
            {"subject_type": "plate", "subject_id": "沪b 66666",
             "owner_name": "李四", "operator": "网格员甲"}, role="grid")
        self.assertEqual(status, 200)
        self.assertEqual(ident["event"]["subject_id"], "沪B66666")
        self.assertEqual(ident["event"]["stage"], "collecting")

    def test_pii_hidden_by_role(self):
        _, resp = self.report()
        eid = resp["event"]["event_id"]
        status, owner_view = self.request(
            "GET", f"/v1/events/{eid}", role="owner")
        self.assertEqual(status, 200)
        # 车主看不到上报网格员姓名与群聊来源
        self.assertNotIn("网格员甲", json.dumps(owner_view, ensure_ascii=False))

    def test_stats_restart_consistency(self):
        self.report()
        status, stats1 = self.request("GET", "/v1/stats", role="reviewer")
        self.assertEqual(stats1["events_total"], 1)

        # 重启：同一 db 文件重建服务
        self.server.shutdown()
        self.server.server_close()
        self.server = build_server(self.db, port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        status, stats2 = self.request("GET", "/v1/stats", role="reviewer")
        self.assertEqual(status, 200)
        self.assertEqual(stats1, stats2)


if __name__ == "__main__":
    unittest.main()
