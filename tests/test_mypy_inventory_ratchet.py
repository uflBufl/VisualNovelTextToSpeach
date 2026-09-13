import unittest
from collections import Counter

from scripts.check_mypy_inventory import check_counts, error_counts


class MypyInventoryRatchetTest(unittest.TestCase):
    def test_counts_errors_by_production_file(self):
        output = "\n".join(
            (
                "vntts/a.py:1: error: first [no-untyped-def]",
                "vntts/a.py:2:3: error: second [assignment]",
                "vntts/b.py:4: note: context",
            )
        )

        self.assertEqual(error_counts(output), Counter({"vntts/a.py": 2}))

    def test_allows_debt_to_shrink(self):
        baseline = {
            "schema_version": 1,
            "maximum_errors_by_file": {"vntts/a.py": 2, "vntts/b.py": 1},
        }

        self.assertEqual(check_counts(baseline, Counter({"vntts/a.py": 1})), [])

    def test_rejects_growth_and_new_error_files(self):
        baseline = {
            "schema_version": 1,
            "maximum_errors_by_file": {"vntts/a.py": 1},
        }

        self.assertEqual(
            check_counts(baseline, Counter({"vntts/a.py": 2, "vntts/new.py": 1})),
            [
                "mypy debt increased in vntts/a.py: 2 > 1",
                "mypy debt increased in vntts/new.py: 1 > 0",
            ],
        )

    def test_rejects_malformed_baseline(self):
        with self.assertRaisesRegex(ValueError, "malformed"):
            check_counts(
                {"schema_version": 1, "maximum_errors_by_file": {"vntts/a.py": 0}},
                Counter(),
            )


if __name__ == "__main__":
    unittest.main()
