# Decisions, assumptions, and limitations

## Decisions

- Python and SQLite were selected because they run locally with no paid services or credentials.
- Synthetic data is used for every applicant, employment, income, credit, document, and application record. No personal or confidential data is imported.
- The policy thresholds are explicit and deterministic. They are illustrative challenge rules and are documented in the README.
- A manual-review decision may be reassessed after evidence is remediated. Approval and rejection are terminal. Disbursement is allowed only after an approval, verified documents, replayable evidence, and a reconciled schedule.
- Audit fixtures are applied after a clean baseline is verified. Every fixture has a labelled issue type and expected decision.
- The local role switcher models the four named actors. It is a workflow demonstration, not production authentication.

## Assumptions

- Money is represented as whole INR values.
- Annual interest rates are stored as basis points. Repayment uses a fixed-rate monthly amortization formula.
- An income conflict means the declared income differs from payroll by more than 15%.
- A duplicate means the same applicant, product, principal, and term submitted within 30 days of an original request.
- Applicant IDs and loan IDs use the 2026 naming convention required by the prompt.

## Limitations

- The application is a local evaluator demo. It does not provide production identity management, external document storage, payment rails, email notifications, or hosted deployment.
- Document records represent document status events. They do not upload or retain document files.
- The policy is designed to demonstrate traceable deterministic decisions. It is not a lending policy for real applicants.
- The supplied Candidate FAQ and Submission Guidelines were reviewed after the initial implementation. Their prompt-history requirement is satisfied by `ai-disclosure/PROMPT_HISTORY.md`.
- A public GitHub repository and portal submission are external actions. They remain pending until a repository destination is supplied and the candidate performs the final submission.
