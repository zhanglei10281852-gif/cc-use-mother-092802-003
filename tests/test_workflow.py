"""端到端业务流程：材料先到、核验晚到、部分支付、批准到核销全程对平。"""

import unittest
from decimal import Decimal

from helpers import FINANCE, OFFICER, REVIEWER, build_app, seed_ready_milestone


class HappyPathTests(unittest.TestCase):
    def setUp(self):
        self.app = build_app()
        self.project, self.m, _ = seed_ready_milestone(self.app)
        self.s = self.app.service

    def test_full_disbursement_to_reconcile_is_balanced(self):
        # 申请可以在现场核验之后；此处核验已通过
        order = self.s.request_payment(OFFICER, self.project["id"], self.m["id"], "4000")
        self.assertEqual(order["status"], "requested")

        approved = self.s.approve_payment(REVIEWER, order["id"], note="同意拨付")
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["approved_by"], REVIEWER)

        paid = self.s.record_receipt(
            FINANCE, order["id"], receipt_no="RCP-001", amount="4000", note="首期款")
        self.assertEqual(paid["status"], "disbursed")

        done = self.s.reconcile_payment(FINANCE, order["id"], note="票据核销")
        self.assertEqual(done["status"], "reconciled")

        trace = self.s.project_fund_trace(self.project["id"])
        self.assertTrue(trace["reconciliation"]["balanced"], trace["reconciliation"])
        self.assertEqual(trace["reconciliation"]["ledger"]["reconciled"], "4000.00")
        self.assertEqual(trace["reconciliation"]["project_total"], "10000.00")
        # 余额 = 总额 - 已核销
        self.assertEqual(trace["project"]["balances"]["budget"], "6000.00")

        # 资金依据链完整
        po = trace["payment_orders"][0]
        actions = [e["action"] for e in po["events"]]
        self.assertEqual(
            actions,
            ["payment_requested", "payment_approved", "payment_disbursed", "payment_reconciled"],
        )
        self.assertEqual(po["receipts"][0]["receipt_no"], "RCP-001")
        self.assertIsNotNone(po["verification"])

    def test_request_allowed_before_field_verification_arrives(self):
        """财务/现场凭证到达晚：材料受理即可申请，但核验未到不能批准。"""
        s = self.app.service
        p = s.create_project(OFFICER, title="湿地恢复", grantee="某省林草局",
                             site="B市", total_amount="5000")
        m = s.add_milestone(OFFICER, p["id"], sequence=1, title="一期", planned_amount="2000")
        ev = s.submit_evidence(OFFICER, m["id"], doc_ref="doc://x1")
        ev_id = ev["evidence_versions"][0]["id"]
        s.decide_evidence(REVIEWER, ev_id, accept=True)

        # 核验结论尚未到达：允许申请
        order = s.request_payment(OFFICER, p["id"], m["id"], "2000")
        self.assertEqual(order["status"], "requested")

        # 批准被拒：核验未到
        from climate_fund.errors import ConflictError
        with self.assertRaises(ConflictError):
            s.approve_payment(REVIEWER, order["id"])

        # 核验晚到后再批准、放款、核销
        s.record_verification(REVIEWER, m["id"], evidence_id=ev_id,
                              result="pass", site_actual="B市")
        s.approve_payment(REVIEWER, order["id"])
        s.record_receipt(FINANCE, order["id"], receipt_no="RCP-LATE", amount="2000")
        s.reconcile_payment(FINANCE, order["id"])
        self.assertTrue(s.reconciliation(p["id"])["balanced"])

    def test_partial_payments_sum_to_stage_cap(self):
        """上一阶段款项部分支付：同一里程碑可拆多笔，总额不得超阶段上限与预算。"""
        s = self.app.service
        o1 = s.request_payment(OFFICER, self.project["id"], self.m["id"], "1500")
        s.approve_payment(REVIEWER, o1["id"])
        s.record_receipt(FINANCE, o1["id"], receipt_no="R-1", amount="1500")

        # 第二笔部分支付（财务凭证尚未到，先挂账）
        o2 = s.request_payment(OFFICER, self.project["id"], self.m["id"], "2000")
        s.approve_payment(REVIEWER, o2["id"])
        # o2 已批准但无回执 -> payable
        rep = s.reconciliation(self.project["id"])
        self.assertTrue(rep["balanced"], rep)
        self.assertEqual(rep["ledger"]["payable"], "2000.00")
        self.assertEqual(rep["ledger"]["disbursed"], "1500.00")

        # 再申请 600 会超阶段 4000 上限
        from climate_fund.errors import ConflictError
        with self.assertRaises(ConflictError):
            s.request_payment(OFFICER, self.project["id"], self.m["id"], "600")

        # 第二笔回执到达后核销
        s.record_receipt(FINANCE, o2["id"], receipt_no="R-2", amount="2000")
        s.reconcile_payment(FINANCE, o1["id"])
        s.reconcile_payment(FINANCE, o2["id"])
        rep = s.reconciliation(self.project["id"])
        self.assertTrue(rep["balanced"], rep)
        self.assertEqual(rep["ledger"]["reconciled"], "3500.00")

    def test_partial_verification_limits_payment(self):
        s = self.app.service
        p = s.create_project(OFFICER, title="防护林", grantee="某市", site="C镇",
                             total_amount="3000")
        m = s.add_milestone(OFFICER, p["id"], sequence=1, title="苗木", planned_amount="3000")
        ev = s.submit_evidence(OFFICER, m["id"], doc_ref="doc://tree")
        ev_id = ev["evidence_versions"][0]["id"]
        s.decide_evidence(REVIEWER, ev_id, accept=True)
        s.record_verification(REVIEWER, m["id"], evidence_id=ev_id, result="partial",
                              site_actual="C镇", verified_amount="2400",
                              note="枯死补种未完成，核减600")

        order = s.request_payment(OFFICER, p["id"], m["id"], "3000")
        from climate_fund.errors import ConflictError
        with self.assertRaises(ConflictError):
            s.approve_payment(REVIEWER, order["id"])  # 核定仅 2400

        # 取消原申请，按核定额重新申请
        s.cancel_payment(OFFICER, order["id"], note="按核定额改申请")
        order2 = s.request_payment(OFFICER, p["id"], m["id"], "2400")
        s.approve_payment(REVIEWER, order2["id"])
        rep = s.reconciliation(p["id"])
        self.assertTrue(rep["balanced"], rep)
        self.assertEqual(rep["ledger"]["payable"], "2400.00")
        self.assertEqual(rep["ledger"]["budget"], "600.00")


if __name__ == "__main__":
    unittest.main()
