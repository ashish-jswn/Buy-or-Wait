# Buy or Wait? — a deterministic affordability agent

For each purchase request, decide whether the user can **safely** pay now, pay part now,
use an installment option, wait, or not proceed — by reconstructing their cash position
from ~25,000 financial events and simulating their balance forward 90 days.

Built for the HackerRank Orchestrate hackathon (September 2026). See
[`problem_statement.md`](problem_statement.md) for the full task spec.

```bash
python -m venv .venv && .venv/Scripts/python.exe -m pip install -r code/requirements.txt
python code/main.py           # writes output.csv — no API key needed, see below
cd code && python -m pytest   # 359 tests, no API key needed
```

**This runs with no credentials.** The model's readings of 213 messages and 16 images are
committed under `code/extraction/cache/`, keyed on a hash of prompt + schema + context +
content. (213, not 215 — the other two never reached a model; see below.) A full 250-row
run reproduces byte-for-byte from that cache without making a single API call, so every
number below is verifiable by anyone who clones this. Copy `.env.example`
to `.env` and fill it in only to re-extract from scratch — or after editing a prompt, which
invalidates the cache by design rather than silently serving stale readings.

## What it produces

[`output.csv`](output.csv) — one row per request, all seven required fields populated. Four
real rows from the committed run, verbatim:

**`request_26` — affordable now**
`15656000` · `affordable_now` / `full_payment` · plan `2025-08-03:15656000`
> Pay IDR 15,656,000 today. This leaves at least IDR 24,768,300 available over the next 90 days.

**`request_30` — spread over installments**
`775.2` · `affordable_with_plan` / `installments` · plan `2026-04-06:268.74|2026-05-06:268.74|2026-06-05:268.74`
> Use 3 installments of USD 268.74, starting 6 April 2026. This leaves at least USD 900 available.

**`request_29` — affordable only after a spending change**
`500.41` · `affordable_with_plan` / `full_payment` · `reduce_to:event_2654:712.80`
> Reduce the lunch with colleagues to ZAR 712.80, then pay ZAR 51,524 today. This leaves at least ZAR 28,300 available.

**`request_49` — refused**
`not_affordable` / `not_recommended` · plan `none` · no completion date
> Do not make this payment by 22 May 2024. None of the available options keeps the IDR 20,275,700 minimum protected.

Across all 250 requests: 73 `not_affordable`, 70 `affordable_with_plan`, 57 `affordable_later`,
50 `affordable_now` — and **0 rows replaced by a fallback**, meaning every answer came from a
completed forecast rather than a default.

> Note the two similarly-named files. **`output.csv` in the repository root is the generated
> result.** `dataset/output.csv` is HackerRank's blank submission template, shipped with the
> dataset and left unmodified — it is empty by design.

---

## The central design decision

**The model never does arithmetic, and never decides anything.**

The obvious way to build this is to hand a language model the user's financial history and
ask "can they afford it?". That produces an answer that is unverifiable, non-reproducible,
and wrong in ways you cannot see. The problem statement already specifies the ranking order,
the conflict-resolution order and the plan format exhaustively — there is no judgment left
to delegate.

So the system splits in two:

| layer | what it does | how |
|---|---|---|
| **Evidence extraction** | Reads 215 free-text messages and 16 receipt images for facts | Language + vision model, strict JSON schema, one call per item |
| **Decision engine** | Cash-state reconstruction, 90-day daily forecast, plan generation, ranking, safety verification | Pure Python, `Decimal` throughout, no model access |

The model reports what a message *says* (`salary_increase`, amount `42750000`, currency
`IDR`, effective `2025-08-15`). Whether that changes the recommendation is computed. Every
number in the final answer is traceable to a line of Python and a row of input data.

## Treating model input as untrusted

Messages and images are adversarial input, not context. The dataset contains payment-demand
injections aimed at the agent.

- **Rule pre-filter** discards known injection patterns before any model call is made. In
  this dataset it caught exactly two messages — `message_67` ("Pay the release charge today
  to receive the funds…") and `message_142`, the same scam in Indonesian ("Bayar biaya
  pencairan hari ini…"). Both were dropped on pattern match, so **no model ever saw them**,
  which is why the committed cache holds 213 readings rather than 215. The patterns pair a
  payment demand with a release/processing charge, so a message merely mentioning a
  processing fee is not discarded.
- **The extraction prompt frames the message as data**, not instruction: if the text demands
  a payment or tells the reader to ignore rules, the model sets `is_instruction_attempt`,
  quotes the phrase, and returns no facts.
- **The profile is trusted context, the message is not.** The model may use the profile to
  interpret (which currency is home), but every amount and date must come from the message
  text itself — it is never allowed to copy a profile value into its output.
- **Structured output only.** Every call is constrained to a JSON schema; there is no free
  prose path from model output into a financial figure.
- **A model fact still has to survive the engine.** Extraction can only add evidence; the
  safety simulation decides whether it changes anything.

Worth being straight about the consequence: because the pre-filter caught both injections
first, the model-level `is_instruction_attempt` flag never fired on this dataset. It is the
second layer of a defence that the first layer happened to cover completely — tested by unit
tests, not by a real injection reaching the model.

## Every model call is metered

All model access goes through a single gate ([`code/extraction/usage.py`](code/extraction/usage.py))
that logs provider, model, tokens, latency, estimated cost and a purpose tag per call,
persisted per call so a crash cannot lose the accounting.

Full run over 250 requests — [`code/evaluation/usage_report.md`](code/evaluation/usage_report.md):

| calls | input tokens | output tokens | est. cost | cost / request |
|---:|---:|---:|---:|---:|
| 210 | 297,529 | 53,416 | **USD 0.91** | **USD 0.0036** |

Readings are cached per item, keyed on prompt + schema + context + text, so a re-run with
unchanged inputs makes zero calls.

---

## Results

Measured against the 25 labelled sample rows — the only labelled data available. Reproduce
with no credentials:

```bash
python code/evaluation/decision_runner.py     # writes evaluation/decision_predictions.csv
python code/evaluation/main.py --predictions code/evaluation/decision_predictions.csv --gold dataset/sample_requests.csv
```

| column | exact |
|---|---|
| `affordability_status` | **23/25** (92%) |
| `recommended_payment_method` | **23/25** (92%) |
| `payment_plan` | 22/25 (88%) — format valid 25/25, sums correctly 12/12 |
| `spending_changes_needed` | 22/25 (88%) |
| `earliest_date_for_full_payment` | 21/25 (84%) |
| `decision_explanation` | 17/25 (68%) |
| `amount_safe_to_pay` | 3/25 exact, 7/25 within 1%, 17/25 within 10% |
| all 7 columns simultaneously | 4/25 (16%) |

**`amount_safe_to_pay` is the weak column and I am not going to dress it up.** Median error
is 4.9%; four rows are more than 25% off. The cause is diagnosed, not mysterious: variable
essential spending (groceries, transport, dining) is projected per category on its observed
cadence, and that model is too coarse for users whose spending is bursty. Two of the four
big misses are users with no confirmed income at all, where gold's forecast window and mine
disagree.

The number I care about more:

> **0 of 25 rows are more permissive than gold.** No answer the system gives is riskier than
> the labelled correct answer — 23 rows match the status exactly, and the 2 that miss both
> err toward caution. For a system telling someone whether they can afford to spend money,
> being conservatively wrong and being dangerously wrong are not the same failure.

Three amounts are overstated by more than 10% (`request_04` +24%, `request_18` +13%,
`request_20` +57%). Those are the real defects, and they are listed in
[`METHOD.md`](METHOD.md) rather than buried.

## How the accuracy was reached

Before any solution code, two things were built: the **scorer** (11 checks across the 6
graded columns, per-row diff table) and an **all-`not_affordable` floor baseline**. Every
change afterwards was measured against the previous adopted run, and adopted only if it
improved a column without making any row riskier. Rejected changes are recorded with their
numbers alongside the accepted ones — see the adoption log in [`METHOD.md`](METHOD.md).

That rule is why one measurably better variant was **not** shipped: charging recurring bills
at their latest amount rather than their maximum improved five columns, but made one row
riskier than gold and raised overstated amounts from 9 to 13. It survives as a config switch
(`RECURRING_EXPENSE_AMOUNT`) with a test, defaulted off.

---

## Repository layout

```
code/            the solution — see code/README.md for the module-by-module tour
  config/        every tunable in one place; model config from .env only
  common/        formatting, dates, currency — no project logic
  data/          typed records, CSV loading, join validation
  engine/        cash state, 90-day forecast, plans, ranking, safety — pure functions
  extraction/    all model access, behind one metered client
  output/        row building, deterministic guards, CSV writing
  evaluation/    scorer, baseline, runners, provider probe, usage report
  tests/         359 tests, mirroring the folders above
dataset/         input data, unmodified (see Attribution)
  output.csv     HackerRank's BLANK submission template — not a result
output.csv       the generated 250-row result, committed so it is visible without a run
METHOD.md        how the dataset was analysed, what was adopted and rejected, limitations
DATASET_FACTS.md every verified dataset fact as Rule / Evidence / Confidence
DECISIONS.md     the six architectural decisions and the reasoning behind each
```

## Known limitations

Stated plainly, with numbers, in [`METHOD.md`](METHOD.md). The four that matter most:

1. **Variable spending is the dominant error source** in `amount_safe_to_pay` (above).
2. **Plan ranking is unverifiable against the sample.** All 5 labelled installment rows
   leave exactly one eligible option, so no labelled example discriminates between the six
   ranking criteria. It is implemented from the spec text and cannot be tuned.
3. **Plans are not checked for dips after `desired_completion_date`.** This matches the
   labelled data but deviates from the problem statement's 90-day rule — a known, recorded
   deviation, not an oversight.
4. **Some rules are fitted to this dataset.** The income-class map and the message-intent
   list are keyed on literal event descriptions observed in the data. They are isolated in
   `config/` and `extraction/` precisely so the generalization boundary is visible;
   [`METHOD.md`](METHOD.md) says which rules were verified across all 275 users and which
   come from the 25-row sample.

---

## Attribution

The challenge specification (`problem_statement.md`) and everything in `dataset/` are the
property of **HackerRank**, published as part of the HackerRank Orchestrate September 2026
contest. They are included here unmodified so the solution is runnable and the results are
reproducible. All rights to that material remain with HackerRank.

Everything under `code/`, and the analysis documents, are my own work and are MIT licensed —
see [`LICENSE`](LICENSE).
