"""测试辅助：内存应用 + 标准三方角色。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from climate_fund.app import Application  # noqa: E402
from climate_fund.clock import Clock  # noqa: E402

OFFICER = "officer-1"
REVIEWER = "reviewer-1"
REVIEWER2 = "reviewer-2"
FINANCE = "finance-1"


def build_app(db_path: str = ":memory:"):
    app = Application(db_path, Clock())
    s = app.service
    s.create_user(OFFICER, "经办人甲", "officer")
    s.create_user(REVIEWER, "审核人乙", "reviewer")
    s.create_user(REVIEWER2, "审核人丙", "reviewer")
    s.create_user(FINANCE, "财务丁", "finance")
    return app


def seed_ready_milestone(app, *, total="10000", planned="4000", site="A省原址"):
    """建好项目+里程碑，材料受理+现场核验通过，返回 (project, milestone, evidence)。"""
    s = app.service
    project = s.create_project(
        OFFICER, title="抗旱光伏", grantee="某国环境部", site=site, total_amount=total)
    m = s.add_milestone(OFFICER, project["id"], sequence=1,
                        title="一期设备到位", planned_amount=planned)
    ev = s.submit_evidence(OFFICER, m["id"], doc_ref="doc://stage1-v1", note="阶段报告")
    evidence_id = ev["evidence_versions"][0]["id"]
    s.decide_evidence(REVIEWER, evidence_id, accept=True, note="材料齐全")
    s.record_verification(REVIEWER, m["id"], evidence_id=evidence_id, result="pass",
                          site_actual=site, note="现场属实")
    return project, m, evidence_id
