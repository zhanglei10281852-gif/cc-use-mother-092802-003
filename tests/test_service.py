"""拨款管理服务层测试：生命周期、权限、幂等、并发、暂停恢复与对账。"""

import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from climate_fund import (  # noqa: E402
    ConflictError,
    Database,
    GrantService,
    ManualClock,
    PermissionDenied,
    StateError,
    ValidationError,
)


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = Database(str(Path(self.dir) / "test.db"))
        self.clock = ManualClock(datetime(2026, 1, 10, 8, 0, 0))
        self.svc = GrantService(self.db, self.clock)
        self.svc.create_user("officer1", "经办甲", "OFFICER")
        self.svc.create_user("officer2", "经办乙", "OFFICER")
        self.svc.create_user("reviewer1", "审核甲", "REVIEWER")
        self.svc.create_user("reviewer2", "审核乙", "REVIEWER")
        self.svc.create_user("finance1", "财务甲", "FINANCE")
        self.svc.create_user("admin1", "管理员", "ADMIN")

    # ---------------------------------------------------------- 场景辅助
    def _project(self, milestones=None, total=1_000_000):
        milestones = milestones or [
            {"title": "M1", "amount": 400_000},
            {"title": "M2", "amount": 350_000},
            {"title": "M3", "amount": 250_000},
        ]
        project = self.svc.create_project(
            "officer1", code="P-001", name="气候项目", partner="受援方",
            location="A 村", total_amount=total, milestones=milestones)
        self.svc.activate_project("reviewer1", project["id"])
        return project

    def _verified_milestone(self, project_id, index=0):
        """把第 index 个里程碑推进到 VERIFIED。"""
        ms = self.svc.list_milestones(project_id)[index]
        ev = self.svc.submit_evidence("officer1", ms["id"], content_uri="uri://ev")
        self.svc.review_evidence("reviewer1", ev["id"], approve=True)
        self.svc.record_verification("reviewer1", ms["id"], kind="FIELD", conclusion="PASS")
        self.svc.record_verification("reviewer1", ms["id"], kind="FINANCIAL", conclusion="PASS")
        return ms["id"]

    def _approved_instruction(self, project_id, index, amount):
        mid = self._verified_milestone(project_id, index)
        ins = self.svc.create_instruction("officer1", mid, amount=amount)
        self.svc.approve_instruction("reviewer1", ins["id"])
        return ins["id"]


class LifecycleTests(ServiceTestBase):
    def test_full_lifecycle_and_trail(self):
        project = self._project()
        pid = project["id"]
        mid = self.svc.list_milestones(pid)[0]["id"]

        # 材料 v1 被驳回，补交 v2 批准
        ev1 = self.svc.submit_evidence("officer1", mid, content_uri="uri://v1")
        self.svc.review_evidence("reviewer1", ev1["id"], approve=False, comment="缺清单")
        ev2 = self.svc.submit_evidence("officer1", mid, content_uri="uri://v2")
        self.svc.review_evidence("reviewer2", ev2["id"], approve=True)

        # 现场核验与财务凭证不同时间到达
        self.clock.advance(days=2)
        self.svc.record_verification("reviewer1", mid, kind="FIELD", conclusion="PASS")
        self.assertEqual(self.svc.list_milestones(pid)[0]["status"], "EVIDENCE_APPROVED")
        self.clock.advance(days=5)
        self.svc.record_verification("reviewer1", mid, kind="FINANCIAL", conclusion="PASS")
        self.assertEqual(self.svc.list_milestones(pid)[0]["status"], "VERIFIED")

        # 指令 → 批准 → 两次部分支付 → 核销
        ins = self.svc.create_instruction("officer1", mid, amount=400_000)
        self.svc.approve_instruction("reviewer1", ins["id"])
        _, c1 = self.svc.record_receipt("finance1", ins["id"],
                                        external_ref="R1", amount=150_000)
        self.assertTrue(c1)
        self.assertEqual(self.svc.get_instruction(ins["id"])["status"], "PARTIALLY_PAID")
        self.svc.record_receipt("finance1", ins["id"], external_ref="R2", amount=250_000)
        self.assertEqual(self.svc.get_instruction(ins["id"])["status"], "PAID")
        self.svc.writeoff_instruction("reviewer1", ins["id"])
        self.assertEqual(self.svc.get_instruction(ins["id"])["status"], "RECONCILED")
        self.assertEqual(self.svc.list_milestones(pid)[0]["status"], "RECONCILED")

        # 轨迹：依据（材料版本、核验）与时间线齐全
        trail = self.svc.instruction_trail(ins["id"])
        self.assertEqual([e["version"] for e in trail["basis"]["evidence"]], [1, 2])
        self.assertEqual(len(trail["basis"]["verifications"]), 2)
        self.assertEqual([r["external_ref"] for r in trail["receipts"]], ["R1", "R2"])
        actions = [e["action"] for e in trail["timeline"]]
        self.assertEqual(actions, ["CREATE", "APPROVE", "RECEIPT", "RECEIPT", "WRITEOFF"])

    def test_rejected_evidence_version_is_preserved(self):
        project = self._project()
        mid = self.svc.list_milestones(project["id"])[0]["id"]
        ev1 = self.svc.submit_evidence("officer1", mid, content_uri="uri://v1")
        self.svc.review_evidence("reviewer1", ev1["id"], approve=False, comment="不合格")

        ev2 = self.svc.submit_evidence("officer2", mid, content_uri="uri://v2")
        versions = self.svc.list_evidence(mid)
        # 补交产生新版本，被驳回的 v1 原样保留
        self.assertEqual([(v["version"], v["status"]) for v in versions],
                         [(1, "REJECTED"), (2, "SUBMITTED")])
        self.assertEqual(versions[0]["review_comment"], "不合格")
        # 已驳回的版本不可再审核（不可被覆盖/改写）
        with self.assertRaises(ConflictError):
            self.svc.review_evidence("reviewer1", ev1["id"], approve=True)
        # 审核中的版本不可重复提交覆盖
        with self.assertRaises(ConflictError):
            self.svc.submit_evidence("officer1", mid, content_uri="uri://v3")
        self.svc.review_evidence("reviewer1", ev2["id"], approve=True)
        # 批准后版本同样不可再改
        with self.assertRaises(ConflictError):
            self.svc.review_evidence("reviewer2", ev2["id"], approve=False)

    def test_verification_binds_to_current_evidence_version(self):
        project = self._project()
        mid = self.svc.list_milestones(project["id"])[0]["id"]
        ev1 = self.svc.submit_evidence("officer1", mid, content_uri="uri://v1")
        self.svc.review_evidence("reviewer1", ev1["id"], approve=True)
        self.svc.record_verification("reviewer1", mid, kind="FIELD", conclusion="PASS")
        self.svc.record_verification("reviewer1", mid, kind="FINANCIAL", conclusion="PASS")
        self.assertEqual(self.svc.list_milestones(project["id"])[0]["status"], "VERIFIED")

        # 补交新版本后，原核验结论绑定旧版本，不再作数
        ev2 = self.svc.submit_evidence("officer1", mid, content_uri="uri://v2")
        self.svc.review_evidence("reviewer1", ev2["id"], approve=True)
        self.assertEqual(self.svc.list_milestones(project["id"])[0]["status"],
                         "EVIDENCE_APPROVED")
        with self.assertRaises(StateError):
            self.svc.create_instruction("officer1", mid, amount=100)
        # 旧批准版本作废留痕
        versions = self.svc.list_evidence(mid)
        self.assertEqual(versions[0]["status"], "SUPERSEDED")

    def test_instruction_limits(self):
        project = self._project()
        mid = self._verified_milestone(project["id"], 0)
        with self.assertRaises(ValidationError):
            self.svc.create_instruction("officer1", mid, amount=400_001)
        self.svc.create_instruction("officer1", mid, amount=300_000)
        with self.assertRaises(ValidationError):
            self.svc.create_instruction("officer1", mid, amount=100_001)

    def test_overpayment_rejected(self):
        project = self._project()
        iid = self._approved_instruction(project["id"], 0, 400_000)
        self.svc.record_receipt("finance1", iid, external_ref="R1", amount=300_000)
        with self.assertRaises(StateError):
            self.svc.record_receipt("finance1", iid, external_ref="R2", amount=100_001)


class PermissionTests(ServiceTestBase):
    def test_roles_cannot_substitute_each_other(self):
        project = self._project()
        pid = project["id"]
        mid = self.svc.list_milestones(pid)[0]["id"]
        ev = self.svc.submit_evidence("officer1", mid, content_uri="uri://v1")

        # 经办不能审核材料/批准指令/登记回执
        with self.assertRaises(PermissionDenied):
            self.svc.review_evidence("officer2", ev["id"], approve=True)
        # 审核不能提交材料/发起指令/登记回执
        with self.assertRaises(PermissionDenied):
            self.svc.submit_evidence("reviewer1", mid, content_uri="uri://v2")
        self.svc.review_evidence("reviewer1", ev["id"], approve=True)
        self.svc.record_verification("reviewer1", mid, kind="FIELD", conclusion="PASS")
        self.svc.record_verification("reviewer1", mid, kind="FINANCIAL", conclusion="PASS")
        with self.assertRaises(PermissionDenied):
            self.svc.create_instruction("reviewer1", mid, amount=1000)
        ins = self.svc.create_instruction("officer1", mid, amount=1000)
        with self.assertRaises(PermissionDenied):
            self.svc.approve_instruction("officer2", ins["id"])
        with self.assertRaises(PermissionDenied):
            self.svc.record_receipt("officer1", ins["id"], external_ref="R", amount=500)
        with self.assertRaises(PermissionDenied):
            self.svc.record_receipt("reviewer1", ins["id"], external_ref="R", amount=500)
        # 财务不能审核材料
        with self.assertRaises(PermissionDenied):
            self.svc.review_evidence("finance1", ev["id"], approve=False)

    def test_four_eyes_principle(self):
        project = self._project()
        pid = project["id"]
        mid = self.svc.list_milestones(pid)[0]["id"]
        # 管理员可执行任意角色动作，但仍受四眼原则约束
        ev = self.svc.submit_evidence("admin1", mid, content_uri="uri://v1")
        with self.assertRaises(PermissionDenied):
            self.svc.review_evidence("admin1", ev["id"], approve=True)
        self.svc.review_evidence("reviewer1", ev["id"], approve=True)
        self.svc.record_verification("reviewer1", mid, kind="FIELD", conclusion="PASS")
        self.svc.record_verification("reviewer1", mid, kind="FINANCIAL", conclusion="PASS")
        ins = self.svc.create_instruction("admin1", mid, amount=1000)
        with self.assertRaises(PermissionDenied):
            self.svc.approve_instruction("admin1", ins["id"])
        self.svc.approve_instruction("reviewer1", ins["id"])
        self.svc.record_receipt("finance1", ins["id"], external_ref="R1", amount=1000)
        with self.assertRaises(PermissionDenied):
            self.svc.writeoff_instruction("admin1", ins["id"])


class ReceiptTests(ServiceTestBase):
    def test_duplicate_receipt_is_idempotent(self):
        project = self._project()
        iid = self._approved_instruction(project["id"], 0, 400_000)
        r1, created1 = self.svc.record_receipt("finance1", iid,
                                               external_ref="BANK-9", amount=150_000)
        r2, created2 = self.svc.record_receipt("finance1", iid,
                                               external_ref="BANK-9", amount=150_000)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(r1["id"], r2["id"])
        # 金额只记一次
        self.assertEqual(self.svc.get_instruction(iid)["paid_amount"], 150_000)
        ledger = self.svc.ledger(project["id"])
        self.assertEqual(ledger["totals"].get("PAYMENT"), 150_000)
        # 同一回执号不同金额 → 冲突
        with self.assertRaises(ConflictError):
            self.svc.record_receipt("finance1", iid, external_ref="BANK-9", amount=150_001)


class SuspendResumeTests(ServiceTestBase):
    def test_suspend_freezes_and_blocks(self):
        project = self._project()
        pid = project["id"]
        iid = self._approved_instruction(pid, 0, 400_000)
        self.svc.record_receipt("finance1", iid, external_ref="R1", amount=100_000)
        self.svc.suspend_project("reviewer1", pid, reason="洪涝")

        self.assertEqual(self.svc.get_instruction(iid)["status"], "FROZEN")
        avail = self.svc.availability(pid)
        self.assertEqual(avail["frozen_unpaid"], 300_000)
        self.assertEqual(avail["available_to_apply"], 0)
        # 暂停期间：不能申请、不能付款、不能交材料
        with self.assertRaises(StateError):
            self.svc.record_receipt("finance1", iid, external_ref="R2", amount=100_000)
        with self.assertRaises(StateError):
            self.svc.create_instruction("officer1",
                                        self.svc.list_milestones(pid)[1]["id"], amount=1000)
        with self.assertRaises(StateError):
            self.svc.submit_evidence("officer1",
                                     self.svc.list_milestones(pid)[1]["id"],
                                     content_uri="uri://x")

    def test_resume_relocation_closes_funding_responsibility(self):
        project = self._project()
        pid = project["id"]
        iid = self._approved_instruction(pid, 0, 400_000)
        self.svc.record_receipt("finance1", iid, external_ref="R1", amount=400_000)
        self.svc.suspend_project("reviewer1", pid, reason="洪涝")

        # 池余 600,000：收回 150,000，承接 450,000，新计划预算必须闭合
        with self.assertRaises(ValidationError):
            self.svc.resume_project(
                "reviewer1", pid, new_location="B 村", reason="改址",
                new_milestones=[{"title": "X", "amount": 500_000}],
                recover_amount=150_000)
        with self.assertRaises(ValidationError):
            self.svc.resume_project(
                "reviewer1", pid, new_location="B 村", reason="改址",
                new_milestones=[{"title": "X", "amount": 450_000}],
                recover_amount=200_000)  # 收回超过池余 600,000? 否；但承接不等

        self.svc.resume_project(
            "reviewer1", pid, new_location="B 村", reason="DISASTER_RELOCATION",
            new_milestones=[{"title": "N1", "amount": 300_000},
                            {"title": "N2", "amount": 150_000}],
            recover_amount=150_000)
        project = self.svc.get_project(pid)
        self.assertEqual(project["status"], "ACTIVE")
        self.assertEqual(project["location"], "B 村")
        rev2 = project["revisions"][1]
        self.assertEqual(rev2["carried_from_previous"], 450_000)
        self.assertEqual(rev2["recovered_to_donor"], 150_000)
        self.assertEqual(rev2["new_funds"], 0)
        # 指令已全额付清，暂停/恢复不改变其 PAID 状态；已付资金仍归属原计划
        self.assertEqual(self.svc.get_instruction(iid)["status"], "PAID")
        # 恢复后，原计划下的已付指令照常核销
        self.svc.writeoff_instruction("reviewer1", iid)
        self.assertEqual(self.svc.get_instruction(iid)["status"], "RECONCILED")
        rec = self.svc.reconciliation(pid)
        self.assertEqual(rec["allocated"], 1_000_000)
        self.assertEqual(rec["recovered"], 150_000)
        self.assertEqual(rec["paid"], 400_000)
        self.assertEqual(rec["pool_balance"], 450_000)
        self.assertTrue(rec["balanced"])
        # 旧里程碑已取消，不能再用；新里程碑可正常推进
        old_mid = self.svc.list_milestones(pid)[0]["id"]
        with self.assertRaises(StateError):
            self.svc.submit_evidence("officer1", old_mid, content_uri="uri://x")

    def test_close_requires_clean_books(self):
        project = self._project()
        pid = project["id"]
        iid = self._approved_instruction(pid, 0, 400_000)
        self.svc.record_receipt("finance1", iid, external_ref="R1", amount=400_000)
        with self.assertRaises(StateError):  # 已付未核销
            self.svc.close_project("reviewer1", pid)
        self.svc.writeoff_instruction("reviewer1", iid)
        self.svc.close_project("reviewer1", pid)
        rec = self.svc.reconciliation(pid)
        # 剩余 600,000 收回出资方，池清零
        self.assertEqual(rec["pool_balance"], 0)
        self.assertEqual(rec["recovered"], 600_000)
        self.assertEqual(rec["outstanding_unreconciled"], 0)
        self.assertTrue(rec["balanced"])


class ConcurrencyTests(ServiceTestBase):
    def _run_parallel(self, fn, count=2):
        barrier = threading.Barrier(count)
        results, errors = [], []

        def worker():
            barrier.wait(timeout=10)
            try:
                results.append(fn())
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        return results, errors

    def test_concurrent_approval_single_winner(self):
        project = self._project()
        iid = self.svc.create_instruction(
            "officer1", self._verified_milestone(project["id"], 0), amount=100_000)["id"]

        results, errors = self._run_parallel(
            lambda: self.svc.approve_instruction("reviewer2", iid))
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ConflictError)
        self.assertEqual(self.svc.get_instruction(iid)["status"], "APPROVED")

    def test_concurrent_duplicate_receipt_idempotent(self):
        project = self._project()
        iid = self._approved_instruction(project["id"], 0, 400_000)

        results, errors = self._run_parallel(
            lambda: self.svc.record_receipt("finance1", iid,
                                            external_ref="BANK-X", amount=200_000))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        receipt_ids = {r[0]["id"] for r in results}
        created_flags = sorted(r[1] for r in results)
        self.assertEqual(len(receipt_ids), 1)          # 同一回执
        self.assertEqual(created_flags, [False, True])  # 只有一次真正入账
        self.assertEqual(self.svc.get_instruction(iid)["paid_amount"], 200_000)
        self.assertEqual(
            self.svc.ledger(project["id"])["totals"]["PAYMENT"], 200_000)

    def test_concurrent_receipts_never_overpay(self):
        project = self._project()
        iid = self._approved_instruction(project["id"], 0, 100_000)

        results, errors = self._run_parallel(
            lambda: self.svc.record_receipt("finance1", iid,
                                            external_ref=f"R-{threading.get_ident()}",
                                            amount=80_000))
        # 两笔 80,000 争抢 100,000 额度：一笔成功，一笔被条件更新拦下
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], StateError)
        self.assertEqual(self.svc.get_instruction(iid)["paid_amount"], 80_000)


class InterleavedReconciliationTests(ServiceTestBase):
    """并发审批、部分支付、灾情改址交织后，账目仍能对平。"""

    def test_interleaved_scenario_reconciles(self):
        project = self._project()
        pid = project["id"]

        # M1：驳回→补交→双通道核验→批准→两次部分支付（含一次重复报送）
        m1 = self.svc.list_milestones(pid)[0]["id"]
        ev = self.svc.submit_evidence("officer1", m1, content_uri="uri://m1v1")
        self.svc.review_evidence("reviewer1", ev["id"], approve=False, comment="缺材料")
        ev = self.svc.submit_evidence("officer1", m1, content_uri="uri://m1v2")
        self.svc.review_evidence("reviewer1", ev["id"], approve=True)
        self.clock.advance(days=2)
        self.svc.record_verification("reviewer1", m1, kind="FIELD", conclusion="PASS")
        self.clock.advance(days=3)
        self.svc.record_verification("reviewer1", m1, kind="FINANCIAL", conclusion="PASS")
        i1 = self.svc.create_instruction("officer1", m1, amount=400_000)
        self.svc.approve_instruction("reviewer1", i1["id"])
        self.svc.record_receipt("finance1", i1["id"], external_ref="B1", amount=150_000)
        self.svc.record_receipt("finance1", i1["id"], external_ref="B1", amount=150_000)
        self.svc.record_receipt("finance1", i1["id"], external_ref="B2", amount=250_000)

        # M2：部分支付 100,000 后灾情暂停
        m2 = self._verified_milestone(pid, 1)
        i2 = self.svc.create_instruction("officer1", m2, amount=350_000)
        self.svc.approve_instruction("reviewer2", i2["id"])
        self.svc.record_receipt("finance1", i2["id"], external_ref="B3", amount=100_000)
        self.svc.suspend_project("reviewer1", pid, reason="洪涝改址")

        # 恢复：池余 500,000，收回 150,000，承接 350,000 + 新增 100,000 = 450,000
        self.svc.resume_project(
            "reviewer1", pid, new_location="B 村", reason="DISASTER_RELOCATION",
            new_milestones=[{"title": "N1", "amount": 250_000},
                            {"title": "N2", "amount": 200_000}],
            recover_amount=150_000, new_funds=100_000)

        # 冻结指令在恢复时作废，已付 100,000 仍归属原计划并可核销
        self.assertEqual(self.svc.get_instruction(i2["id"])["status"], "CANCELLED")
        self.assertEqual(self.svc.get_instruction(i2["id"])["paid_amount"], 100_000)
        # 新计划两个里程碑全部履约、付款、核销
        for m in [m for m in self.svc.list_milestones(pid) if m["revision_no"] == 2]:
            ev = self.svc.submit_evidence("officer1", m["id"], content_uri="uri://n")
            self.svc.review_evidence("reviewer1", ev["id"], approve=True)
            self.svc.record_verification("reviewer1", m["id"], kind="FIELD", conclusion="PASS")
            self.svc.record_verification("reviewer1", m["id"], kind="FINANCIAL", conclusion="PASS")
            ins = self.svc.create_instruction("officer1", m["id"], amount=m["amount"])
            self.svc.approve_instruction("reviewer1", ins["id"])
            self.svc.record_receipt("finance1", ins["id"],
                                    external_ref=f"NB-{m['seq']}", amount=m["amount"])
            self.svc.writeoff_instruction("reviewer1", ins["id"])
        # 原计划两笔已付资金核销（i2 作废指令的已付部分同样可核销）
        self.svc.writeoff_instruction("reviewer1", i1["id"])
        self.svc.writeoff_instruction("reviewer2", i2["id"])
        self.svc.close_project("reviewer1", pid)

        rec = self.svc.reconciliation(pid)
        self.assertEqual(rec["allocated"], 1_100_000)   # 1,000,000 + 新增 100,000
        self.assertEqual(rec["paid"], 950_000)          # 400+100+250+200 (千分)
        self.assertEqual(rec["recovered"], 150_000)
        self.assertEqual(rec["written_off"], 950_000)
        self.assertEqual(rec["pool_balance"], 0)
        self.assertEqual(rec["outstanding_unreconciled"], 0)
        self.assertTrue(rec["balanced"], rec["checks"])
        # 新旧计划资金责任：v1 付 500,000，v2 付 450,000
        by_rev = {r["revision_no"]: r for r in rec["revisions"]}
        self.assertEqual(by_rev[1]["paid_under_revision"], 500_000)
        self.assertEqual(by_rev[2]["paid_under_revision"], 450_000)
        self.assertEqual(by_rev[2]["carried_from_previous"], 350_000)
        self.assertEqual(by_rev[2]["recovered_to_donor"], 150_000)
        self.assertEqual(by_rev[2]["new_funds"], 100_000)

        # 全链路轨迹完整
        trail = self.svc.fund_trail(pid)
        self.assertEqual(len(trail["revisions"]), 2)
        m1_trail = trail["revisions"][0]["milestones"][0]
        self.assertEqual([e["status"] for e in m1_trail["evidence"]],
                         ["REJECTED", "APPROVED"])
        self.assertEqual(len(m1_trail["instructions"][0]["receipts"]), 2)  # 幂等去重后
        self.assertIsNotNone(m1_trail["instructions"][0]["writeoff"])


class ClockTests(ServiceTestBase):
    def test_all_timestamps_come_from_injected_clock(self):
        project = self._project()
        pid = project["id"]
        self.assertEqual(project["created_at"], "2026-01-10T08:00:00")
        self.clock.advance(days=2, hours=3)
        mid = self.svc.list_milestones(pid)[0]["id"]
        ev = self.svc.submit_evidence("officer1", mid, content_uri="uri://v1")
        self.assertEqual(ev["submitted_at"], "2026-01-12T11:00:00")
        self.clock.advance(days=1)
        self.svc.review_evidence("reviewer1", ev["id"], approve=True)
        self.clock.advance(hours=5)
        v = self.svc.record_verification("reviewer1", mid, kind="FIELD", conclusion="PASS")
        self.assertEqual(v["verified_at"], "2026-01-13T16:00:00")


if __name__ == "__main__":
    unittest.main()
