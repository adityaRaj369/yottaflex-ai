# Data dictionary

[`schema.sql`](schema.sql) is the executable schema. Fields are required unless marked **optional**. Only keys and fields listed as unique have a uniqueness requirement. Money is an integer in whole INR. Rates and ratios ending in `_bps` use integer basis points (100 bps = 1%). Terms and installment numbers count months. A date is ISO 8601 YYYY-MM-DD. Timestamps use local business time with six fractional second digits and the same clock basis throughout a run. Effective ranges include their start and exclude their end.

## Master records

| Entity and fields | Type, allowed values, and validation |
| --- | --- |
| Applicant · `id`, `display_name`, `created_on` | Text PK `APP-2026-NNNNNN`; nonempty synthetic label; registration date at or before application. |
| Employment · `id`, `applicant_id`, `start_on`, `end_on` **optional**, `kind` | Text PK `EMP-NNNNNN-NN`; Applicant FK; employment dates with end strictly after start and null for open employment; enum `salaried`, `contract`. Active employment is selected by the check date. |
| IncomeRecord · `id`, `applicant_id`, `source`, `effective_on`, `monthly` | Text PK `INC-NNNNNN-revision-source`; Applicant FK; enum `payroll`, `declared`; effective date; positive integer monthly income. Applicant, source and effective date are jointly unique. The next record for that source supersedes it from the next effective date. |
| CreditFacility · `id`, `applicant_id`, `opened_on`, `closed_on` **optional**, `balance`, `monthly_due`, `late_payments` | Text PK `CR-NNNNNN-NN`; Applicant FK; facility dates with end strictly after start, null for open; nonnegative integer outstanding INR, monthly INR and count of recorded late payments. Closed facilities are excluded from assessment. |
| LoanProduct · `id`, `name`, `min_principal`, `max_principal`, `annual_bps`, `min_term`, `max_term`, `effective_on` | Text PK of 1–16 uppercase letters, digits, or hyphens, with `P01`–`P04` in the bundled portfolio; unique nonempty name; positive principal bounds and month bounds ordered min ≤ max; nonnegative annual rate; activation date at or before submission. |

## Applications and events

| Entity and fields | Type, allowed values, and validation |
| --- | --- |
| LoanApplication · `id`, `applicant_id`, `product_id`, `principal`, `term`, `submitted_at`, `duplicate_of` **optional** | Text PK `LOAN-2026-NNNNNN`; Applicant and LoanProduct FKs; positive integer requested INR and months, checked against product bounds by policy; submission timestamp; self FK to the matching earlier original request, or null. |
| Document · `id`, `application_id`, `kind` | Text PK `DOC-<application-id>-NNN`; LoanApplication FK; enum `identity`, `income_proof`, unique per application. |
| DocumentEvent · `id`, `document_id`, `status`, `recorded_at` | Integer PK assigned by SQLite; Document FK; enum `missing`, `received`, `verified` in that order; timestamp at or after submission and the preceding event. |
| AffordabilityCheck · `id`, `application_id`, `checked_at`, `payroll_id`, `declared_id`, `employment_id` | Integer PK; LoanApplication FK; timestamp at or after submission; two IncomeRecord FKs for the respective effective income sources and one Employment FK for the effective job. Source rows belong to the application applicant. |
| AffordabilityCheck · `income`, `debt`, `payment`, `dti_bps`, `post_loan_bps`, `residual`, `score`, `income_conflict` | Integer payroll INR/month (>0), monthly debt INR (≥0), estimated installment INR (≥0), pre- and post-loan ratios (≥0), signed residual INR/month, score 300–850, and boolean 0/1. All must replay from source rows and policy formulas. |
| CheckFacility · `check_id`, `facility_id` | Integer AffordabilityCheck FK and text CreditFacility FK form the composite PK. Each selected active facility is recorded once per check. |
| Decision · `id`, `check_id`, `decided_at`, `outcome`, `rule`, `inputs` | Text PK `DEC-<application-id>-NN`; unique AffordabilityCheck FK; timestamp at or after check; enum `approved`, `rejected`, `manual_review`; first matching rule code from [policy()](lending.py); valid JSON snapshot that must match reconstructed inputs. |
| RepaymentSchedule · `application_id`, `installment`, `due_on`, `principal`, `interest`, `payment`, `remaining` | LoanApplication FK and positive integer installment form the composite PK. Due date follows approval and increases monthly. Four nonnegative integer amounts reconcile to the amortization formula, with payment equal to principal plus interest and zero final balance. |
| Disbursement · `id`, `application_id`, `decision_id`, `paid_at`, `amount` | Integer PK; unique LoanApplication and Decision FKs; timestamp at or after approval; positive integer INR equal to approved principal. Decision must belong to the same application. Duplicate groups allow a single successful payment. |

## Cardinality and lifecycle

- One applicant has zero or more applications, jobs, income records, and facilities. One product has zero or more applications. Every child has one corresponding parent.
- One original application can have zero or more duplicate children. Each duplicate points to one original. A nonduplicate has a null duplicate reference.
- An application has two required document kinds, and each document has one or more events. An application can have zero or more checks. Each recorded check has exactly one decision in normal workflow. An income record or job can be referenced by many checks.
- An approved application has one or more installment rows and zero or one disbursement. Each disbursement points to the exact approval. Each decision can fund at most one disbursement.
- Checks and credit facilities have a many-to-many relationship via CheckFacility. A check can use zero or more facilities, and a facility can contribute to multiple checks. The decision JSON also preserves the selected IDs for inspection.
- Application state is derived from submission, decision, and disbursement records. Assessment progresses from submitted to manual review, approval, or rejection. A manual-review decision can be followed by another assessment. Approval can progress to disbursed. Rejection and disbursement are terminal.
- The separate ground-truth file has one row per application. `application_id` is unique and references the application, `expected_decision` and `expected_rule` use the decision vocabularies above, and optional `issue_type` is one of the five primary fixture codes in the validation report.
