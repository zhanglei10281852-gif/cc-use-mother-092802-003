#!/usr/bin/env python3
"""气候合作资金拨款管理 —— 端到端演示。

场景：云南气候适应农业项目
1. 立项生效，三个里程碑；
2. 里程碑一：材料被驳回 → 补交新版本 → 现场核验与财务凭证先后到达 →
   批准付款 → 分两次部分支付（其中一张回执重复报送，幂等）；
3. 里程碑二：已批准指令付了一部分，突发洪涝 → 项目暂停、资金冻结；
4. 改址恢复：生成新计划版本，明确承接/收回/新增资金责任；
5. 新计划下继续付款、逐笔核销，最终结项对账，账实相符。
"""

import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from climate_fund import Database, GrantService, ManualClock  # noqa: E402


def line(title: str) -> None:
    print(f"\n{'=' * 8} {title} {'=' * 8}")


def main() -> None:
    clock = ManualClock(datetime(2026, 3, 1, 9, 0, 0))
    db = Database(str(Path(tempfile.mkdtemp()) / "demo.db"))
    svc = GrantService(db, clock)

    svc.create_user("wang", "王岚（经办）", "OFFICER")
    svc.create_user("li", "李谨（审核）", "REVIEWER")
    svc.create_user("zhao", "赵实（财务）", "FINANCE")

    line("1. 立项并生效")
    project = svc.create_project(
        "wang", code="CCA-2026-001", name="云南气候适应农业",
        partner="某县合作社", location="A 村", total_amount=1_000_000,
        milestones=[
            {"title": "灌溉设施修复", "amount": 400_000},
            {"title": "耐旱种苗采购", "amount": 350_000},
            {"title": "农户培训", "amount": 250_000},
        ])
    pid = project["id"]
    svc.activate_project("li", pid)
    ms = svc.list_milestones(pid)
    m1, m2, _ = (m["id"] for m in ms)
    print(f"项目 {project['code']} 生效，承诺资金 {project['total_amount']:,} 入池")

    line("2. 里程碑一：驳回 → 补交 → 双通道核验 → 部分支付")
    ev1 = svc.submit_evidence("wang", m1, content_uri="s3://evidence/m1-v1.pdf")
    svc.review_evidence("li", ev1["id"], approve=False, comment="缺少工程量清单")
    clock.advance(days=3)
    ev2 = svc.submit_evidence("wang", m1, content_uri="s3://evidence/m1-v2.pdf")
    svc.review_evidence("li", ev2["id"], approve=True, comment="补齐，通过")
    clock.advance(days=2)   # 现场核验先到
    svc.record_verification("li", m1, kind="FIELD", conclusion="PASS", detail="现场达标")
    clock.advance(days=4)   # 财务凭证后到
    svc.record_verification("li", m1, kind="FINANCIAL", conclusion="PASS", detail="凭证齐全")
    i1 = svc.create_instruction("wang", m1, amount=400_000)
    svc.approve_instruction("li", i1["id"])
    svc.record_receipt("zhao", i1["id"], external_ref="BANK-001", amount=150_000)
    dup, created = svc.record_receipt("zhao", i1["id"], external_ref="BANK-001", amount=150_000)
    print(f"重复报送回执 BANK-001：幂等返回原回执 {dup['id']}（created={created}）")
    svc.record_receipt("zhao", i1["id"], external_ref="BANK-002", amount=250_000)
    print(f"指令一 400,000 分两次付清（150,000 + 250,000）")

    line("3. 里程碑二：部分支付后突发洪涝，项目暂停")
    ev = svc.submit_evidence("wang", m2, content_uri="s3://evidence/m2-v1.pdf")
    svc.review_evidence("li", ev["id"], approve=True)
    svc.record_verification("li", m2, kind="FIELD", conclusion="PASS")
    svc.record_verification("li", m2, kind="FINANCIAL", conclusion="PASS")
    i2 = svc.create_instruction("wang", m2, amount=350_000)
    svc.approve_instruction("li", i2["id"])
    svc.record_receipt("zhao", i2["id"], external_ref="BANK-101", amount=100_000)
    clock.advance(days=6)
    svc.suspend_project("li", pid, reason="洪涝灾害，实施地点受损")
    avail = svc.availability(pid)
    print(f"已付 100,000 后项目暂停：冻结未付 {avail['frozen_unpaid']:,}，"
          f"资金池余额 {avail['pool_balance']:,}")

    line("4. 改址恢复：新旧计划资金责任")
    svc.resume_project(
        "li", pid, new_location="B 村（高地）", reason="DISASTER_RELOCATION",
        new_milestones=[
            {"title": "耐旱种苗采购（新址）", "amount": 250_000},
            {"title": "农户培训（新址）", "amount": 150_000},
        ],
        recover_amount=100_000, new_funds=0)
    rec = svc.reconciliation(pid)
    for r in rec["revisions"]:
        print(f"  计划 v{r['revision_no']} [{r['status']}] @{r['location']}: "
              f"承接 {r['carried_from_previous']:,} / 收回 {r['recovered_to_donor']:,} / "
              f"新增 {r['new_funds']:,} / 该计划下已付 {r['paid_under_revision']:,}")

    line("5. 新计划履约、核销、结项对账")
    ms2 = svc.list_milestones(pid)
    new_milestones = [m for m in ms2 if m["revision_no"] == 2]
    for m in new_milestones:
        ev = svc.submit_evidence("wang", m["id"], content_uri=f"s3://evidence/{m['id']}.pdf")
        svc.review_evidence("li", ev["id"], approve=True)
        svc.record_verification("li", m["id"], kind="FIELD", conclusion="PASS")
        svc.record_verification("li", m["id"], kind="FINANCIAL", conclusion="PASS")
        ins = svc.create_instruction("wang", m["id"], amount=m["amount"])
        svc.approve_instruction("li", ins["id"])
        svc.record_receipt("zhao", ins["id"],
                           external_ref=f"BANK-2{m['seq']:03d}", amount=m["amount"])
        svc.writeoff_instruction("li", ins["id"])
    svc.writeoff_instruction("li", i1["id"])   # 指令一核销
    svc.writeoff_instruction("li", i2["id"])   # 指令二已付 100,000 部分核销
    svc.close_project("li", pid)

    rec = svc.reconciliation(pid)
    print(f"累计拨款 {rec['allocated']:,} = 支付 {rec['paid']:,} "
          f"+ 收回 {rec['recovered']:,} + 池余 {rec['pool_balance']:,}")
    print(f"已付未核销 {rec['outstanding_unreconciled']:,}")
    for name, ok in rec["checks"].items():
        print(f"  [{'OK' if ok else 'XX'}] {name}")
    print(f"账目对平: {rec['balanced']}")

    line("6. 单笔资金轨迹（指令一：批准 → 部分支付 → 核销）")
    trail = svc.instruction_trail(i1["id"])
    for ev_ in trail["timeline"]:
        print(f"  {ev_['created_at']}  {ev_['action']:<9} 由 {ev_['actor']} ({ev_['role']})")


if __name__ == "__main__":
    main()
