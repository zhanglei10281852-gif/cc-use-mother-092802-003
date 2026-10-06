"""证据版本规则：驳回版本不可覆盖，补交产生新版本，定论不可改。"""

import unittest

from climate_fund.errors import ConflictError, PermissionError
from helpers import OFFICER, REVIEWER, build_app


class EvidenceVersionTests(unittest.TestCase):
    def setUp(self):
        self.app = build_app()
        s = self.app.service
        self.p = s.create_project(OFFICER, title="沼气项目", grantee="某县",
                                  site="D村", total_amount="2000")
        self.m = s.add_milestone(OFFICER, self.p["id"], sequence=1,
                                 title="地基", planned_amount="1000")

    def test_rejected_version_is_never_overwritten_by_resubmission(self):
        s = self.app.service
        v1 = s.submit_evidence(OFFICER, self.m["id"], doc_ref="doc://v1", note="初版")
        id1 = v1["evidence_versions"][0]["id"]

        s.decide_evidence(REVIEWER, id1, accept=False, note="缺少现场照片")

        # 补交 -> 新版本 v2，v1 原样保留为 rejected
        v2 = s.submit_evidence(OFFICER, self.m["id"], doc_ref="doc://v2", note="补照片")
        versions = v2["evidence_versions"]
        self.assertEqual([v["version"] for v in versions], [1, 2])
        self.assertEqual(versions[0]["status"], "rejected")
        self.assertEqual(versions[0]["id"], id1)
        self.assertEqual(versions[1]["status"], "submitted")
        self.assertEqual(versions[1]["doc_ref"], "doc://v2")

        # 不能对被驳回的 v1 再次决策
        with self.assertRaises(ConflictError):
            s.decide_evidence(REVIEWER, id1, accept=True)

        # v2 受理，里程碑进入可申请状态
        s.decide_evidence(REVIEWER, versions[1]["id"], accept=True)
        got = s.get_milestone(self.m["id"])
        self.assertEqual(got["evidence_versions"][0]["status"], "rejected")  # 历史不变

    def test_undecided_previous_version_marked_superseded(self):
        s = self.app.service
        v1 = s.submit_evidence(OFFICER, self.m["id"], doc_ref="doc://a")
        id1 = v1["evidence_versions"][0]["id"]
        # 审核人还没看，经办人又交了一版
        v2 = s.submit_evidence(OFFICER, self.m["id"], doc_ref="doc://b")
        self.assertEqual(v2["evidence_versions"][0]["status"], "superseded")
        self.assertEqual(v2["evidence_versions"][1]["status"], "submitted")
        # 被取代的版本不能再受理
        with self.assertRaises(ConflictError):
            s.decide_evidence(REVIEWER, id1, accept=True)

    def test_submitter_cannot_review_own_evidence(self):
        s = self.app.service
        # 经办人默认不是审核人，直接 403
        v1 = s.submit_evidence(OFFICER, self.m["id"], doc_ref="doc://a")
        id1 = v1["evidence_versions"][0]["id"]
        with self.assertRaises(PermissionError):
            s.decide_evidence(OFFICER, id1, accept=True)


if __name__ == "__main__":
    unittest.main()
