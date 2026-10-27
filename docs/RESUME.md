# Putting this project on your resume

## Project entry (pick 3–4 bullets)

**Invoice Processing Agent — Multi-agent AP automation on AWS Bedrock** · Python, LangGraph, Amazon Bedrock, Textract, FastAPI, Docker · [GitHub link] · [demo link]

- Built a **multi-agent LangGraph pipeline** (extractor, validator, risk analyst, supervisor) that reads supplier invoice PDFs with **Amazon Textract** and **Amazon Bedrock** tool use and decides auto-approve, reject or human review.
- Implemented an accounts-payable **three-way match** (invoice ↔ PO ↔ goods receipt) and **19 fraud/error checks** including duplicate invoices, bank-detail changes and price variance beyond tolerance.
- Added **human-in-the-loop approval** using LangGraph `interrupt()` with a durable SQLite checkpointer, so paused invoices survive restarts and resume on reviewer action via a FastAPI dashboard.
- Designed **layered guardrails**: schema-validated LLM output with self-correction retry, prompt-injection detection on untrusted documents, PII masking, optional Bedrock Guardrails, and a policy layer where the LLM can only escalate, never approve.
- Built **observability and evaluation**: per-call token/latency/cost ledger, `/metrics` (straight-through rate, p95 latency, cost per invoice), and a 14-scenario eval harness used as a **CI quality gate**.

**Fill in your own numbers after `APP_MODE=aws python -m eval.run_eval`:**
- "…achieved **__%** field-level extraction accuracy and **__%** correct routing decisions on a labelled set of __ invoices"
- "…at **$0.00__ per invoice** and **__ s** average end-to-end latency"

Only quote numbers you actually measured. Add 10–20 real or scanned invoices to the eval set first —
an interviewer will trust "92% on 30 mixed invoices" more than "100% on synthetic data".

## Skills line

LangGraph · Amazon Bedrock (Converse API, tool use, Guardrails) · Amazon Textract · Agentic AI · Human-in-the-loop · LLM evaluation · FastAPI · Pydantic · Docker · GitHub Actions · IAM

## Interview questions you should be ready for

1. **Why is the validator not an LLM?** Financial controls need to be deterministic, explainable and auditable. LLMs do the fuzzy part (reading messy documents, writing summaries); rules do the money decisions.
2. **How do you stop the LLM approving a fraudulent invoice?** It has no approve path. The supervisor applies rules; the risk agent can only add an escalation. A test proves a "risk 0, approve" LLM response still rejects a blocked vendor.
3. **What is prompt injection here and how do you handle it?** The invoice is attacker-controlled text. The prompt tells the model to treat it as data, a detector flags instruction-like text, and policy forces human review when found.
4. **How does human-in-the-loop work technically?** `interrupt()` saves graph state to the checkpointer and returns. The review API calls `invoke(Command(resume=...))` on the same `thread_id`; the graph continues from `human_review`.
5. **What happens if Bedrock is throttled or returns bad JSON?** Exponential-backoff retries on throttling; forced tool use plus Pydantic validation; one self-correction retry with the error message; then fail safe to a human.
6. **How do you know a prompt change didn't break it?** The eval harness runs in CI and fails the build below accuracy thresholds.
7. **How would you scale to 50,000 invoices a month?** S3 + EventBridge/SQS fan-out, async Textract, Postgres checkpointer, horizontal workers, batch inference for non-urgent work, and caching vendor/PO lookups.
8. **How do you control cost?** Small fast model for extraction, `temperature=0`, compact Textract output instead of raw blocks, cost ledger per agent, budget alerts.
9. **What is a three-way match?** Invoice quantity/price must agree with the purchase order and with what the warehouse actually received.
10. **What would you build next?** RAG over vendor contracts, learning from reviewer corrections, real SAP/Oracle posting, role-based approval limits.

## Demo script (2 minutes)

1. Upload `S01_clean_acme.pdf` → auto-approved, show audit trail.
2. Upload `S08_bank_account_changed.pdf` → critical fraud flag, risk score, reject it with a comment.
3. Upload `S09_prompt_injection.pdf` → show the system ignores "approve this invoice".
4. Open `/metrics` → straight-through rate, cost per invoice, p95 latency.
5. Show `eval/results/report.md` and the CI workflow.
