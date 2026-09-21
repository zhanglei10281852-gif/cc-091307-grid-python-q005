"""HTTP 接口（标准库 http.server，无第三方依赖）。

角色通过 X-Role 请求头或 role 查询参数指定：
- reviewer：管理端/复核员，全部权限，可见完整个人信息；
- grid：网格员，可上报、取证、确认主体、告知、处置；
- owner：车主，可提交申诉，视图脱敏；
- anonymous：默认，仅能看到脱敏的处置进度。
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from .errors import OrderServiceError, ValidationError
from .service import OrderService
from .storage import JsonStore
from .views import serialize_chain, serialize_event

# 各角色允许的写操作
_GRID_OPS = {"report", "evidence", "identify", "notify", "enforce"}
_REVIEWER_OPS = _GRID_OPS | {"review", "revoke", "close"}
_OWNER_OPS = {"appeal"}

_OP_FOR_PATH = [
    (re.compile(r"^/v1/events/?$"), "report"),
    (re.compile(r"^/v1/events/[^/]+/evidence/?$"), "evidence"),
    (re.compile(r"^/v1/events/[^/]+/identify/?$"), "identify"),
    (re.compile(r"^/v1/events/[^/]+/notify/?$"), "notify"),
    (re.compile(r"^/v1/events/[^/]+/appeals/?$"), "appeal"),
    (re.compile(r"^/v1/events/[^/]+/appeals/[^/]+/review/?$"), "review"),
    (re.compile(r"^/v1/events/[^/]+/enforce/?$"), "enforce"),
    (re.compile(r"^/v1/events/[^/]+/revoke/?$"), "revoke"),
    (re.compile(r"^/v1/events/[^/]+/close/?$"), "close"),
]


def _op_for(path: str) -> Optional[str]:
    for pattern, op in _OP_FOR_PATH:
        if pattern.match(path):
            return op
    return None


def _role_allowed(role: str, op: str) -> bool:
    if role == "reviewer":
        return op in _REVIEWER_OPS
    if role == "grid":
        return op in _GRID_OPS
    if role == "owner":
        return op in _OWNER_OPS
    return False


class _Handler(BaseHTTPRequestHandler):
    server_version = "OrderEventService/1.0"

    # 由 server 注入
    @property
    def service(self) -> OrderService:
        return self.server.service  # type: ignore[attr-defined]

    @property
    def service_lock(self) -> threading.Lock:
        return self.server.service_lock  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静运行
        return

    # ------------------------------------------------------------- 基础工具

    def _role(self, query: dict[str, str]) -> str:
        role = self.headers.get("X-Role") or query.get("role") or "anonymous"
        return role if role in ("reviewer", "grid", "owner", "anonymous") else "anonymous"

    def _operator_name(self, payload: dict[str, Any]) -> Optional[str]:
        name = self.headers.get("X-Operator")
        if name:
            return name
        return payload.get("operator") or payload.get("reporter")

    def _send_json(self, status: int, body: Any) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_error(self, err: OrderServiceError) -> None:
        self._send_json(
            err.http_status,
            {"error": err.code, "message": str(err)},
        )

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            raise ValidationError("请求体为空")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValidationError("请求体不是合法 JSON")
        if not isinstance(payload, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return payload

    # --------------------------------------------------------------- 路由

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        role = self._role(query)
        try:
            with self.service_lock:
                if path == "/v1/events":
                    events = self.service.list_events(stage=query.get("stage"))
                    self._send_json(200, [serialize_event(e, role) for e in events])
                    return
                if path == "/v1/appeals/pending":
                    if role != "reviewer":
                        self._send_json(403, {"error": "forbidden",
                                              "message": "仅管理端可查看待复核申诉"})
                        return
                    events = self.service.pending_appeals()
                    self._send_json(200, [serialize_event(e, role) for e in events])
                    return
                if path == "/v1/stats":
                    if role not in ("reviewer", "grid"):
                        self._send_json(403, {"error": "forbidden",
                                              "message": "仅工作人员可查看统计"})
                        return
                    self._send_json(200, self.service.stats())
                    return
                if path.endswith("/chain"):
                    event_id = path.split("/")[3]
                    chain = self.service.event_chain(event_id)
                    self._send_json(200, serialize_chain(chain, role))
                    return
                m = re.match(r"^/v1/events/([^/]+)/?$", path)
                if m:
                    event = self.service.get_event(m.group(1))
                    self._send_json(200, serialize_event(event, role))
                    return
                self._send_json(404, {"error": "not_found", "message": "未知路径"})
        except OrderServiceError as err:
            self._send_error(err)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            payload = self._read_json()
        except OrderServiceError as err:
            self._send_error(err)
            return

        role = self._role({})
        op = _op_for(path)
        if op is None:
            self._send_json(404, {"error": "not_found", "message": "未知路径"})
            return
        if not _role_allowed(role, op):
            self._send_json(
                403,
                {"error": "forbidden", "message": f"角色 {role} 无权执行 {op}"},
            )
            return

        operator = self._operator_name(payload)
        if isinstance(operator, str) and operator.strip():
            payload.setdefault("operator", operator.strip())

        try:
            with self.service_lock:
                self._dispatch(path, payload, role)
        except OrderServiceError as err:
            self._send_error(err)

    def _dispatch(self, path: str, payload: dict[str, Any], role: str) -> None:
        svc = self.service
        parts = [p for p in path.split("/") if p]

        if path == "/v1/events":
            if not payload.get("reporter") and payload.get("operator"):
                payload["reporter"] = payload["operator"]
            event, created = svc.create_or_report(payload)
            self._send_json(201 if created else 200,
                            {"created": created, "event": serialize_event(event, role)})
            return

        event_id = parts[2]
        tail = parts[3:]

        if tail == ["evidence"]:
            evidence = svc.add_evidence(event_id, payload)
            self._send_json(201, evidence.to_dict())
        elif tail == ["identify"]:
            event, merged = svc.identify_clue(event_id, payload)
            self._send_json(200, {"merged": merged,
                                  "event": serialize_event(event, role)})
        elif tail == ["notify"]:
            action = svc.notify(event_id, payload)
            self._send_json(201, action.to_dict())
        elif tail == ["appeals"]:
            appeal = svc.file_appeal(event_id, payload)
            self._send_json(201, appeal.to_dict())
        elif len(tail) == 3 and tail[0] == "appeals" and tail[2] == "review":
            appeal = svc.review_appeal(event_id, tail[1], payload)
            self._send_json(200, appeal.to_dict())
        elif tail == ["enforce"]:
            action = svc.enforce(event_id, payload)
            self._send_json(201, action.to_dict())
        elif tail == ["revoke"]:
            action = svc.revoke(event_id, payload)
            self._send_json(201, action.to_dict())
        elif tail == ["close"]:
            action = svc.close(event_id, payload)
            self._send_json(201, action.to_dict())
        else:
            self._send_json(404, {"error": "not_found", "message": "未知路径"})


def build_server(
    db_path: str,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    merge_window_minutes: int = 30,
    clock: Optional[Callable[[], str]] = None,
) -> ThreadingHTTPServer:
    store = JsonStore(db_path)
    service = OrderService(
        store, merge_window_minutes=merge_window_minutes, clock=clock
    )
    server = ThreadingHTTPServer((host, port), _Handler)
    server.service = service            # type: ignore[attr-defined]
    server.service_lock = threading.Lock()  # type: ignore[attr-defined]
    return server


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="社区停车充电秩序事件服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default="data/orderdb.json")
    parser.add_argument("--merge-window", type=int, default=30,
                        help="同车同点重复上报的关联窗口（分钟）")
    args = parser.parse_args()

    server = build_server(
        args.db, host=args.host, port=args.port,
        merge_window_minutes=args.merge_window,
    )
    print(f"秩序事件服务已启动: http://{args.host}:{args.port}  数据库: {args.db}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
