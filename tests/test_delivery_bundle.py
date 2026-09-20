"""跨批交付检查：显式选择新结果，不能用旧成功掩盖新失败。夹具不访问网络。"""

import unittest

from helpers import policy
from test_assessment import result_for
from relay_intel.contracts import Batch
from scripts.export_delivery import select_results


class DeliveryBundleTests(unittest.TestCase):
    def batch(self, name, result, synthetic=False):
        return Batch(run_id=name, policy=policy(), synthetic=synthetic, results=[result])

    def test_last_explicit_batch_replaces_previous_and_failure_blocks_delivery(self):
        result = result_for(("exclusion",), domain="unit.test-domain.org")
        first = self.batch("first", result)
        second = self.batch("second", result.model_copy(deep=True))
        selected, rows, replaced = select_results([first, second])
        self.assertEqual(len(rows), 1)
        self.assertEqual(selected[result.candidate.domain][0].run_id, "second")
        self.assertEqual(replaced[0]["previous_run"], "first")
        second.results[0].error = "request failed"
        with self.assertRaisesRegex(ValueError, "request failed"):
            select_results([first, second])

    def test_demo_batch_and_reserved_examples_are_rejected(self):
        for result, synthetic in [(result_for(), False),
                                  (result_for(domain="unit.test-domain.org"), True)]:
            with self.subTest(synthetic=synthetic), self.assertRaisesRegex(ValueError, "synthetic"):
                select_results([self.batch("demo", result, synthetic)])
