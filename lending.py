"""Loan workflow and deterministic synthetic portfolio."""
import random
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parent
BASE = datetime(2026, 6, 1, 9)
PRODUCTS = [
    ("P01", "Everyday personal", 20_000, 600_000, 1250, 6, 36),
    ("P02", "Home improvement", 50_000, 1_500_000, 980, 12, 60),
    ("P03", "Education bridge", 25_000, 850_000, 1090, 6, 48),
    ("P04", "Vehicle refinance", 50_000, 1_200_000, 1150, 12, 60),
]


def connect(path=":memory:"):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def insert(db, table, *values):
    return db.execute(f"INSERT INTO {table} VALUES ({','.join('?' for _ in values)})", values).lastrowid


def init(db):
    db.executescript((ROOT / "schema.sql").read_text())
    for product in PRODUCTS:
        insert(db, "loan_product", *product, "2026-01-01")


def one(db, sql, *params):
    return db.execute(sql, params).fetchone()


def timestamp(value):
    return value.isoformat(timespec="microseconds")


def round_money(value):
    return int(Decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def installment_amount(principal, bps, term):
    rate = Decimal(bps) / 120_000
    amount = principal * rate / (1 - (1 + rate) ** -term) if rate else Decimal(principal) / term
    return round_money(amount)


def schedule(app, bps, decided_at):
    start = date.fromisoformat(decided_at[:10])
    remaining = app["principal"]
    term = app["term"]
    regular = installment_amount(remaining, bps, term)
    for number in range(1, term + 1):
        month_index = start.year * 12 + start.month - 1 + number
        due = date(month_index // 12, month_index % 12 + 1, min(start.day, 28))
        interest = round_money(Decimal(remaining) * bps / 120_000)
        principal = remaining if number == term else min(remaining, max(0, regular - interest))
        remaining -= principal
        yield app["id"], number, due.isoformat(), principal, interest, principal + interest, remaining


def submit(db, applicant, product, principal, term, when):
    if principal <= 0 or term <= 0:
        raise ValueError("Enter a positive principal and term.")
    if not one(db, "SELECT 1 FROM applicant WHERE id=? AND created_on<=?", applicant, when[:10]):
        raise ValueError("Select an existing applicant.")
    if not one(db, "SELECT 1 FROM loan_product WHERE id=? AND effective_on<=?", product, when[:10]):
        raise ValueError("Select an active product.")
    prior = one(db, """SELECT id FROM loan_application
        WHERE applicant_id=? AND product_id=? AND principal=? AND term=?
          AND duplicate_of IS NULL AND submitted_at BETWEEN ? AND ?
        ORDER BY submitted_at,id LIMIT 1""", applicant, product, principal, term,
        timestamp(datetime.fromisoformat(when) - timedelta(days=30)), when)
    number = one(db, "SELECT count(*)+1 FROM loan_application")[0]
    key = f"LOAN-2026-{number:06}"
    insert(db, "loan_application", key, applicant, product, principal, term, when, prior[0] if prior else None)
    for index, kind in enumerate(("identity", "income_proof"), 1):
        doc = f"DOC-{key}-{index:03}"
        insert(db, "document", doc, key, kind)
        insert(db, "document_event", None, doc, "missing", when)
    return key


def document_status(db, app_id, when):
    return dict(db.execute("""SELECT kind, (SELECT status FROM document_event e
        WHERE e.document_id=d.id AND recorded_at<=? ORDER BY recorded_at DESC,id DESC LIMIT 1)
        FROM document d WHERE application_id=?""", (when, app_id)))


def set_document(db, app_id, kind, status, when):
    doc = one(db, "SELECT id FROM document WHERE application_id=? AND kind=?", app_id, kind)
    latest = one(db, "SELECT status,recorded_at FROM document_event WHERE document_id=? ORDER BY id DESC LIMIT 1", doc[0])
    if (latest["status"], status) not in {("missing", "received"), ("received", "verified")} or when < latest["recorded_at"]:
        raise ValueError("Document must progress from missing to received to verified.")
    insert(db, "document_event", None, doc[0], status, when)


def inputs_for(db, app, checked_at, docs_at=None):
    aid = app["applicant_id"]
    day = checked_at[:10]
    product = one(db, "SELECT * FROM loan_product WHERE id=?", app["product_id"])
    income = {}
    for source in ("payroll", "declared"):
        income[source] = one(db, """SELECT * FROM income_record WHERE applicant_id=?
            AND source=? AND effective_on<=? ORDER BY effective_on DESC LIMIT 1""", aid, source, day)
    employment = one(db, """SELECT * FROM employment WHERE applicant_id=? AND start_on<=?
        AND (end_on IS NULL OR end_on>?) ORDER BY start_on DESC,id DESC LIMIT 1""", aid, day, day)
    if not employment or any(row is None for row in income.values()):
        raise ValueError("An active employment record and both income sources are required.")
    facilities = list(db.execute("""SELECT * FROM credit_facility WHERE applicant_id=? AND opened_on<=?
        AND (closed_on IS NULL OR closed_on>?) ORDER BY id""", (aid, day, day)))
    payroll = income["payroll"]["monthly"]
    declared = income["declared"]["monthly"]
    debt = sum(row["monthly_due"] for row in facilities)
    late = sum(row["late_payments"] for row in facilities)
    payment = installment_amount(app["principal"], product["annual_bps"], app["term"])
    tenure = (date.fromisoformat(day) - date.fromisoformat(employment["start_on"])).days
    dti = round_money(Decimal(debt) * 10_000 / payroll)
    post = round_money(Decimal(debt + payment) * 10_000 / payroll)
    score = min(850, max(300, 765 - dti // 140 - 24 * late + min(tenure // 90, 12)))
    return {
        "payroll_id": income["payroll"]["id"], "declared_id": income["declared"]["id"],
        "employment_id": employment["id"], "income": payroll, "debt": debt, "payment": payment,
        "dti_bps": dti, "post_loan_bps": post, "residual": payroll - debt - payment, "score": score,
        "income_conflict": int(abs(payroll - declared) * 100 > 15 * payroll),
        "declared_income": declared, "tenure_days": tenure, "late_payments": late,
        "facility_ids": [row["id"] for row in facilities],
        "documents": document_status(db, app["id"], docs_at or checked_at),
        "product": dict(product), "principal": app["principal"], "term": app["term"],
        "duplicate_of": app["duplicate_of"],
    }


def policy(data):
    product = data["product"]
    rules = (
        ("rejected", "PRODUCT_TERM", not product["min_term"] <= data["term"] <= product["max_term"]),
        ("rejected", "PRODUCT_PRINCIPAL", not product["min_principal"] <= data["principal"] <= product["max_principal"]),
        ("rejected", "DUPLICATE", data["duplicate_of"] is not None),
        ("manual_review", "DOCUMENTS", any(data["documents"].get(kind) != "verified" for kind in ("identity", "income_proof"))),
        ("manual_review", "INCOME_CONFLICT", data["income_conflict"]),
        ("rejected", "AFFORDABILITY", data["post_loan_bps"] > 6000 or data["residual"] < 12_000),
        ("rejected", "LOW_SCORE", data["score"] < 635),
        ("manual_review", "EMPLOYMENT_TENURE", data["tenure_days"] < 180),
        ("manual_review", "BORDERLINE", data["post_loan_bps"] >= 4500 or data["score"] < 680),
    )
    for outcome, rule, matches in rules:
        if matches:
            return outcome, rule
    return "approved", "POLICY_PASS"


CHECK_FIELDS = ("payroll_id", "declared_id", "employment_id", "income", "debt", "payment",
                "dti_bps", "post_loan_bps", "residual", "score", "income_conflict")


def assess(db, key, when):
    app = one(db, "SELECT * FROM loan_application WHERE id=?", key)
    previous = latest_decision(db, key)
    if when < app["submitted_at"] or (previous and (previous["outcome"] != "manual_review" or when <= previous["decided_at"])):
        raise ValueError("Only an open application or one in manual review can be evaluated.")
    data = inputs_for(db, app, when)
    check = insert(db, "affordability_check", None, key, when, *(data[field] for field in CHECK_FIELDS))
    db.executemany("INSERT INTO check_facility VALUES (?,?)", ((check, facility) for facility in data["facility_ids"]))
    revision = one(db, "SELECT count(*) FROM affordability_check WHERE application_id=?", key)[0]
    outcome, rule = policy(data)
    decision = f"DEC-{key}-{revision:02}"
    insert(db, "decision", decision, check, when, outcome, rule, json.dumps(data, sort_keys=True))
    if outcome == "approved":
        db.executemany("INSERT INTO repayment_schedule VALUES (?,?,?,?,?,?,?)",
                       schedule(app, data["product"]["annual_bps"], when))
    return outcome


def latest_decision(db, key):
    return one(db, """SELECT d.*,c.application_id,c.checked_at FROM decision d
        JOIN affordability_check c ON c.id=d.check_id WHERE c.application_id=? ORDER BY c.id DESC LIMIT 1""", key)


def approval_evidence(db, app, decision):
    """Return replayed approval inputs when the check and decision agree."""
    try:
        data = inputs_for(db, app, decision["checked_at"])
        snapshot = json.loads(decision["inputs"])
    except (ValueError, ArithmeticError):
        return None
    check = one(db, "SELECT * FROM affordability_check WHERE id=?", decision["check_id"])
    facilities = [row[0] for row in db.execute(
        "SELECT facility_id FROM check_facility WHERE check_id=? ORDER BY facility_id", (check["id"],))]
    if (policy(data) != ("approved", decision["rule"]) or snapshot != data
            or facilities != data["facility_ids"]
            or any(check[field] != data[field] for field in CHECK_FIELDS)):
        return None
    return data


def schedule_matches(app, bps, decided_at, rows):
    """Check payment identities independently, then compare the dated schedule."""
    if len(rows) != app["term"]:
        return False
    balance = app["principal"]
    for _, _, _, principal, interest, payment, remaining in rows:
        if payment != principal + interest or balance - principal != remaining:
            return False
        balance = remaining
    return balance == 0 and rows == list(schedule(app, bps, decided_at))



def eligibility(db, key, when):
    app = one(db, "SELECT * FROM loan_application WHERE id=?", key)
    decision = latest_decision(db, key)
    canonical = app["duplicate_of"] or key
    if one(db, """SELECT 1 FROM disbursement b JOIN loan_application a ON a.id=b.application_id
        WHERE coalesce(a.duplicate_of,a.id)=? AND b.paid_at<=?""", canonical, when):
        return "Already disbursed"
    if app["duplicate_of"]:
        return "Duplicate request"
    if not decision or decision["outcome"] != "approved":
        return "Approval required"
    if not app["submitted_at"] <= decision["checked_at"] <= decision["decided_at"] <= when:
        return "Invalid decision chronology"
    data = approval_evidence(db, app, decision)
    if data is None:
        return "Approval inputs failed policy or do not match the recorded evidence"
    if any(document_status(db, key, when).get(kind) != "verified" for kind in ("identity", "income_proof")):
        return "Verified documents required"
    actual = [tuple(row) for row in db.execute("SELECT * FROM repayment_schedule WHERE application_id=? ORDER BY installment", (key,))]
    if not schedule_matches(app, data["product"]["annual_bps"], decision["decided_at"], actual):
        return "Schedule does not reconcile"
    return "Eligible"


def disburse(db, key, when):
    result = eligibility(db, key, when)
    if result != "Eligible":
        raise ValueError(result)
    app = one(db, "SELECT principal FROM loan_application WHERE id=?", key)
    insert(db, "disbursement", None, key, latest_decision(db, key)["id"], when, app[0])


def generate(db, applicants, applications, seed):
    rng = random.Random(seed)
    init(db)
    for i in range(1, applicants + 1):
        key = f"APP-2026-{i:06}"
        insert(db, "applicant", key, f"Synthetic applicant {i:06}", "2025-01-01")
        tenure = rng.choice([rng.randint(35, 170), rng.randint(210, 2500), rng.randint(400, 3000)])
        if i <= 8:
            tenure = 90 if i == 7 else 1096
        start = BASE.date() - timedelta(days=tenure)
        kind = rng.choice(["salaried", "salaried", "contract"])
        insert(db, "employment", f"EMP-{i:06}-01", key, (start - timedelta(days=410)).isoformat(),
               (start - timedelta(days=14)).isoformat(), "contract")
        insert(db, "employment", f"EMP-{i:06}-02", key, start.isoformat(), None, kind)
        pay = max(18_000, min(280_000, round(rng.lognormvariate(11.0, .52), -2)))
        if i <= 8:
            pay = 125_000
        for revision, day, factor in [(1, "2025-12-01", .91), (2, "2026-05-01", 1)]:
            for source in ("payroll", "declared"):
                amount = round(pay * factor * (rng.uniform(.97, 1.04) if source == "declared" else 1))
                insert(db, "income_record", f"INC-{i:06}-{revision}-{source}", key, source, day, amount)
        if i == 7:
            insert(db, "credit_facility", "CR-000007-01", key, "2025-09-01", None, 560_000, 70_000, 0)
        if i == 8:
            insert(db, "credit_facility", "CR-000008-01", key, "2025-09-01", None, 376_000, 47_000, 3)
        if i > 8:
            for j in range(rng.choices([0, 1, 2, 3], [24, 42, 26, 8])[0]):
                due = round(pay * rng.uniform(.03, .34))
                insert(db, "credit_facility", f"CR-{i:06}-{j+1:02}", key, "2025-09-01", None,
                       due * rng.randint(4, 28), due, rng.choices([0, 1, 2, 3], [70, 20, 8, 2])[0])
    # Applicant 6 is reserved so the income-conflict fixture cannot change
    # the evidence for any other application, even at small applicant counts.
    history = []
    for index in range(applications):
        when = BASE + timedelta(minutes=index * 5)
        if index == 1 or (index > 7 and index % 41 == 0):
            aid, product, principal, term = history[0 if index == 1 else index - 1]
        else:
            if index < 8:
                applicant_number = index + 1
            else:
                applicant_number = index % (applicants - 1) + 1
                if applicant_number >= 6:
                    applicant_number += 1
            aid = f"APP-2026-{applicant_number:06}"
            product = PRODUCTS[(index if index == 0 else index - 1) % 4]
            _, _, low, high, _, min_term, max_term = product
            principal = 100_000 if index < 8 else min(high, low + 1000 * int((high-low) / 1000 * rng.betavariate(1.2, 4.5)))
            term = min_term if index < 8 else rng.randint(min_term, max_term)
        history.append((aid, product, principal, term))
        key = submit(db, aid, product[0], principal, term, timestamp(when))
        for kind in ("identity", "income_proof"):
            if kind == "income_proof" and (index == 6 or (index >= 8 and rng.random() < .10)):
                continue
            set_document(db, key, kind, "received", timestamp(when + timedelta(seconds=30)))
            set_document(db, key, kind, "verified", timestamp(when + timedelta(minutes=1)))
        result = assess(db, key, timestamp(when + timedelta(minutes=2)))
        if result == "approved" and (index == 0 or (index > 7 and rng.random() < .32)):
            disburse(db, key, timestamp(when + timedelta(minutes=3)))
    db.commit()
