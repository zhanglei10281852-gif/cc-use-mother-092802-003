"""HTTP API（仅标准库）：把 GrantService 暴露为 REST 端点。

调用约定：
- 请求/响应均为 JSON；
- 调用人通过请求头 `X-Actor-Id` 传入用户 id，服务端按用户角色鉴权；
- 错误响应对应领域错误的状态码（400/403/404/409）。
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

from .errors import GrantError, ValidationError
from .service import GrantService


def _parse_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or 0)
    if not length:
        return {}
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ValidationError("请求体不是合法 JSON") from None
    if not isinstance(data, dict):
        raise ValidationError("请求体必须是 JSON 对象")
    return data


def dispatch(
    service: GrantService, method: str, path: str, body: dict[str, Any], actor: Optional[str]
) -> tuple[Any, int]:
    """路由表。返回 (响应体, 状态码)。"""
    path = path.split("?", 1)[0].rstrip("/") or "/"

    def need_actor() -> str:
        if not actor:
            raise ValidationError("缺少 X-Actor-Id 请求头")
        return actor

    m: Optional[re.Match[str]]

    if method == "POST" and path == "/users":
        return service.create_user(body.get("id", ""), body.get("name", ""),
                                   body.get("role", "")), 201
    if method == "POST" and path == "/projects":
        return service.create_project(
            need_actor(),
            code=body.get("code", ""), name=body.get("name", ""),
            partner=body.get("partner", ""), location=body.get("location", ""),
            total_amount=body.get("total_amount", 0),
            milestones=body.get("milestones") or [],
            currency=body.get("currency", "CNY"),
        ), 201
    if m := re.fullmatch(r"/projects/([^/]+)", path):
        (pid,) = m.groups()
        if method == "GET":
            return service.get_project(pid), 200
    if m := re.fullmatch(r"/projects/([^/]+)/(activate|suspend|resume|close)", path):
        pid, action = m.groups()
        if method == "POST":
            if action == "activate":
                return service.activate_project(need_actor(), pid), 200
            if action == "suspend":
                return service.suspend_project(need_actor(), pid,
                                               reason=body.get("reason", "")), 200
            if action == "resume":
                return service.resume_project(
                    need_actor(), pid,
                    new_location=body.get("new_location", ""),
                    reason=body.get("reason", ""),
                    new_milestones=body.get("new_milestones") or [],
                    recover_amount=body.get("recover_amount", 0),
                    new_funds=body.get("new_funds", 0),
                ), 200
            return service.close_project(need_actor(), pid), 200
    if m := re.fullmatch(r"/projects/([^/]+)/(ledger|availability|reconciliation|fund-trail|audit|milestones)", path):
        pid, view = m.groups()
        if method == "GET":
            if view == "ledger":
                return service.ledger(pid), 200
            if view == "availability":
                return service.availability(pid), 200
            if view == "reconciliation":
                return service.reconciliation(pid), 200
            if view == "fund-trail":
                return service.fund_trail(pid), 200
            if view == "audit":
                return service.audit_trail(pid), 200
            return service.list_milestones(pid), 200
    if m := re.fullmatch(r"/milestones/([^/]+)/evidence", path):
        (mid,) = m.groups()
        if method == "POST":
            return service.submit_evidence(
                need_actor(), mid, content_uri=body.get("content_uri", ""),
                note=body.get("note", "")), 201
        if method == "GET":
            return service.list_evidence(mid), 200
    if m := re.fullmatch(r"/evidence/([^/]+)/review", path):
        (eid,) = m.groups()
        if method == "POST":
            return service.review_evidence(
                need_actor(), eid, approve=bool(body.get("approve")),
                comment=body.get("comment", "")), 200
    if m := re.fullmatch(r"/milestones/([^/]+)/verifications", path):
        (mid,) = m.groups()
        if method == "POST":
            return service.record_verification(
                need_actor(), mid, kind=body.get("kind", ""),
                conclusion=body.get("conclusion", ""),
                detail=body.get("detail", "")), 201
    if m := re.fullmatch(r"/milestones/([^/]+)/instructions", path):
        (mid,) = m.groups()
        if method == "POST":
            return service.create_instruction(
                need_actor(), mid, amount=body.get("amount", 0)), 201
    if m := re.fullmatch(r"/instructions/([^/]+)", path):
        (iid,) = m.groups()
        if method == "GET":
            return service.get_instruction(iid), 200
    if m := re.fullmatch(r"/instructions/([^/]+)/trail", path):
        (iid,) = m.groups()
        if method == "GET":
            return service.instruction_trail(iid), 200
    if m := re.fullmatch(r"/instructions/([^/]+)/approve", path):
        (iid,) = m.groups()
        if method == "POST":
            return service.approve_instruction(need_actor(), iid), 200
    if m := re.fullmatch(r"/instructions/([^/]+)/receipts", path):
        (iid,) = m.groups()
        if method == "POST":
            receipt, created = service.record_receipt(
                need_actor(), iid, external_ref=body.get("external_ref", ""),
                amount=body.get("amount", 0))
            return {"receipt": receipt, "created": created}, 201 if created else 200
    if m := re.fullmatch(r"/instructions/([^/]+)/writeoff", path):
        (iid,) = m.groups()
        if method == "POST":
            return service.writeoff_instruction(need_actor(), iid), 200
    raise ValidationError(f"未知路由: {method} {path}")


def make_handler(service: GrantService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "ClimateFundAPI/1.0"
        protocol_version = "HTTP/1.1"

        def _respond(self, code: int, payload: Any) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method: str) -> None:
            try:
                body = _parse_body(self) if method == "POST" else {}
                payload, code = dispatch(
                    service, method, self.path, body,
                    self.headers.get("X-Actor-Id"))
                self._respond(code, payload)
            except GrantError as exc:
                self._respond(exc.http_status,
                              {"error": str(exc), "type": type(exc).__name__})
            except Exception as exc:  # pragma: no cover - 兜底
                self._respond(500, {"error": str(exc), "type": type(exc).__name__})

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def log_message(self, *args: Any) -> None:  # 静默
            pass

    return Handler


def serve(service: GrantService, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    """启动 HTTP 服务（阻塞）。返回 server 便于测试关闭。"""
    server = ThreadingHTTPServer((host, port), make_handler(service))
    print(f"气候拨款 API  listening on http://{host}:{server.server_address[1]}")
    server.serve_forever()
    return server
