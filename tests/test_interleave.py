"""交织场景：并发审批、部分支付、灾情改址同时发生，每一步后账目必须对平。"""

import tempfile
import threading
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from climate_fund.app import Application  # noqa: E402
from climate_fund.clock import Clock  # noqa: E402
from climate_fund.errors import ConflictError  # noqa: E402
from helpers import FINANCE, OFFICER, REVIEWER, REVIEWER2  # noqa: E402


class InterleavingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = Application(str(Path(self.tmp.name) / "interleave.db"), Clock())
        s = self.app.service
        s.create_user(OFFICER, "经办", "officer")
        s.create_user(REVIEWER, "审核甲", "reviewer")
        s.create_user(REVIEWER2, "审核乙", "reviewer")
        s.create_user(FINANCE, "财务", "finance")

        self.p = s.create_project(OFFICER, title="跨境光伏微网", grantee="岛国能源局",
                                  site="北岸村", total_amount="20000")
        # 阶段一 8000，阶段二 6000
        self.m1 = s.add_milestone(OFFICER, self.p["id"], sequence=1,
                                  title="组件到位", planned_amount="8000")
        self.m2 = s.add_milestone(OFFICER, self.p["id"], sequence=2,
                                  title="并网验收", planned_amount="6000")
        for m in (self.m1, self.m2):
            ev = s.submit_evidence(OFFICER, m["id"], doc_ref=f"doc://{m['sequence']}")
            ev_id = ev["evidence_versions"][0]["id"]
            s.decide_evidence(REVIEWER, ev_id, accept=True)
            s.record_verification(REVIEWER, m["id"], evidence_id=ev_id,
                                  result="pass", site_actual="北岸村")

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def assertBalanced(self):
        rep = self.app.service.reconciliation(self.p["id"])
        self.assertTrue(rep["balanced"], rep["errors"])
        return rep

    def test_interwoven_concurrency_partial_and_relocation(self):
        s = self.app.service
        app = self.app
        pid = self.p["id"]

        # 1) 阶段一部分支付：3000 已放款并核销，3000 已批准待回执
        o_paid = s.request_payment(OFFICER, pid, self.m1["id"], "3000")
        s.approve_payment(REVIEWER, o_paid["id"])
        s.record_receipt(FINANCE, o_paid["id"], receipt_no="I-001", amount="3000")
        s.reconcile_payment(FINANCE, o_paid["id"])

        o_pending = s.request_payment(OFFICER, pid, self.m1["id"], "3000")
        s.approve_payment(REVIEWER, o_pending["id"])

        # 2) 阶段二两笔并发申请（各 3000，合计 6000，预算此时余 14000，都应成功）
        barrier = threading.Barrier(2)
        results = []

        def req(mid):
            r = {}
            barrier.wait()
            try:
                r["order"] = app.service.request_payment(OFFICER, pid, mid, "3000")
            except ConflictError as exc:
                r["error"] = str(exc)
            results.append(r)

        t1 = threading.Thread(target=req, args=(self.m1["id"],))   # 阶段一尾段
        t2 = threading.Thread(target=req, args=(self.m2["id"],))   # 阶段二首款
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(len(results), 2)
        # 阶段一累计：3000+3000+3000 = 9000 > 8000，必有一笔被拒；阶段二成功
        errs = [r.get("error") for r in results if "error" in r]
        oks = [r["order"] for r in results if "order" in r]
        self.assertEqual(len(errs), 1)
        self.assertEqual(len(oks), 1)
        self.assertBalanced()

        o_m2 = oks[0]

        # 3) 两个审核人并发批准阶段二款项；只有一人成功
        barrier = threading.Barrier(2)
        approve_results = []

        def approve(actor):
            r = {}
            barrier.wait()
            try:
                r["ok"] = app.service.approve_payment(actor, o_m2["id"])
            except ConflictError as exc:
                r["error"] = str(exc)
            approve_results.append(r)

        t1 = threading.Thread(target=approve, args=(REVIEWER,))
        t2 = threading.Thread(target=approve, args=(REVIEWER2,))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(len([r for r in approve_results if "ok" in r]), 1)
        self.assertEqual(len([r for r in approve_results if "error" in r]), 1)
        self.assertBalanced()

        # 4) 灾害突发：暂停（两笔在途款项连带冻结），改址，追加预算 4000
        s.suspend_project(OFFICER, pid, reason="台风登陆，北岸村受淹")
        frozen = [o for o in s.list_payment_orders(pid) if o["status"] == "frozen"]
        # o_pending(approved,3000) 与 o_m2(approved,3000) 被冻结；已核销的不动
        self.assertEqual(len(frozen), 2)
        self.assertBalanced()

        amend = s.relocate_by_disaster(OFFICER, pid, new_site="南坡高地",
                                       new_total_amount="24000", note="整体搬迁重建")
        # 原计划已承担 = 20000 - budget(11000) = 9000
        # budget = 20000 - 3000(核销) - 3000(payable->冻结) - 3000(阶段二冻结) = 11000
        self.assertEqual(amend["old_plan_responsibility"], "9000.00")
        self.assertEqual(amend["new_plan_responsibility"], "15000.00")
        rep = self.assertBalanced()
        self.assertEqual(rep["project_total"], "24000.00")
        self.assertEqual(rep["ledger"]["frozen"], "6000.00")

        # 5) 恢复项目，逐笔恢复冻结款并重审；尾款待回执
        s.resume_project(OFFICER, pid, note="新址开工")
        for o in s.list_payment_orders(pid):
            if o["status"] == "frozen":
                s.resume_payment(OFFICER, o["id"], note="按新计划重审")
        self.assertBalanced()

        # 审核人对恢复后的款项逐笔批准（按新计划责任）
        for o in s.list_payment_orders(pid):
            if o["status"] == "requested":
                s.approve_payment(REVIEWER, o["id"])
        self.assertBalanced()

        # 6) 财务凭证此刻才到达：两笔冻结-恢复款放款
        pending = [o for o in s.list_payment_orders(pid) if o["status"] == "approved"]
        for i, o in enumerate(pending):
            s.record_receipt(FINANCE, o["id"], receipt_no=f"I-TAIL-{i}", amount=o["amount"])
            s.reconcile_payment(FINANCE, o["id"])
        self.assertBalanced()

        # 7) 重复回执再放一次 -> 幂等，不新增放款
        tail_no = s.get_payment_order(o_pending["id"])["receipts"][0]["receipt_no"]
        again = s.record_receipt(FINANCE, o_pending["id"],
                                 receipt_no=tail_no, amount="3000")
        self.assertEqual(again["status"], "reconciled")
        self.assertEqual(len(again["receipts"]), 1)

        rep = self.assertBalanced()
        # 最终：已核销 9000（原计划责任），新计划预算 15000 未动用
        self.assertEqual(rep["ledger"]["reconciled"], "9000.00")
        self.assertEqual(rep["ledger"]["budget"], "15000.00")
        for acc in ("requested", "payable", "frozen", "disbursed"):
            self.assertEqual(rep["ledger"][acc], "0.00", acc)

        # 全程事件链可回放，三方对平
        trace = s.project_fund_trace(pid)
        self.assertGreaterEqual(len(trace["amendments"]), 1)
        self.assertTrue(trace["reconciliation"]["balanced"])


if __name__ == "__main__":
    unittest.main()
