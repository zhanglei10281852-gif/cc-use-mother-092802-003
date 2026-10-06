"""并发审批 / 并发回执幂等 / 预算竞争：用真实线程 + 文件库 + BEGIN IMMEDIATE 验证。"""

import tempfile
import threading
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from climate_fund.app import Application
from climate_fund.clock import Clock
from climate_fund.errors import ConflictError
from helpers import FINANCE, OFFICER, REVIEWER, REVIEWER2, seed_ready_milestone


class _Result:
    def __init__(self):
        self.values = []
        self.errors = []


def _run_threads(target, n=2):
    results = [_Result() for _ in range(n)]
    threads = [threading.Thread(target=target, args=(results[i],)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def _run_approve(app, order_id, actor, barrier):
    res = _Result()
    barrier.wait()
    try:
        out = app.service.approve_payment(actor, order_id)
        res.values.append(out["status"])
    except ConflictError as exc:
        res.errors.append(str(exc))
    return res


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "conc.db")
        self.app = Application(self.db_path, Clock())
        s = self.app.service
        s.create_user(OFFICER, "经办", "officer")
        s.create_user(REVIEWER, "审核甲", "reviewer")
        s.create_user(REVIEWER2, "审核乙", "reviewer")
        s.create_user(FINANCE, "财务", "finance")
        self.project, self.m, _ = seed_ready_milestone(self.app)

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def test_concurrent_approval_only_one_wins(self):
        """两个审核人同时批准同一笔：恰好一个成功，另一个得到冲突，账目不重不漏。"""
        s = self.app.service
        order = s.request_payment(OFFICER, self.project["id"], self.m["id"], "4000")

        barrier = threading.Barrier(2)

        def worker(res: _Result, actor: str):
            svc = self.app.service  # 线程内惰性连接
            barrier.wait()
            try:
                out = svc.approve_payment(actor, order["id"])
                res.values.append(out["status"])
            except ConflictError as exc:
                res.errors.append(str(exc))

        results = []
        threads = [
            threading.Thread(target=lambda: results.append(_run_approve(self.app, order["id"], REVIEWER, barrier))),
            threading.Thread(target=lambda: results.append(_run_approve(self.app, order["id"], REVIEWER2, barrier))),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ok = [r for r in results if r.values]
        bad = [r for r in results if r.errors]
        self.assertEqual(len(ok), 1, results)
        self.assertEqual(len(bad), 1, results)
        self.assertEqual(ok[0].values, ["approved"])

        rep = self.app.service.reconciliation(self.project["id"])
        self.assertTrue(rep["balanced"], rep)
        self.assertEqual(rep["orders_by_status"].get("approved"), "4000.00")
        self.assertNotIn("requested", rep["orders_by_status"])

    def test_concurrent_duplicate_receipt_is_idempotent(self):
        """同一回执号并发提交两次：只放一次款，两边都拿到成功结果（幂等）。"""
        s = self.app.service
        order = s.request_payment(OFFICER, self.project["id"], self.m["id"], "4000")
        s.approve_payment(REVIEWER, order["id"])
        barrier = threading.Barrier(2)

        def worker(res: _Result):
            barrier.wait()
            try:
                out = self.app.service.record_receipt(
                    FINANCE, order["id"], receipt_no="DUP-RCP-1", amount="4000")
                res.values.append(out["status"])
            except Exception as exc:  # noqa: BLE001
                res.errors.append(repr(exc))

        results = _run_threads(worker)
        self.assertEqual([e for r in results for e in r.errors], [])
        self.assertEqual([v for r in results for v in r.values],
                         ["disbursed", "disbursed"])

        # 物理层：只有一行回执，账本只过账一次
        conn = self.app.connection
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM payment_receipts WHERE receipt_no='DUP-RCP-1'"
        ).fetchone()["c"]
        self.assertEqual(n, 1)
        rep = self.app.service.reconciliation(self.project["id"])
        self.assertTrue(rep["balanced"], rep)
        self.assertEqual(rep["ledger"]["disbursed"], "4000.00")

    def test_concurrent_requests_cannot_overspend_budget(self):
        """两笔各自合规、合计超预算的并发申请：只能成功一笔。"""
        s = self.app.service
        # 项目总额 10000，阶段一 4000；再建阶段二 4000，两笔 4000 同时申请时
        # 剩余预算 6000，必须挡住一笔
        m2 = s.add_milestone(OFFICER, self.project["id"], sequence=2,
                             title="二期设备", planned_amount="4000")
        ev2 = s.submit_evidence(OFFICER, m2["id"], doc_ref="doc://s2")
        ev2_id = ev2["evidence_versions"][0]["id"]
        s.decide_evidence(REVIEWER, ev2_id, accept=True)
        s.record_verification(REVIEWER, m2["id"], evidence_id=ev2_id,
                              result="pass", site_actual="A省原址")

        o1 = s.request_payment(OFFICER, self.project["id"], self.m["id"], "4000")
        s.approve_payment(REVIEWER, o1["id"])
        s.record_receipt(FINANCE, o1["id"], receipt_no="PAID-1", amount="4000")
        # 此刻预算余 6000，两笔 4000 并发
        barrier = threading.Barrier(2)

        def worker(res: _Result, milestone_id):
            barrier.wait()
            try:
                out = self.app.service.request_payment(
                    OFFICER, self.project["id"], milestone_id, "4000")
                res.values.append(out["id"])
            except ConflictError as exc:
                res.errors.append(str(exc))

        results = []
        t1 = threading.Thread(target=lambda: results.append(
            _run_one(self.app, self.project["id"], self.m["id"], barrier)))
        t2 = threading.Thread(target=lambda: results.append(
            _run_one(self.app, self.project["id"], m2["id"], barrier)))
        t1.start(); t2.start(); t1.join(); t2.join()

        ok = [r for r in results if r.values]
        bad = [r for r in results if r.errors]
        self.assertEqual(len(ok), 1, results)
        self.assertEqual(len(bad), 1, results)
        rep = self.app.service.reconciliation(self.project["id"])
        self.assertTrue(rep["balanced"], rep)
        # 已付 4000 + 新申请 4000，预算余 2000
        self.assertEqual(rep["ledger"]["budget"], "2000.00")


def _run_one(app, project_id, milestone_id, barrier):
    res = _Result()
    barrier.wait()
    try:
        out = app.service.request_payment(OFFICER, project_id, milestone_id, "4000")
        res.values.append(out["id"])
    except ConflictError as exc:
        res.errors.append(str(exc))
    return res


if __name__ == "__main__":
    unittest.main()
