import argparse
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest

from app import add_product, home
from lending import (BASE, assess, connect, disburse, eligibility, generate,
                     one, set_document, submit, timestamp)
from portfolio import build, detect


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.db = connect()
        self.addCleanup(self.db.close)
        generate(self.db, 20, 20, 20261022)
        self.start = BASE + timedelta(days=45)

    def at(self, minute):
        return timestamp(self.start + timedelta(minutes=minute))

    def verify(self, key, kind, minute):
        set_document(self.db, key, kind, "received", self.at(minute))
        set_document(self.db, key, kind, "verified", self.at(minute + 1))

    def test_manual_review_approval_and_disbursement(self):
        key = submit(self.db, "APP-2026-000001", "P01", 100_000, 12, self.at(0))
        self.verify(key, "identity", 1)
        self.assertEqual(assess(self.db, key, self.at(3)), "manual_review")
        self.assertEqual(eligibility(self.db, key, self.at(3)), "Approval required")
        with self.assertRaises(ValueError):
            disburse(self.db, key, self.at(3))
        self.verify(key, "income_proof", 4)
        self.assertEqual(assess(self.db, key, self.at(6)), "approved")
        self.assertEqual(eligibility(self.db, key, self.at(7)), "Eligible")
        amounts = one(self.db, """SELECT count(*),sum(principal),sum(payment),sum(interest)
            FROM repayment_schedule WHERE application_id=?""", key)
        self.assertEqual(tuple(amounts)[:2], (12, 100_000))
        self.assertEqual(amounts[2], amounts[1] + amounts[3])
        disburse(self.db, key, self.at(7))
        with self.assertRaisesRegex(ValueError, "Already disbursed"):
            disburse(self.db, key, self.at(8))
        duplicate = submit(self.db, "APP-2026-000001", "P01", 100_000, 12, self.at(9))
        self.assertEqual(assess(self.db, duplicate, self.at(10)), "rejected")
        self.assertEqual(detect(self.db), [])

    def test_disbursement_rejects_mismatched_evidence(self):
        key = "LOAN-2026-000004"
        self.assertEqual(eligibility(self.db, key, self.at(0)), "Eligible")
        self.db.execute("UPDATE affordability_check SET dti_bps=dti_bps+1 WHERE application_id=?", (key,))
        with self.assertRaisesRegex(ValueError, "recorded evidence"):
            disburse(self.db, key, self.at(1))
        self.assertIsNone(one(self.db, "SELECT 1 FROM disbursement WHERE application_id=?", key))

    def test_invalid_transition_and_term_boundary(self):
        accepted = submit(self.db, "APP-2026-000001", "P01", 100_000, 36, self.at(0))
        with self.assertRaisesRegex(ValueError, "must progress"):
            set_document(self.db, accepted, "identity", "verified", self.at(1))
        self.verify(accepted, "identity", 1)
        self.verify(accepted, "income_proof", 3)
        self.assertEqual(assess(self.db, accepted, self.at(5)), "approved")
        rejected = submit(self.db, "APP-2026-000001", "P01", 100_000, 37, self.at(6))
        self.assertEqual(assess(self.db, rejected, self.at(7)), "rejected")
        self.assertEqual(detect(self.db), [(rejected, "term_outside_product")])


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.db = connect()
        self.addCleanup(self.db.close)
        generate(self.db, 20, 20, 20261022)

    def test_catalog_keeps_precise_rates_and_rejects_duplicate_ids(self):
        product = {"product": "TEST-01", "name": "Test product", "min_principal": "5000",
                   "max_principal": "100000", "rate": "9.85", "min_term": "6", "max_term": "24"}
        add_product(self.db, product, "2026-06-01")
        row = one(self.db, "SELECT * FROM loan_product WHERE id='TEST-01'")
        self.assertEqual((row["annual_bps"], row["min_term"], row["max_term"]), (985, 6, 24))
        with self.assertRaisesRegex(ValueError, "already in use"):
            add_product(self.db, product, "2026-06-01")

    def test_queue_filters_out_unrelated_cases(self):
        page = home(self.db, {"q": ["LOAN-2026-000002"], "status": ["rejected"]})
        self.assertIn("1 matching applications", page)
        self.assertIn('/case/LOAN-2026-000002', page)
        self.assertNotIn('/case/LOAN-2026-000001', page)
        page = home(self.db, {"q": ["LOAN-2026-000002"], "status": ["approved"]})
        self.assertIn("No applications match these filters", page)

    def test_role_permissions_are_explicit(self):
        from app import ROLES
        self.assertEqual(ROLES["customer"][1], {"submit", "document"})
        self.assertIn("assess", ROLES["risk"][1])
        self.assertIn("product", ROLES["admin"][1])
        self.assertNotIn("disburse", ROLES["risk"][1])


class PortfolioTest(unittest.TestCase):
    def test_minimum_portfolio_covers_required_categories(self):
        for applications in (8, 50):
            with self.subTest(applications=applications), tempfile.TemporaryDirectory() as directory:
                args = argparse.Namespace(applicants=8, applications=applications, seed=17)
                report, _ = build(Path(directory) / "portfolio.zip", args)
                self.assertTrue(all(report["baseline_coverage"].values()), report["baseline_coverage"])

    def test_expected_anomalies_and_reproducible_seed(self):
        args = argparse.Namespace(applicants=20, applications=20, seed=20261022)
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "first.zip"
            second = Path(directory) / "second.zip"
            report, labels = build(first, args)
            self.assertEqual((report, labels), build(second, args))
            self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(report["baseline_findings"], 0)
        self.assertEqual(report["injected_cases_detected"], 5)
        self.assertEqual(len([label for label in labels if label.get("issue_type")]), 5)
        self.assertTrue(report["sql_round_trip"])


if __name__ == "__main__":
    unittest.main()
