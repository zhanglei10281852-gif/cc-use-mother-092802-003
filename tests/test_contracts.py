import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from climate_fund.contracts import Milestone, MilestoneState, PaymentReceipt


class FundContractTests(unittest.TestCase):
    def test_milestone_is_tied_to_plan_revision(self):
        value = Milestone("fund-3", 2, MilestoneState.VERIFIED, 4)
        self.assertEqual(value.plan_revision, 4)

    def test_receipt_preserves_decimal_amount(self):
        value = PaymentReceipt("receipt-8", "fund-3", Decimal("125.40"), "USD")
        self.assertEqual(value.amount, Decimal("125.40"))


if __name__ == "__main__":
    unittest.main()
