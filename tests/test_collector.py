"""Run the collector's pagination, date and aggregation regression cases."""
import unittest
from src.collector import self_test


class CollectorRegressionTests(unittest.TestCase):
    def test_existing_collector_regressions(self):
        self.assertEqual(self_test(), 0)


if __name__ == "__main__":
    unittest.main()
