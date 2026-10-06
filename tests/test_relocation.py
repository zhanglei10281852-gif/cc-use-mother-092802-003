"""灾情改址与暂停/恢复：原计划与新计划之间的资金责任必须清楚，账目对平。"""

import unittest

from climate_fund.errors import ConflictError
from helpers import FINANCE, OFFICER, REVIEWER, build_app, seed_ready_milestone


class DisasterRelocationTests(unittest.TestCase):
    def setUp(self):
        self.app = build_app()
        # 总额 10000，阶段一 4000
        self.project, self.m, _ = seed_ready_milestone(
            self.app, total="10000", planned="4000", site="河谷旧址")
        self.s = self.app.service

    def test_relocation_requires_suspension_and_assigns_responsibility(self):
        s = self.s
        # 暂停期间不能改址（未暂停先试）
        with self.assertRaises(ConflictError):
            s.relocate_by_disaster(OFFICER, self.project["id"],
                                   new_site="高地新址", new_total_amount="12000")

        # 阶段一部分支付：申请 4000、批准、先放款 3000（回执 3000，余下另开）
        # 本系统回执金额须等于指令额，故用两笔指令体现部分支付：
        o1 = s.request_payment(OFFICER, self.project["id"], self.m["id"], "3000")
        s.approve_payment(REVIEWER, o1["id"])
        s.record_receipt(FINANCE, o1["id"], receipt_no="OLD-1", amount="3000")
        s.reconcile_payment(FINANCE, o1["id"])
        o2 = s.request_payment(OFFICER, self.project["id"], self.m["id"], "1000")
        s.approve_payment(REVIEWER, o2["id"])  # 已批准但回执未到 -> payable

        # 灾害发生：暂停项目，在途款项连带冻结
        s.suspend_project(OFFICER, self.project["id"], reason="洪水淹没旧址")
        self.assertEqual(s.get_payment_order(o2["id"])["status"], "frozen")
        self.assertEqual(s.get_milestone(self.m["id"])["state"], "frozen")

        # 暂停期间不能申请/批准新款项
        with self.assertRaises(ConflictError):
            s.request_payment(OFFICER, self.project["id"], self.m["id"], "500")

        # 改址：新总额 12000（追加 2000 搬迁与重建费用）
        amend = s.relocate_by_disaster(
            OFFICER, self.project["id"], new_site="高地新址", new_total_amount="12000",
            note="旧址冲毁，整体搬迁")
        self.assertEqual(amend["revision"], 2)
        self.assertEqual(amend["old_site"], "河谷旧址")
        self.assertEqual(amend["new_site"], "高地新址")
        # 原计划已承担责任 = 已核销 3000 + 冻结 1000 = 4000
        self.assertEqual(amend["old_plan_responsibility"], "4000.00")
        # 新计划责任 = 12000 - 4000 = 8000
        self.assertEqual(amend["new_plan_responsibility"], "8000.00")

        # 恢复项目：项目开门，但冻结款不自动恢复
        s.resume_project(OFFICER, self.project["id"], note="新址具备实施条件")
        pj = s.get_project(self.project["id"])
        self.assertEqual(pj["status"], "active")
        self.assertEqual(pj["current_site"], "高地新址")
        self.assertEqual(pj["plan_revision"], 2)
        self.assertEqual(s.get_payment_order(o2["id"])["status"], "frozen")

        # 旧计划里程碑（改址前已核验）的冻结款可恢复，回到待审批，按新计划重新批准
        s.resume_payment(OFFICER, o2["id"], note="旧址已完成部分继续支付尾款待重审")
        self.assertEqual(s.get_payment_order(o2["id"])["status"], "requested")
        s.approve_payment(REVIEWER, o2["id"])
        s.record_receipt(FINANCE, o2["id"], receipt_no="TAIL-1", amount="1000")
        s.reconcile_payment(FINANCE, o2["id"])

        rep = s.reconciliation(self.project["id"])
        self.assertTrue(rep["balanced"], rep)
        # 已核销 4000（原计划责任），新计划预算 8000 仍在 budget
        self.assertEqual(rep["ledger"]["reconciled"], "4000.00")
        self.assertEqual(rep["ledger"]["budget"], "8000.00")

    def test_new_work_after_relocation_uses_new_revision_and_budget(self):
        s = self.s
        o1 = s.request_payment(OFFICER, self.project["id"], self.m["id"], "4000")
        s.approve_payment(REVIEWER, o1["id"])
        s.record_receipt(FINANCE, o1["id"], receipt_no="OLD-X", amount="4000")
        s.reconcile_payment(FINANCE, o1["id"])

        s.suspend_project(OFFICER, self.project["id"], reason="泥石流")
        s.relocate_by_disaster(OFFICER, self.project["id"],
                               new_site="安置点", new_total_amount="10000")
        s.resume_project(OFFICER, self.project["id"])

        # 旧计划阶段一虽已付款结清；旧版本上不能再申请新款项（无改址后核验）
        with self.assertRaises(ConflictError):
            s.request_payment(OFFICER, self.project["id"], self.m["id"], "100")

        # 新计划版本 r2 下建立新阶段
        m2 = s.add_milestone(OFFICER, self.project["id"], sequence=2,
                             title="新址重建一期", planned_amount="3000", plan_revision=2)
        ev = s.submit_evidence(OFFICER, m2["id"], doc_ref="doc://new1")
        ev_id = ev["evidence_versions"][0]["id"]
        s.decide_evidence(REVIEWER, ev_id, accept=True)
        s.record_verification(REVIEWER, m2["id"], evidence_id=ev_id,
                              result="pass", site_actual="安置点")
        o2 = s.request_payment(OFFICER, self.project["id"], m2["id"], "3000")
        s.approve_payment(REVIEWER, o2["id"])
        s.record_receipt(FINANCE, o2["id"], receipt_no="NEW-1", amount="3000")
        s.reconcile_payment(FINANCE, o2["id"])

        rep = s.reconciliation(self.project["id"])
        self.assertTrue(rep["balanced"], rep)
        self.assertEqual(rep["ledger"]["reconciled"], "7000.00")
        self.assertEqual(rep["ledger"]["budget"], "3000.00")

        # 修订链可追溯
        amends = s.list_amendments(self.project["id"])
        self.assertEqual(len(amends), 1)
        self.assertEqual(amends[0]["reason"], "natural_disaster_relocation")

    def test_relocation_below_committed_responsibility_rejected(self):
        s = self.s
        o1 = s.request_payment(OFFICER, self.project["id"], self.m["id"], "4000")
        s.approve_payment(REVIEWER, o1["id"])
        s.suspend_project(OFFICER, self.project["id"], reason="灾")
        # 已承担 4000，却想把新总额压到 3000 -> 责任无法界定
        with self.assertRaises(Exception):
            s.relocate_by_disaster(OFFICER, self.project["id"],
                                   new_site="x", new_total_amount="3000")


if __name__ == "__main__":
    unittest.main()
