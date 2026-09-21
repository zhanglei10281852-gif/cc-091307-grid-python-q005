"""HTTP 接口端到端测试：真实起服 + urllib 请求。"""

import json
import threading
import unittest
import urllib.error
import urllib.request

from src.api import create_server


def _request(url, method="GET", body=None, headers=None):
    data = None
    hdrs = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=hdrs,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class ApiTestBase(unittest.TestCase):
    def setUp(self):
        self.httpd = create_server("127.0.0.1", 0, ":memory:", quiet=True)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}/v1"
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.svc.shutdown()
        self.httpd.server_close()

    GW = {"X-Actor-Id": "gw1", "X-Actor-Role": "grid_worker",
          "X-Actor-Name": "Wang"}
    ADMIN = {"X-Actor-Id": "admin1", "X-Actor-Role": "admin",
             "X-Actor-Name": "Li"}
    OWNER = {"X-Actor-Id": "own1", "X-Actor-Role": "owner",
             "X-Actor-Name": "Zhang"}

    LOC = {"location_key": "BLDG3-FIRELANE-A", "name": "3号楼消防通道"}

    def report(self, *, extra_headers=None, **overrides):
        body = {
            "subject_type": "plate",
            "subject_id": "沪A12345",
            "location": self.LOC,
            "owner": {"id": "own1", "name": "张三",
                      "phone": "13911112222", "id_no": "310101199001011234"},
            "evidences": [{"kind": "photo", "content": "占消防通道"}],
        }
        body.update(overrides)
        return _request(f"{self.base}/incidents/report", "POST", body,
                        extra_headers or self.GW)


class TestHttpFlow(ApiTestBase):
    def test_health(self):
        status, body = _request(f"{self.base}/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_full_lifecycle_over_http(self):
        # 1) 上报
        st, r = self.report()
        self.assertEqual(st, 201)
        iid = r["incident_id"]

        # 2) 告知
        st, r = _request(f"{self.base}/incidents/{iid}/notify", "POST",
                         {"basis_version": "XFMD-2026.1", "method": "sms"},
                         self.GW)
        self.assertEqual(st, 200, r)
        self.assertEqual(r["stage"], "NOTIFIED")

        # 3) 处置
        st, r = _request(f"{self.base}/incidents/{iid}/disposals", "POST",
                         {"kind": "fine", "basis_version": "XFMD-2026.1",
                          "amount": 50}, self.ADMIN)
        self.assertEqual(st, 201, r)
        did = r["disposal_id"]

        # 4) 网格员无权处置 → 403
        st, r = _request(f"{self.base}/incidents/{iid}/disposals", "POST",
                         {"kind": "tow", "basis_version": "XFMD-2026.1"},
                         self.GW)
        self.assertEqual(st, 403)

        # 5) 申诉 → 冻结
        st, r = _request(f"{self.base}/incidents/{iid}/appeals", "POST",
                         {"reason": "车已提前搬离",
                          "contact": "13911112222"}, self.OWNER)
        self.assertEqual(st, 201, r)
        aid = r["appeal_id"]

        st, r = _request(f"{self.base}/incidents/{iid}/disposals", "POST",
                         {"kind": "tow", "basis_version": "XFMD-2026.1"},
                         self.ADMIN)
        self.assertEqual(st, 409)

        # 6) 待复核申诉工作台
        st, r = _request(f"{self.base}/admin/pending-appeals",
                         headers=self.ADMIN)
        self.assertEqual(st, 200)
        self.assertEqual(len(r["pending"]), 1)

        # 7) 复核成立 → 处置撤销
        st, r = _request(
            f"{self.base}/incidents/{iid}/appeals/{aid}/review", "POST",
            {"decision": "upheld", "basis_version": "XFMD-2026.2",
             "note": "监控属实"}, self.ADMIN)
        self.assertEqual(st, 200, r)
        self.assertEqual(r["stage"], "REVOKED")

        # 8) 撤销原因在详情中可见
        st, detail = _request(f"{self.base}/incidents/{iid}",
                              headers=self.ADMIN)
        self.assertEqual(st, 200)
        self.assertEqual(detail["disposals"][0]["status"], "revoked")
        self.assertIn("申诉复核成立",
                      detail["disposals"][0]["revoke_reason"])
        # 事件链完整
        actions = [c["action"] for c in detail["chain"]]
        self.assertEqual(actions,
                         ["created", "notified", "disposed", "appealed",
                          "appeal_reviewed"])

        # 9) 结案（无生效处置，REVOKED 可直接结案）
        st, r = _request(f"{self.base}/incidents/{iid}/close", "POST",
                         {"basis_version": "XFMD-2026.2",
                          "note": "撤销后结案"}, self.ADMIN)
        self.assertEqual(st, 200, r)
        self.assertEqual(r["stage"], "CLOSED")

    def test_role_based_redaction_over_http(self):
        _, r = self.report()
        iid = r["incident_id"]

        st, gw = _request(f"{self.base}/incidents/{iid}", headers=self.GW)
        self.assertEqual(st, 200)
        self.assertEqual(gw["owner_phone"], "***")
        self.assertEqual(gw["owner_name"], "张三")

        st, owner = _request(f"{self.base}/incidents/{iid}",
                             headers=self.OWNER)
        self.assertEqual(owner["owner_phone"], "13911112222")
        self.assertNotIn("reporter_name", owner["reports"][0])

        # 其他车主访问 → 404（不暴露存在性）
        other = {"X-Actor-Id": "own9", "X-Actor-Role": "owner"}
        st, _ = _request(f"{self.base}/incidents/{iid}", headers=other)
        self.assertEqual(st, 404)

    def test_missing_actor_is_rejected(self):
        st, body = _request(f"{self.base}/incidents",
                            headers={"X-Actor-Id": "", "X-Actor-Role": ""})
        self.assertEqual(st, 403)

    def test_invalid_json_returns_400(self):
        req = urllib.request.Request(
            f"{self.base}/incidents/report", data=b"{not json",
            headers={**self.GW, "Content-Type": "application/json"},
            method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("应返回 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    def test_unknown_route_404(self):
        st, _ = _request(f"{self.base}/nope", headers=self.ADMIN)
        self.assertEqual(st, 404)


class TestMultiChannelScenario(ApiTestBase):
    """还原需求场景：现场照片、车主申诉、整改结果分散在不同群聊，
    服务必须把它们关联到同一事件，避免重复处罚。"""

    def test_same_car_from_different_chats_links_and_punishes_once(self):
        # 群 1：晚高峰巡查群的现场照片
        st, r1 = self.report(
            channel="晚高峰巡查群",
            evidences=[{"kind": "photo", "source": "晚高峰巡查群",
                        "attachment_uri": "oss://a/1.jpg",
                        "content": "电动车占用消防通道"}])
        iid = r1["incident_id"]

        # 群 2：另一网格员在同点看到同一辆车（短时间内）
        gw2 = {"X-Actor-Id": "gw2", "X-Actor-Role": "grid_worker",
               "X-Actor-Name": "Zhao"}
        st, r2 = self.report(
            extra_headers=gw2, channel="消防隐患督办群",
            evidences=[{"kind": "video", "source": "消防隐患督办群",
                        "attachment_uri": "oss://a/2.mp4",
                        "content": "同一车辆仍在现场"}])
        self.assertTrue(r2["merged"])
        self.assertEqual(r2["incident_id"], iid)

        # 告知 + 罚款
        _request(f"{self.base}/incidents/{iid}/notify", "POST",
                 {"basis_version": "XFMD-2026.1"}, self.GW)
        st, d = _request(f"{self.base}/incidents/{iid}/disposals", "POST",
                         {"kind": "fine", "basis_version": "XFMD-2026.1",
                          "amount": 50}, self.ADMIN)
        self.assertEqual(st, 201)

        # 群 3：车主在整改反馈群提交申诉（由网格员代登记）
        st, a = _request(
            f"{self.base}/incidents/{iid}/appeals", "POST",
            {"reason": "车主称当时正在搬运，附整改照片",
             "owner_id": "own1", "contact": "13911112222"}, self.GW)
        self.assertEqual(st, 201, a)

        # 申诉期间，第三个网格员又开一张罚单 —— 必须被拒绝
        gw3 = {"X-Actor-Id": "gw3", "X-Actor-Role": "grid_worker"}
        st, _ = _request(f"{self.base}/incidents/{iid}/disposals", "POST",
                         {"kind": "fine", "basis_version": "XFMD-2026.1"},
                         gw3)
        self.assertEqual(st, 403)  # 网格员本就无权；管理员同样 409（前例已测）

        # 申诉驳回
        st, rv = _request(
            f"{self.base}/incidents/{iid}/appeals/{a['appeal_id']}/review",
            "POST", {"decision": "rejected",
                     "basis_version": "XFMD-2026.1",
                     "note": "视频显示持续占用"}, self.ADMIN)
        self.assertEqual(st, 200)

        # 整个事件链按时间串联了三个群的材料与各次操作
        st, detail = _request(f"{self.base}/incidents/{iid}",
                              headers=self.ADMIN)
        self.assertEqual(len(detail["reports"]), 2)
        sources = {e["source"] for e in detail["evidences"]}
        self.assertIn("晚高峰巡查群", sources)
        self.assertIn("消防隐患督办群", sources)
        # 罚款自始至终只有一条
        fines = [d for d in detail["disposals"] if d["kind"] == "fine"]
        self.assertEqual(len(fines), 1)
        self.assertEqual(fines[0]["status"], "active")


class TestDeviceSubject(ApiTestBase):
    def test_device_identifier_dedup(self):
        body = {"subject_type": "device",
                "subject_id": "DEV:BATTERY-00088",
                "location": {"location_key": "CHARGE-RACK-2"},
                "evidences": [{"kind": "note", "content": "飞线充电"}]}
        st, r1 = _request(f"{self.base}/incidents/report", "POST", body,
                          self.GW)
        self.assertEqual(st, 201, r1)
        self.assertEqual(r1["incident"]["stage"], "LEAD")
        st, r2 = _request(f"{self.base}/incidents/report", "POST", body,
                          self.GW)
        self.assertTrue(r2["merged"])
        self.assertEqual(r1["incident_id"], r2["incident_id"])


if __name__ == "__main__":
    unittest.main()
