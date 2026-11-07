# Loan Workbench

Loan Workbench is a local application queue, credit policy simulator, and synthetic portfolio audit. It uses Python's standard library and SQLite. All amounts are simulated INR and no payment network is involved.

## Run locally

Use Python 3.11 or later with SQLite 3.38 or later.

```sh
python3 app.py
```

Open http://127.0.0.1:8000. The first launch imports the bundled SQL seed into `workbench.sqlite`. Changes persist locally. Use `python3 app.py --db demo.sqlite --port 8001` for a separate copy.

1. Open **New application**. Use `APP-2026-000001`, **Everyday personal**, INR **100000**, and **12 months**.
2. Mark the identity document received, then verified. Run the assessment with income proof missing to enter manual review.
3. Mark income proof received, then verified. Run the assessment again. This applicant meets the policy with those inputs. Expand **Decision history** and **Repayment schedule** to inspect the result.
4. Record the simulated disbursement. The eligibility status changes to **Already disbursed**.
5. Open **Portfolio audit** to find the five seeded exceptions. The audit page describes the bundled portfolio snapshot. Queue counts and application details reflect current local activity.

The queue supports search by application ID or applicant, filters by product and decision, and a funding filter that rechecks eligibility for every matching case. The case view shows the financial inputs, document states, and rule behind each assessment revision. The product catalog lets you add a product with effective limits, terms, and rates. The portfolio audit links to case evidence and the dataset downloads.

The role switcher provides four explicit local workspaces. Customer can submit and provide document statuses. Operations can submit, update documents, assess, and record an eligible disbursement. Risk and compliance can assess or reassess an open case. System administration can maintain products and inspect audit exports. The role is stored in a browser cookie for this local demo. It is an interaction model for the challenge workflow, not an identity provider.

A customer submits the request, operations records document progress and checks, risk can inspect policy evidence and reassess after remediation, and administration can inspect product settings and rebuild or validate the portfolio. These are responsibilities in a shared local demo. The app binds to loopback and uses a form token. Role-based accounts and document file storage are outside this demo's scope. Document evidence is represented by status events.

## Generate and validate

```sh
python3 portfolio.py
python3 portfolio.py --verify
python3 portfolio.py --applicants 50 --applications 35 --seed 20261022 --output starter
python3 -m unittest -v
```

The full run uses 10,000 applicants, 6,000 applications, and seed 20261022. A starter run uses the same four products, policy, and fixture construction. The generator accepts 8–999999 applicants and 8–8000 applications. Exact archive comparison assumes the same runtime and compression library. The bundled report records both the archive hash and per-file SHA-256 values.

| Artifact | Contents |
| --- | --- |
| [Portfolio archive](deliverables/portfolio.zip) | One CSV and one JSON array per table, plus a standalone SQLite seed script |
| [Ground truth](deliverables/ground_truth.json) | Expected decision and rule per application, with issue types on the five injected cases |
| [Validation results](deliverables/validation.json) | Counts, decision distribution, eligibility, observed issue types, SQL restore check, and reproducibility hashes |
| [Schema](schema.sql) and [data dictionary](DATA_DICTIONARY.md) | Types, constraints, cardinality, dates, and lifecycle rules |
| [Source manifest](SOURCE_MANIFEST.md) | Provenance and generation assumptions |
| [Prompt history and AI disclosure](ai-disclosure/PROMPT_HISTORY.md) | Required prompts, AI-use disclosure, and validation ownership |
| [Decisions and limitations](DECISIONS_AND_LIMITATIONS.md) | Assumptions, trade-offs, limitations, and requirement deviations |

The generator builds a clean portfolio, validates it, constructs controlled exceptions, and compares independently detected findings with expected results. Expected baseline decisions come from replaying policy from source records. Each injected fixture also has a fixed expected decision and rule, which the build verifies. The duplicate disbursement produces two findings because it also lacks a valid approval. It restores the exported SQL into an empty database and checks the same findings and counts. It repeats the entire process and compares counts, labels, and hashes. The application imports only the SQL seed. The standalone verifier checks the archive and per-file hashes, compares every CSV and JSON row with restored SQL, reruns data validation, and checks the separate labels.

## Decision and repayment rules

At assessment, use the latest effective payroll and declared monthly income, the most recent active job, and every credit facility open on that date. All contributing record IDs, dates of the check and decision, document states, product terms, rule code, and numerical inputs are retained or reachable by foreign key.

- DTI is existing monthly debt payments divided by payroll income. Post-loan DTI adds the estimated new installment. Both are rounded to integer basis points. Residual income is payroll less existing monthly debt and the estimated installment.
- The synthetic score is `765 − floor(DTI bps / 140) − 24 × late payments + min(floor(tenure days / 90), 12)`, clipped to 300–850. Income conflicts when the absolute difference exceeds 15% of payroll.
- A duplicate is the same applicant, product, principal and term submitted within 30 days of an original request. Duplicates retain a link to the original.
- Policy applies the first matching rule below. Thresholds are illustrative and deliberately fixed for the simulation. Inputs are financial and employment records, product terms, document states, and duplicate indicators.

| Order | Condition | Result |
| --- | --- | --- |
| 1 | Term outside inclusive product bounds | Reject |
| 2 | Principal outside inclusive product bounds | Reject |
| 3 | Duplicate request | Reject |
| 4 | Either required document is unverified | Manual review |
| 5 | Income sources conflict | Manual review |
| 6 | Post-loan DTI above 60% or residual under INR 12,000 | Reject |
| 7 | Score below 635 | Reject |
| 8 | Active employment tenure under 180 days | Manual review |
| 9 | Post-loan DTI at least 45% or score below 680 | Manual review |
| 10 | Remaining cases | Approve |

A manual-review case can be assessed again after document updates. The system records a new check and decision and keeps previous evidence. Approval and rejection end assessment. Approval creates a fixed-rate amortization schedule using the annual rate divided by 12. Installments and interest use half-up whole-rupee rounding. The last installment clears the remaining principal. Installments begin the month after approval and use the approval day capped at 28.

Disbursement requires a nonduplicate approved request, chronologically valid checks and approval, approval inputs that replay from stored evidence, verified documents, and a schedule that reconciles to the approved terms. The request and duplicate group must have no previous disbursement. The insert records amount, decision, and time in the same transaction as the eligibility check.

## Coverage and metrics

The baseline mixes skewed income and requested amounts, variable tenure, credit obligations and late payments, missing proof, near-threshold applications, and duplicate requests. The portfolio includes all three decision outcomes and applications at inclusive product boundaries. Audit fixtures are isolated to applications ending in 000002 through 000006. The validation report shows baseline counts for every required coverage category, decision counts by product and policy rule, eligible cases at the snapshot date, and detection results. The generator fails if any required baseline category is absent, including at the minimum starter size. The bundled SQL contains intentional business-rule exceptions, which are listed by the audit and retained as evidence.
