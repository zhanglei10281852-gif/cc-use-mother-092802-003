"""HTTP API（标准库 http.server，ThreadingHTTPServer）。

鉴权：每个请求带 X-User-Id 头；服务层再按角色鉴权。
全部接口返回 JSON，错误体 {"error": code, "message": ...}。

路由：
  POST   /users
  POST   /projects
  GET    /projects/{id}
  POST   /projects/{id}/milestones
  GET    /milestones/{id}
  POST   /milestones/{id}/evidence
  POST   /evidence/{id}/decision            {accept, note}
  POST   /milestones/{id}/verifications
  POST   /projects/{id}/payments
  GET    /projects/{id}/payments
  GET    /payments/{id}                     资金依据链 + 状态时间线
  POST   /payments/{id}/approve | /reject | /freeze | /resume | /cancel
  POST   /payments/{id}/receipts            放款回执（幂等）
  POST   /payments/{id}/reconcile
  POST   /projects/{id}/suspend
  POST   /projects/{id}/relocate
  POST   /projects/{id}/resume
  GET    /projects/{id}/amendments
  GET    /projects/{id}/trace               每笔资金依据链 + 对账
  GET    /projects/{id}/reconciliation
  GET    /projects/{id}/events
  POST   /admin/clock/advance               可控时钟（测试用）
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .app import Application
from .errors import DomainError


def _json_default(obj):
    from decimal import Decimal
    if isinstance(obj, Decimal):
        return str(obj)
    return str(obj)


class _Handler(BaseHTTPRequestHandler):
    server_version = "ClimateFund/1.0"
    app: Application = None  # type: ignore  # 由 make_server 注入

    def log_message(self, *args):  # 静默默认访问日志
        pass

    # ------------------------------------------------------------ plumbing
    def _send(self, status: int, body) -> None:
        data = json.dumps(body, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError
            return data
        except Exception as exc:  # noqa: BLE001
            raise DomainError("请求体必须是 JSON 对象", status=400, code="bad_json") from exc

    def _actor(self) -> str:
        actor = self.headers.get("X-User-Id")
        if not actor:
            raise DomainError("缺少 X-User-Id 请求头", status=401, code="unauthorized")
        return actor

    def _dispatch(self, method: str, path: str, body: dict):
        s = self
        uid = r"(?P<id>[A-Za-z0-9_\-]+)"
        actor = s._actor

        def call(fn, **kw):
            return fn(**kw)

        if method == "POST" and path == "/users":
            return s.app.service.create_user(
                body.get("id", ""), body.get("name", ""), body.get("role", ""))

        if method == "POST" and path == "/projects":
            return s.app.service.create_project(
                actor(), title=body["title"], grantee=body["grantee"], site=body["site"],
                total_amount=body["total_amount"], currency=body.get("currency", "USD"))

        m = re.fullmatch(rf"/projects/{uid}", path)
        if method == "GET" and m:
            return s.app.service.get_project(m["id"])

        m = re.fullmatch(rf"/projects/{uid}/milestones", path)
        if method == "POST" and m:
            return s.app.service.add_milestone(
                actor(), m["id"], sequence=body["sequence"], title=body["title"],
                planned_amount=body["planned_amount"], plan_revision=body.get("plan_revision"))

        m = re.fullmatch(rf"/milestones/{uid}", path)
        if method == "GET" and m:
            return s.app.service.get_milestone(m["id"])

        m = re.fullmatch(rf"/milestones/{uid}/evidence", path)
        if method == "POST" and m:
            return s.app.service.submit_evidence(
                actor(), m["id"], doc_ref=body["doc_ref"], note=body.get("note", ""))

        m = re.fullmatch(rf"/evidence/{uid}/decision", path)
        if method == "POST" and m:
            return s.app.service.decide_evidence(
                actor(), m["id"], accept=bool(body.get("accept")), note=body.get("note", ""))

        m = re.fullmatch(rf"/milestones/{uid}/verifications", path)
        if method == "POST" and m:
            return s.app.service.record_verification(
                actor(), m["id"], evidence_id=body["evidence_id"], result=body["result"],
                site_actual=body["site_actual"], verified_amount=body.get("verified_amount"),
                note=body.get("note", ""))

        m = re.fullmatch(rf"/projects/{uid}/payments", path)
        if method == "POST" and m:
            return s.app.service.request_payment(
                actor(), m["id"], body["milestone_id"], body["amount"],
                note=body.get("note", ""))
        if method == "GET" and m:
            return {"payment_orders": s.app.service.list_payment_orders(m["id"])}

        m = re.fullmatch(rf"/payments/{uid}", path)
        if method == "GET" and m:
            return s.app.service.get_payment_order(m["id"])

        m = re.fullmatch(rf"/payments/{uid}/(?P<action>approve|reject|freeze|resume|cancel|reconcile)", path)
        if method == "POST" and m:
            svc = s.app.service
            oid, act = m["id"], m["action"]
            if act == "approve":
                return svc.approve_payment(actor(), oid, note=body.get("note", ""))
            if act == "reject":
                return svc.reject_payment(actor(), oid, note=body.get("note", ""))
            if act == "freeze":
                return svc.freeze_payment(actor(), oid, reason=body.get("reason", "manual_freeze"))
            if act == "resume":
                return svc.resume_payment(actor(), oid, note=body.get("note", ""))
            if act == "cancel":
                return svc.cancel_payment(actor(), oid, note=body.get("note", ""))
            if act == "reconcile":
                return svc.reconcile_payment(actor(), oid, note=body.get("note", ""))

        m = re.fullmatch(rf"/payments/{uid}/receipts", path)
        if method == "POST" and m:
            return s.app.service.record_receipt(
                actor(), m["id"], receipt_no=body["receipt_no"], amount=body["amount"],
                currency=body.get("currency"), note=body.get("note", ""))

        m = re.fullmatch(rf"/projects/{uid}/suspend", path)
        if method == "POST" and m:
            return s.app.service.suspend_project(actor(), m["id"], reason=body.get("reason", ""))

        m = re.fullmatch(rf"/projects/{uid}/relocate", path)
        if method == "POST" and m:
            return s.app.service.relocate_by_disaster(
                actor(), m["id"], new_site=body["new_site"],
                new_total_amount=body["new_total_amount"],
                reason=body.get("reason", "natural_disaster_relocation"),
                note=body.get("note", ""))

        m = re.fullmatch(rf"/projects/{uid}/resume", path)
        if method == "POST" and m:
            return s.app.service.resume_project(actor(), m["id"], note=body.get("note", ""))

        m = re.fullmatch(rf"/projects/{uid}/amendments", path)
        if method == "GET" and m:
            return {"amendments": s.app.service.list_amendments(m["id"])}

        m = re.fullmatch(rf"/projects/{uid}/trace", path)
        if method == "GET" and m:
            return s.app.service.project_fund_trace(m["id"])

        m = re.fullmatch(rf"/projects/{uid}/reconciliation", path)
        if method == "GET" and m:
            return s.app.service.reconciliation(m["id"])

        m = re.fullmatch(rf"/projects/{uid}/events", path)
        if method == "GET" and m:
            return {"events": s.app.service.timeline(project_id=m["id"])}

        if method == "POST" and path == "/admin/clock/advance":
            s.app.clock.advance(seconds=int(body.get("seconds", 0)), **{
                k: int(v) for k, v in body.items()
                if k in ("days", "hours", "minutes") and v is not None
            })
            return {"now": s.app.clock.now().isoformat()}

        raise DomainError(f"无此路由: {method} {path}", status=404, code="not_found")

    # ------------------------------------------------------------- entry
    def _handle(self, method: str):
        path = urlparse(self.path).path
        try:
            body = self._body() if method == "POST" else {}
            result = self._dispatch(method, path, body)
            self._send(200, result if result is not None else {"ok": True})
        except DomainError as exc:
            self._send(exc.status, {"error": exc.code, "message": str(exc)})
        except KeyError as exc:
            self._send(400, {"error": "validation_error", "message": f"缺少字段: {exc.args[0]}"})
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": "internal_error", "message": str(exc)})

    def do_GET(self):  # noqa: N802
        self._handle("GET")

    def do_POST(self):  # noqa: N802
        self._handle("POST")


def make_server(host: str = "127.0.0.1", port: int = 0, *, app: Application | None = None):
    app = app or Application(":memory:")

    class Handler(_Handler):
        pass

    Handler.app = app
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.app = app
    return httpd


def serve(host: str = "127.0.0.1", port: int = 8080, db_path: str = "climate_fund.db") -> None:
    app = Application(db_path)
    httpd = make_server(host, port, app=app)
    print(f"气候拨款管理后端运行于 http://{host}:{httpd.server_address[1]} （数据库 {db_path}）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        app.close()


if __name__ == "__main__":
    serve()
