"""Build, validate and package a reproducible portfolio."""
import argparse
from collections import Counter
import csv
from datetime import date, datetime, timedelta
import hashlib
import io
import json
from pathlib import Path
import tempfile
import zipfile

from lending import (CHECK_FIELDS, ROOT, assess, connect, eligibility, generate,
                     document_status, inputs_for, insert, latest_decision, one, policy,
                     schedule_matches, timestamp)

AS_OF = "2026-07-01T00:00:00.000000"
SEED = 20261022
EXPORT = ROOT / "deliverables"


def detect(db):
    findings = set()

    def flag(key, issue):
        findings.add((str(key), issue))

    for row in db.execute("PRAGMA foreign_key_check"):
        flag(f"{row[0]}:{row[1]}", "foreign_key")
    for row in db.execute("PRAGMA integrity_check"):
        if row[0] != "ok":
            flag("database", row[0])
    for name in tables(db):
        columns = [row["name"] for row in db.execute(f"PRAGMA table_info({name})")]
        date_fields = [field for field in columns if field.endswith(("_on", "_at"))]
        if date_fields:
            for row in db.execute(f"SELECT * FROM {name}"):
                for field in date_fields:
                    if row[field] is None:
                        continue
                    try:
                        parser = datetime.fromisoformat if field.endswith("_at") else date.fromisoformat
                        parser(row[field])
                    except ValueError:
                        flag(row[0], f"invalid_date_{field}")
    for doc in db.execute("""SELECT d.id,d.application_id,a.submitted_at FROM document d
        JOIN loan_application a ON a.id=d.application_id"""):
        previous = doc["submitted_at"]
        events = list(db.execute("SELECT * FROM document_event WHERE document_id=? ORDER BY id", (doc["id"],)))
        states = [event["status"] for event in events]
        if not states or states != ["missing", "received", "verified"][:len(states)]:
            flag(doc["application_id"], "document_transition")
        for event in events:
            if event["recorded_at"] < previous:
                flag(doc["application_id"], "document_chronology")
            previous = event["recorded_at"]
    for app in db.execute("SELECT * FROM loan_application ORDER BY id"):
        key = app["id"]
        product = one(db, "SELECT * FROM loan_product WHERE id=?", app["product_id"])
        applicant = one(db, "SELECT * FROM applicant WHERE id=?", app["applicant_id"])
        if product is None or applicant is None:
            continue
        try:
            cutoff = timestamp(datetime.fromisoformat(app["submitted_at"]) - timedelta(days=30))
        except ValueError:
            continue
        if app["submitted_at"][:10] < max(product["effective_on"], applicant["created_on"]):
            flag(key, "application_chronology")
        if not product["min_term"] <= app["term"] <= product["max_term"]:
            flag(key, "term_outside_product")
        prior = one(db, """SELECT id FROM loan_application WHERE applicant_id=? AND product_id=?
            AND principal=? AND term=? AND id<>? AND duplicate_of IS NULL
            AND submitted_at BETWEEN ? AND ?
            AND (submitted_at<? OR (submitted_at=? AND id<?))
            ORDER BY submitted_at,id LIMIT 1""", app["applicant_id"], app["product_id"],
            app["principal"], app["term"], key,
            cutoff,
            *([app["submitted_at"]] * 3), key)
        if app["duplicate_of"] != (prior[0] if prior else None):
            flag(key, "duplicate_reference")
        previous = None
        checks = db.execute("""SELECT c.*,d.id AS decision_id,d.decided_at,d.outcome,d.rule,d.inputs
            FROM affordability_check c LEFT JOIN decision d ON d.check_id=c.id
            WHERE c.application_id=? ORDER BY c.id""", (key,))
        for check in checks:
            if not check["decision_id"]:
                flag(key, "missing_decision")
                continue
            if not app["submitted_at"] <= check["checked_at"] <= check["decided_at"]:
                flag(key, "decision_before_checks")
            if previous and (previous["outcome"] != "manual_review" or check["checked_at"] <= previous["decided_at"]):
                flag(key, "decision_transition")
            previous = check
            try:
                data = inputs_for(db, app, check["checked_at"])
            except (ValueError, ArithmeticError):
                flag(key, "invalid_check_sources")
                continue
            if data["income_conflict"]:
                flag(key, "income_conflict")
            facility_ids = [row[0] for row in db.execute(
                "SELECT facility_id FROM check_facility WHERE check_id=? ORDER BY facility_id", (check["id"],))]
            if facility_ids != data["facility_ids"]:
                flag(key, "credit_evidence_mismatch")
            if any(check[field] != data[field] for field in CHECK_FIELDS) or json.loads(check["inputs"]) != data:
                flag(key, "input_replay_mismatch")
            if (check["outcome"], check["rule"]) != policy(data):
                flag(key, "decision_replay_mismatch")
        rows = [tuple(row) for row in db.execute("SELECT * FROM repayment_schedule WHERE application_id=? ORDER BY installment", (key,))]
        approved = previous and previous["outcome"] == "approved"
        reconciled = False
        if approved:
            try:
                reconciled = schedule_matches(app, product["annual_bps"], previous["decided_at"], rows)
            except (ValueError, ArithmeticError):
                pass
            if not reconciled:
                flag(key, "schedule_reconciliation")
        elif rows:
            flag(key, "schedule_without_approval")
        paid = one(db, "SELECT * FROM disbursement WHERE application_id=?", key)
        if paid:
            if (not approved or paid["decision_id"] != previous["decision_id"] or
                    not previous["checked_at"] <= previous["decided_at"] <= paid["paid_at"] or
                    paid["amount"] != app["principal"] or not reconciled or
                    any(document_status(db, key, paid["paid_at"]).get(kind) != "verified"
                        for kind in ("identity", "income_proof"))):
                flag(key, "disbursement_prerequisites")
            if app["duplicate_of"]:
                original = one(db, "SELECT 1 FROM disbursement WHERE application_id=?", app["duplicate_of"])
                flag(key, "duplicate_disbursed_twice" if original else "duplicate_disbursement")
    return sorted(findings)


def inject(db):
    """Modify five isolated fixtures after validating the clean portfolio."""
    truth = []
    fixtures = [
        (3, "term_outside_product", "rejected", "PRODUCT_TERM"),
        (4, "schedule_reconciliation", "approved", "POLICY_PASS"),
        (5, "decision_before_checks", "approved", "POLICY_PASS"),
        (6, "income_conflict", "manual_review", "INCOME_CONFLICT"),
        (2, "duplicate_disbursed_twice", "rejected", "DUPLICATE"),
    ]
    for number, issue, outcome, rule in fixtures:
        key = f"LOAN-2026-{number:06}"
        decision = latest_decision(db, key)
        if issue in ("term_outside_product", "income_conflict"):
            db.execute("DELETE FROM repayment_schedule WHERE application_id=?", (key,))
            db.execute("DELETE FROM decision WHERE id=?", (decision["id"],))
            db.execute("DELETE FROM check_facility WHERE check_id=?", (decision["check_id"],))
            db.execute("DELETE FROM affordability_check WHERE id=?", (decision["check_id"],))
            if issue == "term_outside_product":
                db.execute("""UPDATE loan_application SET term=(SELECT max_term+1 FROM loan_product
                    WHERE id=product_id) WHERE id=?""", (key,))
            else:
                db.execute("""UPDATE income_record SET monthly=monthly*2
                    WHERE id=json_extract(?, '$.declared_id')""", (decision["inputs"],))
            assess(db, key, decision["checked_at"])
        elif issue == "schedule_reconciliation":
            cursor = db.execute("""UPDATE repayment_schedule SET payment=payment+1
                WHERE application_id=? AND installment=1""", (key,))
            assert cursor.rowcount == 1
        elif issue == "decision_before_checks":
            db.execute("UPDATE decision SET decided_at=? WHERE id=?", ("2026-06-01T09:20:30.000000", decision["id"]))
        else:
            app = one(db, "SELECT * FROM loan_application WHERE id=?", key)
            insert(db, "disbursement", None, key, decision["id"], "2026-06-01T09:09:00.000000", app["principal"])
        actual = latest_decision(db, key)
        if (actual["outcome"], actual["rule"]) != (outcome, rule):
            raise RuntimeError(f"Fixture {issue} failed its decision oracle")
        truth.append({"application_id": key, "issue_type": issue,
                      "expected_decision": outcome, "expected_rule": rule})
    return truth


def tables(db):
    return [row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table' ORDER BY rowid")]


def write_archive(db, path):
    checksums = {}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        def write(name, content):
            payload = content.encode()
            checksums[name] = hashlib.sha256(payload).hexdigest()
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)

        for table in tables(db):
            cursor = db.execute(f"SELECT * FROM {table} ORDER BY rowid")
            records = [dict(row) for row in cursor]
            csv_data = io.StringIO(newline="")
            writer = csv.DictWriter(csv_data, [item[0] for item in cursor.description])
            writer.writeheader()
            writer.writerows(records)
            write(f"csv/{table}.csv", csv_data.getvalue())
            write(f"json/{table}.json", "[\n" + ",\n".join(json.dumps(record) for record in records) + "\n]\n")

        statements = list(db.iterdump())
        schema = [line for line in statements if line.startswith("CREATE TABLE")]
        inserts = {table: [] for table in tables(db)}
        indexes = [line for line in statements if line.startswith("CREATE INDEX")]
        for line in statements:
            if line.startswith('INSERT INTO "'):
                inserts[line.split('"', 2)[1]].append(line)
        data = [line for group in inserts.values() for line in group]
        write("seed.sql", "\n".join(["PRAGMA foreign_keys=ON;", "BEGIN;"]
                                   + schema + indexes + data + ["COMMIT;", ""]))
    return checksums


def build(path, args):
    db = connect()
    generate(db, args.applicants, args.applications, args.seed)
    clean = detect(db)
    if clean:
        raise RuntimeError(f"Unexpected clean portfolio findings: {clean[:12]}")
    decision_labels = []
    for app in db.execute("SELECT * FROM loan_application ORDER BY id"):
        decision = latest_decision(db, app["id"])
        outcome, rule = policy(inputs_for(db, app, decision["checked_at"]))
        decision_labels.append({"application_id": app["id"], "expected_decision": outcome,
                                "expected_rule": rule})
    coverage = dict(one(db, """SELECT
        sum(json_extract(d.inputs,'$.documents.income_proof')='missing') AS missing_income_proof,
        sum(c.dti_bps>=4500) AS high_existing_debt,
        sum(json_extract(d.inputs,'$.tenure_days')<180) AS short_employment_tenure,
        sum(a.duplicate_of IS NOT NULL) AS duplicate_requests,
        sum(c.score>=635 AND c.score<680) AS borderline_scores,
        sum(a.term=p.min_term OR a.term=p.max_term) AS inclusive_term_boundary,
        sum(d.outcome='approved') AS approved,
        sum(d.outcome='rejected') AS rejected,
        sum(d.outcome='manual_review') AS manual_review
        FROM loan_application a JOIN loan_product p ON p.id=a.product_id
        JOIN affordability_check c ON c.application_id=a.id JOIN decision d ON d.check_id=c.id"""))
    if not all(coverage.values()):
        raise RuntimeError(f"Portfolio is missing required baseline coverage: {coverage}")
    truth = inject(db)
    findings = detect(db)
    expected = {(row["application_id"], row["issue_type"]) for row in truth}
    expected.add(("LOAN-2026-000002", "disbursement_prerequisites"))
    observed = set(findings)
    if observed != expected:
        raise RuntimeError(f"Unexpected {sorted(observed - expected)} / missing {sorted(expected - observed)}")
    labels = {row["application_id"]: row for row in decision_labels}
    for row in truth:
        labels[row["application_id"]].update(row)
    counts = {name: one(db, f"SELECT count(*) FROM {name}")[0] for name in tables(db)}
    outcomes = [dict(row) for row in db.execute("""SELECT a.product_id,d.outcome,count(*) AS applications
        FROM decision d JOIN affordability_check c ON c.id=d.check_id
        JOIN loan_application a ON a.id=c.application_id GROUP BY a.product_id,d.outcome ORDER BY a.product_id,d.outcome""")]
    reasons = Counter(row[0] for row in db.execute("SELECT rule FROM decision"))
    eligible = sum(eligibility(db, row[0], AS_OF) == "Eligible"
                   for row in db.execute("SELECT id FROM loan_application"))
    checksums = write_archive(db, path)
    with zipfile.ZipFile(path) as archive, connect() as restored:
        restored.executescript(archive.read("seed.sql").decode())
        if detect(restored) != findings or any(one(restored, f"SELECT count(*) FROM {name}")[0] != count
                                               for name, count in counts.items()):
            raise RuntimeError("SQL seed failed round-trip verification")
    db.close()
    return {
        "seed": args.seed, "counts": counts, "outcomes_by_product": outcomes, "rule_counts": dict(reasons),
        "baseline_findings": len(clean), "baseline_coverage": coverage, "eligible_as_of": AS_OF, "eligible_applications": eligible,
        "injected_cases": len(truth), "injected_cases_detected": len(truth),
        "findings": [{"application_id": key, "issue_type": issue} for key, issue in findings],
        "unexpected_findings": 0, "sql_round_trip": True, "sha256": checksums,
    }, decision_labels


def verify(directory):
    """Verify a published export from its files, including cross-format parity."""
    report = json.loads((directory / "validation.json").read_text())
    labels = json.loads((directory / "ground_truth.json").read_text())
    path = directory / "portfolio.zip"
    with path.open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != report["reproducibility"]["archive_sha256"]:
            raise ValueError("Portfolio archive checksum mismatch")
    db = connect()
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or set(names) != set(report["sha256"]):
                raise ValueError("Portfolio archive entries do not match the manifest")
            for name, checksum in report["sha256"].items():
                if hashlib.sha256(archive.read(name)).hexdigest() != checksum:
                    raise ValueError(f"Checksum mismatch for {name}")
            db.executescript(archive.read("seed.sql").decode())
            counts = {}
            for table in tables(db):
                cursor = db.execute(f"SELECT * FROM {table} ORDER BY rowid")
                rows = [dict(row) for row in cursor]
                counts[table] = len(rows)
                if rows != json.loads(archive.read(f"json/{table}.json")):
                    raise ValueError(f"JSON and SQL records differ for {table}")
                source = io.StringIO(archive.read(f"csv/{table}.csv").decode(), newline="")
                reader = csv.DictReader(source)
                csv_rows = [{key: "" if value is None else str(value) for key, value in row.items()}
                            for row in rows]
                if reader.fieldnames != [column[0] for column in cursor.description] or list(reader) != csv_rows:
                    raise ValueError(f"CSV and SQL records differ for {table}")
        if counts != report["counts"]:
            raise ValueError("Reported row counts differ from the exported data")
        observed = detect(db)
        expected = [(row["application_id"], row["issue_type"]) for row in report["findings"]]
        if observed != expected:
            raise ValueError("Restored validation findings differ from the report")
        decisions = {
            row["application_id"]: (row["outcome"], row["rule"])
            for row in db.execute("""SELECT c.application_id,d.outcome,d.rule
                FROM decision d JOIN affordability_check c ON c.id=d.check_id""")
        }
        truth = {row["application_id"]: (row["expected_decision"], row["expected_rule"]) for row in labels}
        anomaly_labels = {(row["application_id"], row["issue_type"]) for row in labels if "issue_type" in row}
        if (len(labels) != len(truth) or truth != decisions or len(anomaly_labels) != report["injected_cases"]
                or not anomaly_labels.issubset(observed)):
            raise ValueError("Ground truth does not match restored decisions and detected cases")
    finally:
        db.close()
    print(f"Verified SQL, CSV, JSON and ground truth for {counts['loan_application']:,} applications.")



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--applicants", type=int, default=10_000)
    parser.add_argument("--applications", type=int, default=6_000)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--output", type=Path, default=EXPORT)
    parser.add_argument("--verify", action="store_true", help="Check existing exports in the output directory.")
    args = parser.parse_args()
    if args.verify:
        verify(args.output)
        return
    if not 8 <= args.applicants <= 999_999 or not 8 <= args.applications <= 8_000:
        parser.error("Use 8–999999 applicants and 8–8000 applications.")
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.output) as directory:
        first = Path(directory) / "portfolio.zip"
        second = Path(directory) / "repeat.zip"
        report, labels = build(first, args)
        repeat, repeat_labels = build(second, args)
        checksum = hashlib.file_digest(first.open("rb"), "sha256").hexdigest()
        repeated_checksum = hashlib.file_digest(second.open("rb"), "sha256").hexdigest()
        if report != repeat or labels != repeat_labels or checksum != repeated_checksum:
            raise RuntimeError("Repeat generation produced different data")
        first.replace(args.output / "portfolio.zip")
    report["reproducibility"] = {"runs": 2, "row_counts_match": True, "checksums_match": True,
                                 "archive_sha256": checksum}
    (args.output / "ground_truth.json").write_text(json.dumps(labels, indent=2) + "\n")
    (args.output / "validation.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"Verified {args.applicants:,} applicants, {args.applications:,} applications and {len(report['findings'])} expected findings.")


if __name__ == "__main__":
    main()
