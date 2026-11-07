"""Local browser workbench. Start with python3 app.py."""
import argparse
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from decimal import Decimal, InvalidOperation
import json
import re
import secrets
from urllib.parse import parse_qs, urlencode, urlsplit
import zipfile

from lending import (ROOT, assess, connect, disburse, eligibility, insert, latest_decision,
                     one, set_document, submit, timestamp)

DATA = ROOT / "deliverables"
TOKEN = secrets.token_urlsafe(32)
ROLES = {
    "customer": ("Customer", {"submit", "document"}),
    "operations": ("Operations analyst", {"submit", "document", "assess", "disburse"}),
    "risk": ("Risk / compliance", {"assess"}),
    "admin": ("System administrator", {"product", "audit"}),
}
DOWNLOADS = {f"/exports/{filename}": filename for filename in
             ("portfolio.zip", "validation.json", "ground_truth.json")}


def esc(value):
    return escape(str(value if value is not None else "—"))


def table(headers, rows):
    head = "".join(f"<th scope='col'>{esc(col)}</th>" for col in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def records(rows):
    rows = list(rows)
    return table(rows[0].keys(), ([esc(value) for value in row] for row in rows)) if rows else "<p>No records yet.</p>"


def form(fields, action, label, key=""):
    return f"""<form class="entry-form" method="post" action="/action">
      <input type="hidden" name="token" value="{TOKEN}">
      <input type="hidden" name="action" value="{action}">
      <input type="hidden" name="key" value="{esc(key)}">
      {fields}<button>{label}</button></form>"""


DECISIONS = {
    "submitted": "Submitted", "manual_review": "Manual review",
    "approved": "Approved", "rejected": "Rejected",
}
RULES = {
    "PRODUCT_TERM": "Requested term is outside the product range.",
    "PRODUCT_PRINCIPAL": "Requested principal is outside the product limits.",
    "DUPLICATE": "This request matches an earlier application.",
    "DOCUMENTS": "One or more required documents were unverified.",
    "INCOME_CONFLICT": "Declared income differed from payroll by more than 15%.",
    "AFFORDABILITY": "Post-loan debt exceeded 60% of income or residual income was below ₹12,000.",
    "LOW_SCORE": "The affordability score was below 635.",
    "EMPLOYMENT_TENURE": "Current employment tenure was under 180 days.",
    "BORDERLINE": "The score or post-loan debt ratio fell in the review range.",
    "POLICY_PASS": "The application met the approval rules at the time of assessment.",
}


def badge(value, label=None):
    return f'<span class="state {esc(value)}">{esc(label or DECISIONS.get(value, value.title()))}</span>'


def money(value):
    return f"₹{value:,}"


def option(value, label, selected):
    marker = " selected" if value == selected else ""
    return f'<option value="{esc(value)}"{marker}>{esc(label)}</option>'


def products(db):
    return list(db.execute("SELECT * FROM loan_product ORDER BY id"))


def heading(title, description, action=""):
    return f'<div class="page-heading"><div><h1>{esc(title)}</h1><p>{esc(description)}</p></div>{action}</div>'


def evidence(fields):
    return '<dl class="evidence">' + "".join(
        f"<div><dt>{esc(label)}</dt><dd>{esc(value)}</dd></div>" for label, value in fields
    ) + '</dl>'


def new_application(db):
    options = "".join(option(row["id"], row["name"], "") for row in products(db))
    fields = f"""<label>Applicant ID<input name="applicant" value="APP-2026-000001" required></label>
      <label>Product<select name="product">{options}</select></label>
      <label>Principal (INR)<input name="principal" type="number" min="1" max="100000000" required></label>
      <label>Term (months)<input name="term" type="number" min="1" max="1200" required></label>"""
    return (heading("New application", "Choose an existing synthetic applicant and enter the requested loan terms.")
            + '<section class="panel"><h2>Loan request</h2><p>You can verify documents and run an assessment after submission.</p>'
            + form(fields, "submit", "Create application") + '</section>')


def home(db, params):
    query = params.get("q", [""])[0][:80].strip().upper()
    product = params.get("product", [""])[0]
    status = params.get("status", [""])[0]
    order = params.get("sort", ["recent"])[0]
    ordering = {"recent": "submitted_at DESC,id DESC", "oldest": "submitted_at,id",
                "highest": "principal DESC,id DESC"}
    catalog = products(db)
    if product not in {row["id"] for row in catalog}:
        product = ""
    if status not in DECISIONS and status not in {"disbursed", "eligible"}:
        status = ""
    if order not in ordering:
        order = "recent"
    try:
        page = max(1, int(params.get("page", ["1"])[0]))
    except ValueError:
        page = 1
    base = """WITH queue AS (
      SELECT a.*,p.name AS product, customer.display_name,
        coalesce((SELECT d.outcome FROM decision d JOIN affordability_check c ON c.id=d.check_id
          WHERE c.application_id=a.id ORDER BY c.id DESC LIMIT 1),'submitted') AS status,
        EXISTS(SELECT 1 FROM disbursement WHERE application_id=a.id) AS paid
      FROM loan_application a JOIN loan_product p ON p.id=a.product_id
      JOIN applicant customer ON customer.id=a.applicant_id
    )"""
    totals = dict(db.execute(base + " SELECT status,count(*) FROM queue GROUP BY status"))
    disbursed = one(db, "SELECT count(*) FROM disbursement")[0]
    metrics = "".join(
        f'<a class="metric" href="/?{urlencode(dict(status=key))}"><span>{label}</span><strong>{value:,}</strong></a>'
        for key, label, value in [("", "Total applications", sum(totals.values())),
            ("submitted", "Awaiting assessment", totals.get("submitted", 0)),
            ("manual_review", "In manual review", totals.get("manual_review", 0)),
            ("approved", "Approved", totals.get("approved", 0)),
            ("disbursed", "Disbursed", disbursed)]
    )
    where = """ FROM queue WHERE (instr(id,?)>0 OR instr(applicant_id,?)>0 OR instr(upper(display_name),?)>0)
                AND (?='' OR product_id=?) AND (?='' OR status=? OR (?='disbursed' AND paid))"""
    match_state = "approved" if status == "eligible" else status
    args = (query, query, query, product, product, match_state, match_state, match_state)
    size = 15
    query_rows = base + " SELECT *" + where + f" ORDER BY {ordering[order]}"
    if status == "eligible":
        now = timestamp(datetime.now())
        eligible_rows = [row for row in db.execute(query_rows, args)
                         if not row["paid"] and eligibility(db, row["id"], now) == "Eligible"]
        count = len(eligible_rows)
    else:
        count = one(db, base + " SELECT count(*)" + where, *args)[0]
    pages = max(1, (count + size - 1) // size)
    page = min(page, pages)
    start = (page - 1) * size
    rows = (eligible_rows[start:start + size] if status == "eligible" else
            db.execute(query_rows + " LIMIT ? OFFSET ?", (*args, size, start)))
    queue = table(["Application / applicant", "Loan product", "Requested amount", "Term", "Decision", "Submitted"],
        ([f'<a href="/case/{esc(row["id"])}">{esc(row["id"])}</a><small>{esc(row["applicant_id"])}</small>',
          esc(row["product"]), money(row["principal"]), f'{row["term"]} mo', badge(row["status"]) +
          (" " + badge("disbursed") if row["paid"] else " " + badge("eligible") if status == "eligible" else ""),
          esc(row["submitted_at"][:10])] for row in rows)
        ) if count else '<p class="empty">No applications match these filters. Try another ID or clear the filters.</p>'
    pagination_state = dict(q=query, product=product, status=status, sort=order)
    links = " ".join(
        f'<a class="button secondary" href="/?{esc(urlencode({**pagination_state, "page": index}))}">{label}</a>'
        for index, label in [(1, "First"), (page - 1, "Previous"), (page + 1, "Next"), (pages, "Last")]
        if index != page and 1 <= index <= pages
    )
    pagination_html = f"""<nav class="pagination" aria-label="Pagination">
      <span>{start + 1 if count else 0:,}–{min(start + size,count):,} of {count:,} · Page {page:,} of {pages:,}</span>
      <div class="pagination-actions">{links}</div></nav>"""
    product_options = option("", "All products", product) + "".join(option(row["id"], row["name"], product) for row in catalog)
    states = {"": "All decisions", **DECISIONS, "eligible": "Eligible for disbursement", "disbursed": "Disbursed"}
    status_options = "".join(option(key, label, status) for key, label in states.items())
    sort_options = "".join(option(key, label, order) for key, label in
                           [("recent", "Newest first"), ("oldest", "Oldest first"), ("highest", "Highest amount")])
    return f"""<div class="hero page-heading"><div><h1>Application queue</h1>
      <p>Track requests, clear review exceptions, and check which loans can be funded.</p></div>
      <a href="/new" class="button">+ New application</a></div>
      <div class="metrics">{metrics}</div>
      <section class="panel queue"><div class="panel-title"><h2>Loan applications</h2>
      <p>{count:,} matching applications</p></div>
      <form method="get" class="toolbar">
        <label class="search">Search applications<input name="q" value="{esc(query)}" placeholder="Application ID or applicant"></label>
        <label>Decision<select name="status">{status_options}</select></label>
        <label>Product<select name="product">{product_options}</select></label>
        <label>Sort<select name="sort">{sort_options}</select></label>
        <button>Apply filters</button><a href="/" class="button secondary">Clear</a>
      </form>{pagination_html}{queue}{pagination_html}</section>"""


def details(title, content):
    return f"<details><summary>{esc(title)}</summary>{content}</details>"


def readable_time(value):
    return datetime.fromisoformat(value).strftime("%d %b %Y · %H:%M:%S")


def decision_evidence(decision):
    data = json.loads(decision["inputs"])
    amounts = evidence([
        ("Payroll / month", money(data["income"])), ("Declared income / month", money(data["declared_income"])),
        ("Existing debt / month", money(data["debt"])), ("Estimated installment", money(data["payment"])),
        ("Existing debt-to-income", f'{data["dti_bps"]/100:.2f}%'),
        ("Debt-to-income with loan", f'{data["post_loan_bps"]/100:.2f}%'),
        ("Residual / month", money(data["residual"])), ("Synthetic score", data["score"]),
        ("Job tenure", f'{data["tenure_days"]} days'), ("Late payments", data["late_payments"]),
    ])
    documents = " · ".join(f'{name.replace("_", " ").title()} {status or "unknown"}'
                           for name, status in data["documents"].items())
    return (f'<p>{esc(RULES.get(decision["rule"], decision["rule"]))}</p>{amounts}'
            + details("Decision record and source evidence",
                      evidence([("Rule", decision["rule"]), ("Assessed", readable_time(decision["checked_at"])),
                                ("Decided", readable_time(decision["decided_at"])), ("Decision ID", decision["id"]),
                                ("Document states at check", documents)])
                      + f"<pre>{esc(json.dumps(data, indent=2))}</pre>"))


def case(db, key):
    app = one(db, """SELECT a.*,p.name AS product, p.annual_bps, customer.display_name FROM loan_application a
        JOIN loan_product p ON p.id=a.product_id JOIN applicant customer ON customer.id=a.applicant_id
        WHERE a.id=?""", key)
    if app is None:
        return None
    current = latest_decision(db, key)
    eligible = eligibility(db, key, timestamp(datetime.now()))
    open_case = current is None or current["outcome"] == "manual_review"
    docs = list(db.execute("""SELECT d.kind,e.status,e.recorded_at FROM document d
        JOIN document_event e ON e.document_id=d.id WHERE d.application_id=? ORDER BY e.recorded_at,e.id""", (key,)))
    document_lines = []
    for kind, status in {row["kind"]: row["status"] for row in docs}.items():
        title = kind.replace("_", " ").capitalize()
        next_status = {"missing": "received", "received": "verified"}.get(status)
        action = ""
        if open_case and next_status:
            fields = f'<input type="hidden" name="kind" value="{esc(kind)}"><input type="hidden" name="status" value="{next_status}">'
            action = form(fields, "document", f"Mark {title.lower()} {next_status}", key)
        document_lines.append(f'<div class="doc-line"><div><strong>{esc(title)}</strong>{badge(status)}</div>{action}</div>')
    assess_action = form("", "assess", "Run assessment", key) if open_case else ""
    payment_rows = list(db.execute("SELECT decision_id,paid_at,amount FROM disbursement WHERE application_id=?", (key,)))
    paid = bool(payment_rows)
    eligibility_label = (("Disbursed · " + eligible) if paid else
                         "Original request already disbursed" if eligible == "Already disbursed" else eligible)
    eligibility_status = "disbursed" if paid else "eligible" if eligible == "Eligible" else "blocked"
    payment = form("", "disburse", "Record disbursement", key) if eligibility_status == "eligible" else ""
    duplicate = (f'<div class="notice">Duplicate of <a href="/case/{esc(app["duplicate_of"])}">{esc(app["duplicate_of"])}</a></div>'
                 if app["duplicate_of"] else "")
    checks = list(db.execute("""SELECT d.*,c.checked_at FROM decision d JOIN affordability_check c ON c.id=d.check_id
      WHERE c.application_id=? ORDER BY c.id DESC""", (key,)))
    history = "".join(details(f'{DECISIONS[row["outcome"]]} · {row["id"]}', decision_evidence(row)) for row in checks[1:])
    decision_panel = (decision_evidence(current) if current else "<p>Run an assessment to calculate affordability and apply the lending rules.</p>")
    schedules = list(db.execute("""SELECT installment,due_on,principal,interest,payment,remaining
      FROM repayment_schedule WHERE application_id=? ORDER BY installment""", (key,)))
    repayment = "<p>A schedule is generated after approval.</p>"
    if schedules:
        repayment = evidence([("Installments", len(schedules)), ("Total interest", money(sum(row["interest"] for row in schedules))),
                              ("Total repayment", money(sum(row["payment"] for row in schedules)))])
        repayment += table(["#", "Due date", "Principal", "Interest", "Payment", "Remaining balance"],
                           ([row["installment"], esc(row["due_on"]), *(money(value) for value in tuple(row)[2:])]
                            for row in schedules))
    sources = "".join(details(title, records(db.execute(f"SELECT * FROM {name} WHERE applicant_id=?",
                                                          (app["applicant_id"],))))
                      for title, name in [("Employment records", "employment"), ("Income records", "income_record"),
                                          ("Credit facilities", "credit_facility")])
    payments = table(["Decision ID", "Recorded at", "Amount"],
                     ([esc(row[0]), esc(row[1]), money(row[2])] for row in payment_rows)) if paid else ""
    snapshot = json.loads((DATA / "validation.json").read_text())
    findings = [row["issue_type"].replace("_", " ").capitalize() for row in snapshot["findings"]
                if row["application_id"] == key]
    audit = ('<div class="notice"><strong>Seeded audit findings</strong><ul>' +
             "".join(f"<li>{esc(finding)}</li>" for finding in findings) + "</ul></div>") if findings else ""
    state = current["outcome"] if current else "submitted"
    return f"""<a class="back" href="/">← Application queue</a>
      {heading(key, f'{app["display_name"]} · {app["applicant_id"]}', badge(state))}
      {duplicate}{audit}
      <section class="panel" aria-label="Request details"><h2>Loan request</h2>
      {evidence([("Product", app["product"]), ("Principal", money(app["principal"])),
        ("Term", f'{app["term"]} months'), ("Annual rate", f'{app["annual_bps"]/100:.2f}%'),
        ("Submitted", app["submitted_at"][:10])])}</section>
      <nav class="section-links" aria-label="Case sections"><a href="#assessment">Assessment</a>
        <a href="#documents">Documents</a><a href="#repayment">Repayment</a><a href="#sources">Applicant records</a></nav>
      <div class="case-grid"><div>
        <section class="panel" id="assessment"><h2>Affordability &amp; decision</h2>
          {decision_panel}{assess_action}</section>
        {('<h2>Earlier decisions</h2>' + history) if history else ''}
      </div><div>
        <section class="panel" id="documents"><h2>Document verification</h2>{''.join(document_lines)}</section>
        {details("Document history", records(docs))}
        <section class="panel"><h2>Disbursement</h2><p>{badge(eligibility_status, eligibility_label)}</p>
          {payment}{payments if paid else ''}</section>
      </div></div>
      <section class="panel" id="repayment"><h2>Repayment schedule</h2>{repayment}</section>
      <section id="sources" aria-label="Applicant records">{sources}</section>"""


def product_catalog(db):
    fields = """<label>Product ID<input name="product" maxlength="16" required></label>
       <label>Product name<input name="name" maxlength="80" required></label>
       <label>Minimum principal (INR)<input name="min_principal" type="number" min="1" required></label>
       <label>Maximum principal (INR)<input name="max_principal" type="number" min="1" required></label>
       <label>Annual interest rate (%)<input name="rate" type="number" step=".01" min="0" max="100" required></label>
       <label>Minimum term (months)<input name="min_term" type="number" min="1" max="1200" required></label>
       <label>Maximum term (months)<input name="max_term" type="number" min="1" max="1200" required></label>"""
    catalog = table(["Product ID", "Product", "Minimum principal", "Maximum principal", "Annual rate", "Term", "Effective date"],
        ([esc(row["id"]), esc(row["name"]), money(row["min_principal"]), money(row["max_principal"]),
          f'{row["annual_bps"]/100:.2f}%', f'{row["min_term"]}–{row["max_term"]} mo',
          esc(row["effective_on"])] for row in products(db)))
    return (heading("Loan products", "Manage the product limits and rates available to new applications.")
            + '<section class="panel"><h2>Product catalog</h2>' + catalog + '</section>'
            + details("Add a loan product", form(fields, "product", "Add product")))


def add_product(db, data, now):
    key, name = data["product"].strip().upper(), data["name"].strip()
    if not re.fullmatch(r"[A-Z0-9-]{1,16}", key) or not 1 <= len(name) <= 80:
        raise ValueError("Enter a product ID and name. IDs use letters, digits, and hyphens.")
    minimum, maximum = int(data["min_principal"]), int(data["max_principal"])
    short, long = int(data["min_term"]), int(data["max_term"])
    rate = Decimal(data["rate"]) * 100
    if not (1 <= minimum <= maximum <= 100_000_000 and 1 <= short <= long <= 1200
            and rate.is_finite() and 0 <= rate <= 10_000 and rate == rate.to_integral_value()):
        raise ValueError("Check the principal limits, term limits, and annual rate.")
    if one(db, "SELECT 1 FROM loan_product WHERE id=? OR name=?", key, name):
        raise ValueError("This product ID or name is already in use.")
    insert(db, "loan_product", key, name, minimum, maximum, int(rate), short, long, now[:10])


def report():
    summary = json.loads((DATA / "validation.json").read_text())
    downloads = """<div class="section-links">
      <a class="button" href="/exports/portfolio.zip">Download portfolio</a>
      <a class="button secondary" href="/exports/validation.json">Validation report</a>
      <a class="button secondary" href="/exports/ground_truth.json">Ground truth</a></div>"""
    outcomes = table(["Product", "Decision", "Applications"],
                     ([esc(row["product_id"]), badge(row["outcome"]), f'{row["applications"]:,}']
                      for row in summary["outcomes_by_product"]))
    checks = summary["reproducibility"]
    return f"""{heading("Portfolio audit", "Review the bundled dataset, detected exceptions, and reproducibility evidence.")}
      {downloads}<div class="metrics">
      <div class="metric"><span>Clean baseline findings</span><strong>{summary['baseline_findings']}</strong></div>
      <div class="metric"><span>Injected cases detected</span><strong>{summary['injected_cases_detected']} / {summary['injected_cases']}</strong></div>
      <div class="metric"><span>Unexpected findings</span><strong>{summary['unexpected_findings']}</strong></div>
      <div class="metric"><span>Eligible at snapshot</span><strong>{summary['eligible_applications']:,}</strong></div></div>
      <section class="panel queue"><div class="panel-title"><h2>Expected findings</h2>
      <p>Open a case to inspect the records behind the finding.</p></div>
      {table(["Application", "Finding"], ([f'<a href="/case/{esc(row["application_id"])}">{esc(row["application_id"])}</a>',
           esc(row["issue_type"].replace("_", " ").capitalize())] for row in summary["findings"]))}</section>
      <div class="case-grid"><section class="panel queue"><div class="panel-title"><h2>Decisions by product</h2></div>{outcomes}</section>
      <section class="panel queue"><div class="panel-title"><h2>Baseline coverage</h2></div>
      {table(["Case", "Applications"], ([esc(label.replace("_", " ").capitalize()), f'{count:,}']
           for label, count in summary["baseline_coverage"].items()))}</section></div>
      <section class="panel"><h2>Reproducibility</h2>
      {evidence([("Generation seed", summary["seed"]), ("Runs", checks["runs"]),
                 ("Row counts", "Matched" if checks["row_counts_match"] else "Differ"),
                 ("Checksums", "Matched" if checks["checksums_match"] else "Differ"),
                 ("SQL import", "Verified" if summary["sql_round_trip"] else "Failed")])}
      {details("Archive SHA-256", f'<pre>{esc(checks["archive_sha256"])}</pre>')}</section>"""


def current_link(active):
    return ' aria-current="page"' if active else ""


def icon(path):
    return f'<svg aria-hidden="true" width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">{path}</svg>'


class Handler(BaseHTTPRequestHandler):
    def role(self):
        raw = self.headers.get("Cookie", "")
        selected = next((part.split("=", 1)[1] for part in raw.split("; ") if part.startswith("role=")), "operations")
        return selected if selected in ROLES else "operations"

    def page(self, content, status=200):
        path = urlsplit(self.path).path
        navigation = [
            ("/", "Applications", '<path d="M4 4h16v16H4zM8 9h8M8 14h8"/>', path not in {"/products", "/audit"}),
            ("/products", "Loan products", '<path d="M4 7h16v13H4zM8 7V4h8v3M4 12h16"/>', path == "/products"),
            ("/audit", "Portfolio audit", '<path d="M4 19h16M7 16v-5m5 5V5m5 11V8"/>', path == "/audit"),
        ]
        links = "".join(f'<a href="{url}"{current_link(active)}>{icon(symbol)}{label}</a>'
                        for url, label, symbol, active in navigation)
        title = next(label for _, label, _, active in navigation if active)
        logo = icon('<path d="m4 11 8-6 8 6M6 12v6h12v-6M3 20h18M10 12v6m4-6v6"/>')
        role = self.role()
        role_options = "".join(f'<option value="{key}"{" selected" if key == role else ""}>{label}</option>' for key, (label, _) in ROLES.items())
        html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>{title} · Loan Workbench</title><link rel="stylesheet" href="/static/workbench.css"></head>
        <body><a class="skip" href="#main">Skip to content</a>
        <aside class="sidebar"><a href="/" class="brand">{logo}Loan Workbench</a>
          <nav aria-label="Main">{links}</nav><footer>Local lending workspace</footer></aside>
        <div class="workspace"><header class="topbar"><strong>{title}</strong>
        <form class="role-switcher" method="get" action="/switch-role"><label>Workspace role<select name="role">{role_options}</select></label><button class="secondary">Switch</button></form></header>
        <main id="main">{content}</main></div></body></html>"""
        body = html.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def serve_file(self, path, content_type, download=False):
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        db = self.server.db
        if url.path == "/switch-role":
            role = parse_qs(url.query).get("role", ["operations"])[0]
            if role not in ROLES:
                role = "operations"
            self.send_response(303)
            self.send_header("Set-Cookie", f"role={role}; Path=/; SameSite=Lax")
            self.send_header("Location", "/")
            self.end_headers()
            return
        if url.path == "/":
            self.page(home(db, parse_qs(url.query)))
        elif url.path == "/new":
            self.page(new_application(db))
        elif url.path == "/static/workbench.css":
            self.serve_file(ROOT / "static" / "workbench.css", "text/css; charset=utf-8")
        elif url.path == "/products":
            self.page(product_catalog(db))
        elif url.path in DOWNLOADS:
            self.serve_file(DATA / DOWNLOADS[url.path], "application/octet-stream", download=True)
        elif url.path == "/audit":
            self.page(report())
        elif url.path.startswith("/case/"):
            content = case(db, url.path.removeprefix("/case/"))
            self.page(content or "<h1>Application unavailable</h1>", 200 if content else 404)
        else:
            self.page("<h1>Page unavailable</h1>", 404)

    def do_POST(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if self.path != "/action" or not 0 < size <= 4096:
                raise ValueError("Invalid request.")
            data = {key: values[0] for key, values in parse_qs(self.rfile.read(size).decode()).items()}
            if not secrets.compare_digest(data.get("token", ""), TOKEN):
                raise ValueError("Refresh the page and try again.")
            db = self.server.db
            now = timestamp(datetime.now())
            key = data.get("key", "")
            action = data["action"]
            role = self.role()
            permissions = ROLES[role][1]
            if action not in permissions:
                raise ValueError(f"{ROLES[role][0]} cannot perform this action. Switch workspace role first.")
            with db:
                db.execute("BEGIN IMMEDIATE")
                if action == "submit":
                    principal, term = int(data["principal"]), int(data["term"])
                    if not 1 <= principal <= 100_000_000 or not 1 <= term <= 1200:
                        raise ValueError("Principal or term is outside the supported input range.")
                    key = submit(db, data["applicant"].upper().strip(), data["product"], principal, term, now)
                elif action == "product":
                    add_product(db, data, now)
                else:
                    if not one(db, "SELECT 1 FROM loan_application WHERE id=?", key):
                        raise ValueError("Application unavailable.")
                    if action == "document":
                        current = latest_decision(db, key)
                        if current and current["outcome"] != "manual_review":
                            raise ValueError("The application has a final decision.")
                        if data["kind"] not in {"identity", "income_proof"}:
                            raise ValueError("Unknown document type.")
                        set_document(db, key, data["kind"], data["status"], now)
                    elif action == "assess":
                        assess(db, key, now)
                    elif action == "disburse":
                        disburse(db, key, now)
                    else:
                        raise ValueError("Unknown action.")
            self.send_response(303)
            self.send_header("Location", "/products" if action == "product" else f"/case/{key}")
            self.end_headers()
        except (ValueError, KeyError, InvalidOperation) as error:
            self.page(f"<h1>Action could not be completed</h1><p class='error'>{esc(error)}</p>", 400)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=str(ROOT / "workbench.sqlite"))
    args = parser.parse_args()
    db = connect(args.db)
    if not one(db, "SELECT 1 FROM sqlite_schema WHERE name='applicant'"):
        with zipfile.ZipFile(DATA / "portfolio.zip") as archive:
            db.executescript(archive.read("seed.sql").decode())
    with HTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.db = db
        print(f"Loan Workbench running at http://127.0.0.1:{args.port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            db.close()


if __name__ == "__main__":
    main()
