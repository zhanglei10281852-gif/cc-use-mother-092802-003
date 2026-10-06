"""HTTP API 端到端：真实线程服务器 + JSON 调用 + 角色头鉴权。"""

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from climate_fund.api import make_server  # noqa: E402
from climate_fund.app import Application  # noqa: E402
from climate_fund.clock import Clock  # noqa: E402


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "api.db")
        self.app = Application(self.db, Clock())
        self.httpd = make_server("127.0.0.1", 0, app=self.app)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        import threading
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.app.close()
        self.tmp.cleanup()

    def req(self, method, path, body=None, user=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if user:
            headers["X-User-Id"] = user
        r = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_end_to_end_over_http_with_role_enforcement_and_idempotent_receipt(self):
        # 用户
        for uid, name, role in [("u1", "经办", "officer"), ("u2", "审核", "reviewer"),
                                ("u3", "财务", "finance")]:
            st, _ = self.req("POST", "/users", {"id": uid, "name": name, "role": role})
            self.assertEqual(st, 200)

        # 无身份头 -> 401
        st, body = self.req("POST", "/projects",
                            {"title": "x", "grantee": "y", "site": "z", "total_amount": 1000})
        self.assertEqual(st, 401)

        # 建协议
        st, project = self.req("POST", "/projects", {
            "title": "海岸红树林", "grantee": "某国海洋局", "site": "东岸",
            "total_amount": "8000", "currency": "USD"}, user="u1")
        self.assertEqual(st, 200)
        pid = project["id"]

        # 审核人越权建协议 -> 403
        st, _ = self.req("POST", "/projects", {
            "title": "x", "grantee": "y", "site": "z", "total_amount": 1}, user="u2")
        self.assertEqual(st, 403)

        # 里程碑 -> 材料 -> 受理 -> 核验
        st, m = self.req("POST", f"/projects/{pid}/milestones",
                         {"sequence": 1, "title": "护岸一期", "planned_amount": "3000"},
                         user="u1")
        self.assertEqual(st, 200)
        mid = m["id"]

        st, ev = self.req("POST", f"/milestones/{mid}/evidence",
                          {"doc_ref": "doc://http-1", "note": "首版被驳回"}, user="u1")
        ev1 = ev["evidence_versions"][0]["id"]
        st, _ = self.req("POST", f"/evidence/{ev1}/decision",
                         {"accept": False, "note": "缺票据"}, user="u2")
        self.assertEqual(st, 200)

        st, ev2 = self.req("POST", f"/milestones/{mid}/evidence",
                           {"doc_ref": "doc://http-2", "note": "补票据"}, user="u1")
        self.assertEqual(st, 200)
        self.assertEqual(ev2["evidence_versions"][0]["status"], "rejected")  # 驳回版仍在
        ev2_id = ev2["evidence_versions"][1]["id"]
        self.req("POST", f"/evidence/{ev2_id}/decision", {"accept": True}, user="u2")
        st, _ = self.req("POST", f"/milestones/{mid}/verifications", {
            "evidence_id": ev2_id, "result": "pass", "site_actual": "东岸"}, user="u2")
        self.assertEqual(st, 200)

        # 申请 -> 批准 -> 回执（重复回执必须幂等）
        st, order = self.req("POST", f"/projects/{pid}/payments",
                             {"milestone_id": mid, "amount": "3000"}, user="u1")
        oid = order["id"]
        st, _ = self.req("POST", f"/payments/{oid}/approve", {}, user="u2")
        self.assertEqual(st, 200)
        # 财务凭证迟到：批准时无回执，payable 挂账
        st, trace = self.req("GET", f"/projects/{pid}/trace")
        self.assertEqual(trace["reconciliation"]["ledger"]["payable"], "3000.00")

        receipt = {"receipt_no": "HTTP-RCP-1", "amount": "3000"}
        st1, paid1 = self.req("POST", f"/payments/{oid}/receipts", receipt, user="u3")
        st2, paid2 = self.req("POST", f"/payments/{oid}/receipts", receipt, user="u3")
        self.assertEqual((st1, st2), (200, 200))
        self.assertEqual((paid1["status"], paid2["status"]), ("disbursed", "disbursed"))
        self.assertEqual(len(paid2["receipts"]), 1)

        st, _ = self.req("POST", f"/payments/{oid}/reconcile", {}, user="u3")
        st, trace = self.req("GET", f"/projects/{pid}/trace")
        self.assertTrue(trace["reconciliation"]["balanced"], trace["reconciliation"])

        # 事件流包含幂等重放留痕
        st, events = self.req("GET", f"/projects/{pid}/events")
        actions = {e["action"] for e in events["events"]}
        self.assertIn("receipt_replayed_idempotent", actions)

        # 时钟推进
        st, clk = self.req("POST", "/admin/clock/advance", {"days": 3}, user="u1")
        self.assertEqual(st, 200)
        self.assertIn("2026-01-04", clk["now"])


if __name__ == "__main__":
    unittest.main()
