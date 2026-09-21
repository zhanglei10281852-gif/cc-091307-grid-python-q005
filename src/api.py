"""基于标准库 http.server 的 JSON 接口。

鉴权约定（部署时应替换为网关注名/令牌校验）：
    X-Actor-Id    经办人/车主标识（必填）
    X-Actor-Role  grid_worker / admin / owner
    X-Actor-Name  经办人姓名（可选，用于留痕展示）

路由
====
POST   /v1/incidents/report                       上报（自动去重关联）
POST   /v1/incidents/{id}/evidence                证据追加
POST   /v1/incidents/{id}/identify                匿名线索确认车主
POST   /v1/incidents/{id}/notify                  告知
POST   /v1/incidents/{id}/disposals               新增处置
POST   /v1/incidents/{id}/disposals/{did}/revoke  撤销处置（需原因）
POST   /v1/incidents/{id}/appeals                 提起申诉
POST   /v1/incidents/{id}/appeals/{aid}/review    复核申诉
POST   /v1/incidents/{id}/close                   结案
GET    /v1/incidents/{id}                         事件详情（按角色裁剪）
GET    /v1/incidents/{id}/chain                   事件链
GET    /v1/incidents?stage=                       事件列表
GET    /v1/admin/pending-appeals                  待复核申诉
GET    /v1/admin/stage-board                      阶段计数
GET    /v1/health
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from .models import Actor, OrderServiceError
from .service import OrderEventService

_ID = r"[^/]+"


def create_server(host: str, port: int, db_path: str,
                  dedup_minutes: int = 120,
                  quiet: bool = False) -> ThreadingHTTPServer:
    from datetime import timedelta

    svc = OrderEventService(db_path,
                            dedup_window=timedelta(minutes=dedup_minutes))

    class Handler(BaseHTTPRequestHandler):
        server_version = "OrderEventService/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            if getattr(self.server, "quiet", False):
                return
            super().log_message(fmt, *args)

        # -- 基础读写 ------------------------------------------------------

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise OrderServiceError("请求体不是合法 JSON",
                                        code="invalid_request",
                                        http_status=400) from exc
            if not isinstance(data, dict):
                raise OrderServiceError("请求体必须是 JSON 对象",
                                        code="invalid_request",
                                        http_status=400)
            return data

        def _actor(self) -> Actor:
            return Actor(
                id=self.headers.get("X-Actor-Id", ""),
                role=self.headers.get("X-Actor-Role", ""),
                name=self.headers.get("X-Actor-Name"),
            )

        def _send(self, status: int, body: Any) -> None:
            payload = json.dumps(body, ensure_ascii=False,
                                 default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type",
                             "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _ok(self, body: Any, status: int = 200) -> None:
            self._send(status, body)

        # -- 路由 ----------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            try:
                parts = urlsplit(self.path)
                path = parts.path.rstrip("/") or "/"
                query = parse_qs(parts.query)
                for pattern, verbs, fn in self.ROUTES:
                    m = re.fullmatch(pattern, path)
                    if m and method in verbs:
                        fn(self, m.groupdict(), query)
                        return
                self._send(404, {"error": "not_found",
                                 "message": f"无此路由: {method} {path}"})
            except OrderServiceError as exc:
                self._send(exc.http_status, exc.to_dict())
            except Exception as exc:  # pragma: no cover - 兜底
                self._send(500, {"error": "internal_error",
                                 "message": str(exc)})

        # -- 端点 ----------------------------------------------------------

        def h_health(self, _p: dict, _q: dict) -> None:
            self._ok({"status": "ok" if svc.ready else "unavailable"})

        def h_report(self, _p: dict, _q: dict) -> None:
            body = self._read_json()
            res = svc.report(body, self._actor(),
                             collected_at=body.pop("collected_at", None))
            self._ok(res, 201)

        def h_evidence(self, p: dict, _q: dict) -> None:
            body = self._read_json()
            res = svc.add_evidence(p["id"], body, self._actor(),
                                   collected_at=body.pop("collected_at", None))
            self._ok(res, 201)

        def h_identify(self, p: dict, _q: dict) -> None:
            self._ok(svc.identify_owner(p["id"], self._read_json(),
                                        self._actor()))

        def h_notify(self, p: dict, _q: dict) -> None:
            self._ok(svc.notify_owner(p["id"], self._read_json(),
                                      self._actor()))

        def h_dispose(self, p: dict, _q: dict) -> None:
            self._ok(svc.dispose(p["id"], self._read_json(), self._actor()),
                     201)

        def h_revoke(self, p: dict, _q: dict) -> None:
            self._ok(svc.revoke_disposal(p["id"], int(p["did"]),
                                         self._read_json(), self._actor()))

        def h_appeal(self, p: dict, _q: dict) -> None:
            self._ok(svc.appeal(p["id"], self._read_json(), self._actor()),
                     201)

        def h_review(self, p: dict, _q: dict) -> None:
            self._ok(svc.review_appeal(p["id"], int(p["aid"]),
                                       self._read_json(), self._actor()))

        def h_close(self, p: dict, _q: dict) -> None:
            self._ok(svc.close(p["id"], self._read_json(), self._actor()))

        def h_detail(self, p: dict, _q: dict) -> None:
            self._ok(svc.get_incident_detail(p["id"], self._actor()))

        def h_chain(self, p: dict, _q: dict) -> None:
            self._ok({"incident_id": p["id"],
                      "chain": svc.get_chain(p["id"], self._actor())})

        def h_list(self, _p: dict, q: dict) -> None:
            actor = self._actor()
            stage = q.get("stage", [None])[0]
            limit = int(q.get("limit", ["100"])[0])
            offset = int(q.get("offset", ["0"])[0])
            self._ok(svc.list_incidents(actor, stage=stage,
                                        limit=limit, offset=offset))

        def h_pending_appeals(self, _p: dict, _q: dict) -> None:
            self._ok({"pending": svc.pending_appeals(self._actor())})

        def h_stage_board(self, _p: dict, _q: dict) -> None:
            self._ok(svc.stage_board(self._actor()))

        ROUTES = [
            (r"/v1/health", {"GET"}, h_health),
            (r"/v1/incidents/report", {"POST"}, h_report),
            (rf"/v1/incidents/(?P<id>{_ID})/evidence", {"POST"}, h_evidence),
            (rf"/v1/incidents/(?P<id>{_ID})/identify", {"POST"}, h_identify),
            (rf"/v1/incidents/(?P<id>{_ID})/notify", {"POST"}, h_notify),
            (rf"/v1/incidents/(?P<id>{_ID})/disposals", {"POST"}, h_dispose),
            (rf"/v1/incidents/(?P<id>{_ID})/disposals/(?P<did>\d+)/revoke",
             {"POST"}, h_revoke),
            (rf"/v1/incidents/(?P<id>{_ID})/appeals", {"POST"}, h_appeal),
            (rf"/v1/incidents/(?P<id>{_ID})/appeals/(?P<aid>\d+)/review",
             {"POST"}, h_review),
            (rf"/v1/incidents/(?P<id>{_ID})/close", {"POST"}, h_close),
            (rf"/v1/incidents/(?P<id>{_ID})/chain", {"GET"}, h_chain),
            (rf"/v1/incidents/(?P<id>{_ID})", {"GET"}, h_detail),
            (r"/v1/incidents", {"GET"}, h_list),
            (r"/v1/admin/pending-appeals", {"GET"}, h_pending_appeals),
            (r"/v1/admin/stage-board", {"GET"}, h_stage_board),
        ]

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.quiet = quiet
    httpd.svc = svc
    return httpd


def main(argv: Optional[list[str]] = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="秩序事件服务")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="data/order_events.db")
    parser.add_argument("--dedup-minutes", type=int, default=120)
    args = parser.parse_args(argv)

    httpd = create_server(args.host, args.port, args.db,
                          dedup_minutes=args.dedup_minutes)
    print(f"秩序事件服务监听 http://{args.host}:{args.port} "
          f"(db={args.db}, dedup={args.dedup_minutes}min)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.svc.shutdown()
        httpd.server_close()


if __name__ == "__main__":  # pragma: no cover
    main()
