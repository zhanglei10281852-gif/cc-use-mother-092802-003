"""HTTP API 测试：资金轨迹展示与回执幂等。"""

import http.client
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from climate_fund import Database, GrantService, ManualClock  # noqa: E402
from climate_fund.api import make_handler  # noqa: E402


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        db = Database(str(Path(cls.dir) / "api.db"))
        cls.svc = GrantService(db, ManualClock(datetime(2026, 2, 1, 9, 0, 0)))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.svc))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _call(self, method, path, body=None, actor=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"}
        if actor:
            headers["X-Actor-Id"] = actor
        conn.request(method, path,
                     body=json.dumps(body) if body is not None else None,
                     headers=headers)
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, payload

    def test_fund_trail_and_idempotent_receipt_over_http(self):
        # 准备用户与项目
        for uid, role in (("o1", "OFFICER"), ("r1", "REVIEWER"), ("f1", "FINANCE")):
            status, _ = self._call("POST", "/users", {"id": uid, "name": uid, "role": role})
            self.assertEqual(status, 201)
        status, project = self._call("POST", "/projects", {
            "code": "API-001", "name": "API 项目", "partner": "受援方",
            "location": "A 村", "total_amount": 500_000,
            "milestones": [{"title": "M1", "amount": 500_000}]}, actor="o1")
        self.assertEqual(status, 201)
        pid = project["id"]
        self.assertEqual(self._call("POST", f"/projects/{pid}/activate", actor="r1")[0], 200)

        # 无调用人头 → 400；经办越权批准 → 403
        self.assertEqual(self._call("POST", f"/projects/{pid}/suspend", {"reason": "x"})[0], 400)
        mid = self._call("GET", f"/projects/{pid}/milestones")[1][0]["id"]
        status, ev = self._call("POST", f"/milestones/{mid}/evidence",
                                {"content_uri": "uri://v1"}, actor="o1")
        self.assertEqual(status, 201)
        self.assertEqual(
            self._call("POST", f"/evidence/{ev['id']}/review",
                       {"approve": True}, actor="o1")[0], 403)
        self._call("POST", f"/evidence/{ev['id']}/review", {"approve": True}, actor="r1")
        for kind in ("FIELD", "FINANCIAL"):
            self._call("POST", f"/milestones/{mid}/verifications",
                       {"kind": kind, "conclusion": "PASS"}, actor="r1")
        status, ins = self._call("POST", f"/milestones/{mid}/instructions",
                                 {"amount": 500_000}, actor="o1")
        self.assertEqual(status, 201)
        iid = ins["id"]
        self._call("POST", f"/instructions/{iid}/approve", actor="r1")

        # 回执：首次 201，重复报送 200 且 created=false、同一回执 id
        status1, p1 = self._call("POST", f"/instructions/{iid}/receipts",
                                 {"external_ref": "B-1", "amount": 200_000}, actor="f1")
        status2, p2 = self._call("POST", f"/instructions/{iid}/receipts",
                                 {"external_ref": "B-1", "amount": 200_000}, actor="f1")
        self.assertEqual((status1, p1["created"]), (201, True))
        self.assertEqual((status2, p2["created"]), (200, False))
        self.assertEqual(p1["receipt"]["id"], p2["receipt"]["id"])
        self._call("POST", f"/instructions/{iid}/receipts",
                   {"external_ref": "B-2", "amount": 300_000}, actor="f1")
        self._call("POST", f"/instructions/{iid}/writeoff", actor="r1")

        # 资金轨迹：从批准到核销的依据与状态变化
        status, trail = self._call("GET", f"/instructions/{iid}/trail")
        self.assertEqual(status, 200)
        self.assertEqual(trail["instruction"]["status"], "RECONCILED")
        self.assertEqual([e["action"] for e in trail["timeline"]],
                         ["CREATE", "APPROVE", "RECEIPT", "RECEIPT", "WRITEOFF"])
        self.assertEqual(len(trail["receipts"]), 2)

        status, rec = self._call("GET", f"/projects/{pid}/reconciliation")
        self.assertEqual(status, 200)
        self.assertTrue(rec["balanced"], rec["checks"])
        self.assertEqual(rec["paid"], 500_000)
        self.assertEqual(rec["outstanding_unreconciled"], 0)

        status, trail = self._call("GET", f"/projects/{pid}/fund-trail")
        self.assertEqual(status, 200)
        self.assertEqual(trail["project"]["id"], pid)
        self.assertEqual(len(trail["revisions"][0]["milestones"]), 1)


if __name__ == "__main__":
    unittest.main()
