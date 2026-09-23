# Method

How the dataset was analysed, how the system was built, what was measured, what was
adopted, what was rejected, and what is still wrong with it.

This is the engineering narrative. [`DATASET_FACTS.md`](DATASET_FACTS.md) is the underlying
evidence register (every fact as Rule / Evidence / Confidence);
[`DECISIONS.md`](DECISIONS.md) holds the six architectural decisions.

---

## 1. The task, precisely

For each of 250 requests: reconstruct one user's cash position from their event history,
project it forward 90 days at daily resolution, and answer two separate questions.

- **`amount_safe_to_pay`** — the largest payment on `request_date` that survives the 90-day
  trough, *before* any optional spending changes, capped at `requested_amount`.
- **The plan** — the cheapest safe way to complete the *whole* request by
  `desired_completion_date`.

Those two are easy to conflate and are not the same number. "Safe" means the projected
balance never drops below `minimum_balance_to_keep` at any point in the window, after every
essential expense and every payment in the recommended plan.

Daily resolution rather than monthly is the load-bearing choice. Monthly netting hides the
case where income and obligations balance on paper but a mid-month gap between payroll and
a rent debit breaches the floor. The trough is the answer; a monthly average erases it.

## 2. Shape of the data

Nine CSVs, all joining on explicit keys — `user_id`, `request_id`, `related_event_id`.

| file | rows | notes |
|---|---:|---|
| `financial_events.csv` | 25,342 | 275 users, 56–129 events each |
| `request_payment_options.csv` | 790 | 3–4 options for most requests |
| `financial_profiles.csv` | 275 | one per user |
| `requests.csv` | 250 | the evaluation set |
| `sample_requests.csv` | 25 | the only labelled rows |
| `messages.csv` | 215 | free text, 3+ languages |
| `images.csv` + `media/images/` | 16 | receipts linked to blank-amount events |
| `exchange_rates.csv` | 134 | fixed, dated |

Two consequences drove the architecture:

- **275 independent single-user cases.** Nothing crosses users. There is no corpus to search
  and no similarity problem — the "retrieval" is a dictionary lookup on `user_id`. Hence no
  RAG, no embeddings, no database (see [`DECISIONS.md`](DECISIONS.md) §1).
- **25,342 rows is small.** The whole dataset fits in memory comfortably, so loading is a
  one-shot pass into typed records grouped by user, with `validate_joins` asserting the key
  invariants on load and raising loudly rather than degrading quietly.

## 3. How facts were established

Every rule in [`DATASET_FACTS.md`](DATASET_FACTS.md) carries the check that produced it and
a confidence level, and four early facts were later **disproved and corrected in place**
with a corrections log — the blank-amount currency split, exchange-rate sparsity, language
counts, and the explanation-template count. A register that only ever accumulates
confirmations is not being used honestly.

Two rules kept the register from overstating itself:

1. `confirmed` means checked against the data with zero exceptions found. One instance is
   `probable`, not `confirmed`.
2. Anything derived only from `sample_requests.csv` is derived from **25 of 275 users (9%)**
   and says so in its evidence line.

That distinction is what makes the generalization boundary legible. Rules verified across
all 275 users (exchange-rate coverage, duplicate-charge pairs, join integrity) are
structural. Rules from the 25 labelled rows (explanation wording, the near-floor threshold)
are fitted, and are marked as fitted.

## 4. Measure before building

The first two things built were not the solution.

**The scorer** ([`code/evaluation/main.py`](code/evaluation/main.py)) — 11 checks over the 6
graded columns with a per-row diff table, deliberately not one headline number. Amount
"exact" is numeric equality, so `620.40 == 620.4`; `payment_plan` reports literal-string and
normalized match side by side, so a formatting gap is distinguishable from an arithmetic
one. It also reports reliability: error bands, worst error, amounts overstated by more than
10%, status step-distance from gold, and rows riskier than gold.

**The floor baseline** — answer `not_affordable` to everything:

| check | floor |
|---|---|
| `affordability_status` | 28% (the 7 genuinely-unaffordable rows) |
| `spending_changes_needed` | 88% (the 22 rows whose answer is `none`) |
| `amount_safe_to_pay` | 0% |

88% on `spending_changes_needed` from a baseline that does nothing is the point of building
it. Without that number, the system's own 88% on that column looks like a result.

**The adoption rule that followed:** a change ships only if it improves a column without
making any row riskier than gold. Everything measured is recorded, including what lost.

## 5. Architecture

```
dataset/ ──► data/ ──► engine/ ──► output/ ──► output.csv
   │         (load,     (state, forecast,   (rows, guards)
   │          validate)  plans, safety)
   │                         ▲
   └──► extraction/ ─────────┘
        (messages, images — the only model access)
```

`engine/` is pure functions: no file reads, no model calls, no printing. That is what makes
the financial core testable without credentials — all 359 tests run with no API key present.

`extraction/` is the only place that talks to a provider, and everything goes through one
client with a retry-and-backoff loop, strict JSON-schema responses, and per-call usage
logging. Callers ask for JSON matching a schema and get a parsed dict; they never see the
SDK. Tests inject a fake client.

## 6. Message and image extraction

One model call per message, in a single step that identifies the language, lists every money
amount with its currency, flags instruction attempts, and maps each financial fact onto a
closed intent list. The user's profile is supplied as *trusted* context for interpretation
only — the model is explicitly barred from copying a profile value into its output.

Messages span English, Indonesian and Spanish. A confirmed practical hazard: model numeric
output uses **Indian digit grouping** in some cases, which breaks a naive parser — the
amount parser handles it explicitly.

Images are receipts attached to blank-amount events, read only when an event's amount is
missing. Both caches are keyed on a hash of prompt + schema + context + content, so changing
a prompt invalidates the cache rather than silently serving stale readings.

## 7. Results

Against the 25 labelled rows (see README for the full table): status and method 23/25,
plan 22/25, spending changes 22/25, earliest date 21/25, explanation 17/25, all seven
columns simultaneously 4/25. `amount_safe_to_pay` 3/25 exact, 7/25 within 1%, 17/25 within
10%, median error 4.9%.

Reliability, which matters more than exactness here:

- **rows more permissive than gold: 0.** Status step-distance 0 on 23 rows, 1 on one, 2 on one.
- amounts overstated by >10%: 3 — `request_04` (+24%), `request_18` (+13%), `request_20` (+57%).
- worst amount error: 100% (`request_10`, a no-confirmed-income user).
- rows replaced by a fallback across the full 250-row run: **0**.

## 8. Adoption log — what shipped and what did not

**Adopted, with the measurement that justified it:**

| change | effect |
|---|---|
| Income classification by description class | fixed `request_01`, `request_05`, `request_09` on both amount and date |
| Message facts applied to the forecast | `earliest_date` 28% → 56% → 68% across successive runs |
| Category-cadence variable spend + marginal pay-now rule | replaced a flat daily average; largest single accuracy gain |
| Near-floor safety check under income stress | catches plans that clear the floor by a hair |
| Duplicate-charge rule | structural, verified on 6/6 duplicate pairs (same amount and category) |
| Checking plan feasibility to `desired_completion_date` | status/method/plan 17/17/16 → 23/23/22; see limitation 3 |

**Measured and rejected:**

- **Recurring bills at latest amount instead of maximum (`C1`).** Improved five columns and
  fixed two installment rows — but made `request_21` riskier than gold and raised overstated
  amounts from 9 to 13. It fails the adoption rule. Kept as the config switch
  `RECURRING_EXPENSE_AMOUNT`, defaulted to `max`, with a test.
- **Retuning the 5% near-floor threshold** to rescue `request_21` under C1. One row, one
  parameter — that is overfitting, and it was not done.
- **The profile-category hypothesis** for explaining amount errors: tested, not supported.
- **Case-specific patches** cut on principle after they worked: a single-row income-map
  entry and an image digit hint. The injection pre-filter was deliberately *kept* under the
  same review, because it is a safety net rather than accuracy tuning.

Four hypotheses were tested for why gold's safe amounts differ (bill due on request date
excluded, pending not reserved, scheduled debits not counted, variable purchase on request
date counted). Each helps some rows and breaks others; none is generic, so none was adopted.

## 9. Known limitations

1. **Variable essential spending is the dominant error source.** Projected per category on
   its observed cadence at the category mean. Too coarse for bursty spenders; 4 of 25 rows
   are more than 25% off.
2. **Plan ranking is unverifiable.** All 5 labelled installment rows leave exactly one
   eligible option after `max_installment_months` filtering, so no labelled example
   discriminates "minimize total paid" from "start earlier" from "fewer payments" from
   "lowest `payment_option_id`". Across the evaluation set 30 requests have 4 options and
   180 have 3, so ranking matters there and cannot be tuned.
3. **Plans are not checked for dips after `desired_completion_date`.** Matches the labelled
   data; deviates from the problem statement's 90-day rule. Recorded deliberately — under a
   strict 90-day check, a recommended plan could breach the minimum after the deadline.
4. **One decision archetype has a single labelled example.** `partial_payment` appears once
   (`request_19`); `affordable_with_plan` with spending changes appears three times.
5. **No income staleness rule.** A second household income is 45–48 days stale in 10/10
   users with no terminal row; `request_13` predicts 941.6 against a labelled 433.4. Fixing
   it needs a decision — add staleness for income, or reclassify that description as
   terminal — not a patch.
6. **Vision is validated on 1 image of 16** (`image_02`). The dataset contains both clean
   rendered documents and photographed/stamped receipts; only one style is tested.
7. **Fitted rules.** The income-class map (39 literal event descriptions) and the
   message-intent list (30 intents) are keyed on descriptions observed in this dataset. They
   are isolated in `config/` and `extraction/` so the boundary is visible, but a new dataset
   would need them re-derived.
8. **Sample-tuned parameters remain in place** and are named rather than hidden: the 5%
   near-floor fraction, the marginal pay-now rule, the request-day purchase exclusion, and
   the not-affordable explanation wording rule.
9. **The verifier is off by default.** A second model from a different family reviews a
   finalized decision, but its usefulness probe wrongly rejected 1 of 3 *correct* decisions,
   reproduced twice with identical reasoning — it misread the unaided `earliest_date` field
   as contradicting the plan. A verifier with a 1-in-3 false-reject rate that can overrule
   would corrupt correct answers, so `VERIFIER_ENABLED` defaults to false. Free-tier
   throughput of 5 req/min (~50 minutes for 250 rows) is a second reason.
10. **The model-level injection flag is untested against real injections.** The rule
    pre-filter caught both scam messages in the dataset (`message_67`, `message_142`) before
    any model call, so `is_instruction_attempt` never fired outside unit tests. The layer
    exists and is exercised by tests, but this dataset did not exercise it end to end.
11. **`code/evaluation/main.py` is 647 lines**, past the point where it should split into
    loading, checking and rendering. It is one responsibility, but three separable jobs.

## 10. What I would do next

- Fix the verifier's context so it knows `earliest_date_for_full_payment` is the *unaided*
  date, re-run the usefulness probe, and decide on evidence whether it earns its place —
  including what happens on disagreement, which is a policy question, not a default.
- Replace the category-cadence variable-spend model with a per-user distribution and
  forecast a conservative quantile rather than the mean. That is limitation 1, which is most
  of the remaining amount error.
- Resolve the forecast-window question in limitation 3 against the problem statement rather
  than against the 25 labelled rows.
