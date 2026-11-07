# Submission readiness

This checklist maps the supplied problem statement to the repository.

| Requirement | Evidence |
| --- | --- |
| Master records before dependent records | `generate()` inserts applicants, employment, income, credit, and products before applications, documents, checks, decisions, and schedules. |
| Ten core entities and relationships | `schema.sql`, `DATA_DICTIONARY.md`, and the `check_facility` bridge table. |
| Deterministic IDs | `lending.py` uses the required `APP`, `LOAN`, `DOC`, and `DEC` formats. |
| 10,000 applicants and 6,000 applications across four products | `deliverables/validation.json` and `portfolio.zip`. |
| Required baseline variation | `baseline_coverage` in `validation.json` includes missing income proof, debt burden, short tenure, duplicates, borderline scores, boundaries, and all three outcomes. |
| Required anomalies | Five labelled cases in `ground_truth.json`, listed in the audit page. |
| Derived values | Affordability, scores, decisions, schedules, balances, and statuses are replayed from source records. |
| Separate ground truth | `deliverables/ground_truth.json` is not used as a decision input. |
| Validation | `portfolio.py --verify`, `test_workbench.py`, foreign keys, enums, chronology, reconciliation, lifecycle, and duplicate checks. |
| Reproducibility | Two full runs compare row counts, labels, and SHA-256 checksums. |
| CSV, JSON, SQL, README, dictionary, manifest | All are bundled in `deliverables/` or the repository root. |
| Actors | The local role switcher exposes customer, operations, risk/compliance, and administrator workspaces. |
| Public repository and portal submission | Not performed. The repository remains local until a GitHub destination is supplied. |
| Prompt history and transparent AI use | `ai-disclosure/PROMPT_HISTORY.md`. |
| Assumptions, trade-offs, limitations, and requirement deviations | `DECISIONS_AND_LIMITATIONS.md`. |

The Candidate FAQ and Submission Guidelines PDFs referenced by the prompt were not included in the supplied files. Their extra requirements cannot be checked from this workspace. The public-repository URL and portal submission are intentionally left for the candidate to perform.

Before submission, run:

```sh
python3 -m unittest -v
python3 portfolio.py --verify
python3 app.py
```

Then inspect the public repository from a private browser window and submit that URL once through the portal.
