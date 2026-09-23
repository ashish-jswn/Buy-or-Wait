# Dataset facts

Every fact here was run against the data, not remembered. Each one carries the evidence
that produced it and an honest confidence level, so the boundary between what is
*structurally true of the dataset* and what is *inferred from 25 labelled rows* stays
visible. [`METHOD.md`](METHOD.md) explains how these were established and what was built on
them; [`DECISIONS.md`](DECISIONS.md) cites them as evidence.

## Entry format

Every entry has exactly three fields:

- **Rule** - what is true, stated so it can be acted on.
- **Evidence** - the specific rows or counts that prove it. A count with no denominator is
  not evidence.
- **Confidence** - one of:
  - `confirmed` - checked against the data, zero exceptions found in what was checked.
  - `probable` - consistent with everything seen, but the sample is small enough that a
    counterexample would not be surprising.
  - `assumption` - believed, not checked, or not checkable against available data.

## How this register is maintained

1. Confidence is downgraded honestly. `confirmed` on 1 instance is `probable`, not
   `confirmed`.
2. A rule derived only from `sample_requests.csv` is derived from 25 of 275 users (9%), and
   its evidence line says so.
3. When a fact is disproved it is **corrected in place**, with a line added to the
   Corrections log at the bottom. No superseded rule is left standing.
4. The check is re-run before any confidence level changes. Evidence must match the current
   data.

Four facts recorded here were later disproved and corrected under rule 3 - see the
Corrections log.

---

## A. Spending changes

### A1. `reduce_to` uses the event's `minimum_allowed_amount` verbatim
- **Rule:** the amount in `reduce_to:<event_id>:<amount>` is that event's
  `minimum_allowed_amount` exactly — not a computed "just enough to make it safe" figure.
- **Evidence:** 2/2 `reduce_to` actions in gold. `request_11` → `event_989`, written
  `665950`, column `665950`. `request_21` → `event_1816`, written `23.50`, column `23.5`.
- **Confidence:** confirmed (only 2 instances exist in the sample).

### A2. The targeted event is the most recent occurrence before `request_date`
- **Rule:** a spending change names the latest event in that recurring series (same user,
  category and description) dated strictly before `request_date` — not the series' first
  occurrence, and not a synthetic id.
- **Evidence:** 4/4 change actions in gold. `request_06`/`event_476` (2025-12-10, request
  2026-01-03, series of 5); `request_11`/`event_989` (2025-04-23, request 2025-05-03, series
  of 2); `request_21`/`event_1815` (2026-03-12) and `event_1816` (2026-03-09), request
  2026-04-03, series of 5 each. In all four, latest-before-request equals latest-overall.
- **Confidence:** confirmed.

### A3. Eligibility is two conditions, and the verb must match the matching list
- **Rule:** an event may be changed only if (a) `flexibility != fixed`, and (b) its
  `category` appears in the list matching the verb — `stop:` requires the category in
  `expense_categories_user_is_willing_to_stop`, `reduce_to:` requires it in
  `expense_categories_user_is_willing_to_reduce`. The verb must also be permitted by the
  flexibility value itself (`stop` needs `stoppable`, `reduce_to` needs `reducible`).
- **Evidence:** 4/4 change actions satisfy all three tests. `request_06` stop/streaming,
  `stoppable`, in stop-list only. `request_11` reduce_to/dining, `reducible`, in reduce-list
  only. `request_21` stop/cloud_storage, `stoppable`, in stop-list only; and
  reduce_to/streaming, `reducible_or_stoppable`, in both lists.
- **Confidence:** confirmed.

### A4. When both verbs are permitted, gold chose `reduce_to`
- **Rule:** where an event is `reducible_or_stoppable` **and** its category appears in both
  the reduce list and the stop list, the gold answer reduced rather than stopped.
- **Evidence:** 1 instance — `request_21`, `event_1816`, streaming, user_21 reduce-list
  `dining|streaming|shopping`, stop-list `streaming|cloud_storage`; gold wrote `reduce_to`.
  This situation is **not rare**: all 225 `reducible_or_stoppable` rows in the full dataset
  have their category in both lists, across 45 users, so the tie-break fires often on the
  eval set while resting on one labelled example.
- **Confidence:** probable. This is the weakest load-bearing rule in this file.

### A5. `minimum_allowed_amount` exists only on reducible-capable events
- **Rule:** the column is populated on exactly `reducible` and `reducible_or_stoppable`
  rows, and is always empty on `fixed` and `stoppable`. A stop needs no floor.
- **Evidence:** 2,682 reducible (2,682 populated) + 225 reducible_or_stoppable (225
  populated) = 2,907 of 25,342. `fixed` 21,138 → 0 populated. `stoppable` 1,297 → 0.
- **Confidence:** confirmed.

### A6. Spending changes appear exactly where capacity arrives after the deadline
- **Rule:** the rows carrying spending changes are exactly the `affordable_with_plan` +
  `full_payment` rows, and in every one `earliest_date_for_full_payment` (the unaided date)
  falls *after* `desired_completion_date`. Cutting spending is what brings an otherwise
  too-late full payment inside the deadline.
- **Evidence:** 3/3 and 3/3 in gold. `request_06` earliest 2026-01-15 vs deadline
  2026-01-14; `request_11` 2025-07-15 vs 2025-06-12; `request_21` 2026-04-15 vs 2026-04-14.
  The other 22 rows have `spending_changes_needed = none`.
- **Confidence:** probable — 3 instances, and the direction of causation is inferred.

---

## B. Installments

### B1. `max_installment_months` gates `number_of_payments`
- **Rule:** an installment option is eligible only when
  `number_of_payments <= max_installment_months`.
- **Evidence:** 5/5 gold installment rows. Caps 7, 12, 11, 3, 6; chosen plans all 3 payments;
  in each case exactly one supplied option was excluded by the cap.
- **Confidence:** confirmed.

### B2. `max_installment_months` is blank for exactly the users who refuse installments
- **Rule:** the field is blank if and only if `installments` is absent from
  `payment_methods_user_will_consider`. It carries no eligibility information beyond the
  method list; it is only a cap.
- **Evidence:** 275/275 profiles agree. 119 blank, 119 not accepting installments, 0
  exceptions in either direction.
- **Confidence:** confirmed.

### B3. An installments plan must reproduce a supplied option exactly
- **Rule:** `payment_amount`, `number_of_payments` and `first_payment_date` must match one
  `payment_option_id`, stepping by that option's `payment_frequency_days`.
- **Evidence:** 5/5 gold installment plans match exactly one option
  (`payment_option_05, 19, 33, 47, 61`).
- **Confidence:** confirmed.

### B4. Installment option arithmetic is internally exact
- **Rule:** for every installment option,
  `payment_amount × number_of_payments == total_payable_amount` and
  `requested_amount + financing_fee == total_payable_amount`. So `total_payable_amount` is
  the ranking cost directly and never needs recomputing.
- **Evidence:** 515/515 installment options, both identities within 0.02.
- **Confidence:** confirmed.

### B5. An installments total is not expected to equal `requested_amount`
- **Rule:** installment plans carry `financing_fee`, so their sum legitimately exceeds the
  requested amount. Never sum-check them against it.
- **Evidence:** `request_02` gold plan is 3 × 15,952,906.67 = 47,858,720.01 against a
  requested 46,018,000. All 5 gold installment plans behave this way.
- **Confidence:** confirmed.

### B6. The `full_payment` option carries no information
- **Rule:** every request has exactly one, and for all 275 its `payment_amount ==
  requested_amount`, `financing_fee == 0`, and `first_payment_date == request_date`. It can
  be synthesised from the request; it never needs to be looked up.
- **Evidence:** 275/275 on all three properties.
- **Confidence:** confirmed.

### B7. No `partial_payment` option is ever supplied
- **Rule:** `request_payment_options.csv` contains only `full_payment` (275) and
  `installments` (515). A partial-payment plan is always constructed, never matched.
- **Evidence:** 790/790 rows, method counts as above.
- **Confidence:** confirmed.

### B8. The six-criteria plan ranking is untested by the sample
- **Rule:** the tie-break order in `problem_statement.md` (complete by deadline → no
  spending changes → minimise total paid → start earlier → fewer payments → lowest
  `payment_option_id`) cannot be validated against any labelled example.
- **Evidence:** in all 5 gold installment rows exactly **one** option survives the cap, so
  "chosen == cheapest" is trivially true and discriminates nothing. On the eval set 30
  requests carry 4 options and 180 carry 3, so ranking decides those.
- **Confidence:** assumption — implement from the spec text, not from the data.

---

## C. Output shape

### C1. Two number-formatting conventions, both exact
- **Rule:** `payment_plan` entries render a whole number bare and anything else with exactly
  two decimals (`25256`, `620.40`, `15952906.67`). `amount_safe_to_pay` and
  `requested_amount` round to 2dp then strip trailing zeros and any trailing dot (`603.3`,
  `433.4`, `25256`). Rounding is half-up, not banker's.
- **Evidence:** 79/79 gold amounts reproduce identically — 29 plan amounts (6 full_payment +
  6 wait at one entry, 1 partial at two, 5 installments at three) and 50 safe/requested
  values. Implemented in `code/formatting.py`, re-checked by `tests/test_formatting.py`.
- **Confidence:** confirmed.

### C2. `wait` carries a plan; only `not_recommended` uses `none`
- **Rule:** `wait` writes a single payment of the **full** `requested_amount` on
  `earliest_date_for_full_payment`. `none` appears only on `not_recommended`.
- **Evidence:** entries per plan by method — wait 6 rows all 1 entry, full_payment 6 rows all
  1, partial_payment 1 row 2 entries, installments 5 rows 3 entries, not_recommended 7 rows
  all `none`. All 6 wait plans equal `{earliest}:{requested_amount}` exactly.
- **Confidence:** confirmed.

### C3. `decision_explanation` is templated, with wording variants
- **Rule:** explanations follow a small set of fixed shapes keyed on the recommendation —
  but there are **at least eight**, not six: six main templates plus two variant wordings.
  Amounts use thousands separators and a currency code; dates are long-form
  (`15 November 2019`).
- **Evidence:** 23/25 gold rows match six regexes (affordable_now 2, installments 5, wait 5,
  not_affordable-A 5, not_affordable-B 2, with-changes 3, partial 1). Two do not:
  `request_04` *"Wait until 15 June 2024, then pay … Paying sooner would put the … minimum at
  risk."* (a `wait` variant) and `request_09` *"Pay EUR 166.61 today. This keeps the EUR 600
  minimum available over the next 90 days."* (an `affordable_now` variant using "keeps …
  available" rather than "leaves at least … available").
- **Confidence:** probable — templated in structure, but the variant set is not closed and
  matching gold wording exactly is not achievable from 25 examples.
- **Which not-affordable wording (added 2026-09-13):** *"Do not proceed with the {req}
  request. Although {safe} is available today, the full amount cannot be completed safely
  within 90 days."* appears exactly when the request allows partial payment, the user
  accepts partial_payment but **not** installments, and safe > 0. Otherwise the wording
  is *"Do not make this payment by {deadline}. None of the available options keeps the
  {min} minimum protected."* Evidence: 7/7 gold not-affordable rows. request_14 and
  request_24 match the first rule. request_10 accepts installments and request_25 does
  not accept partial, so both get the second. request_05, 15 and 20 disallow partial and
  get the second. `output/explanations.py` built from gold decisions reproduces **23/25**
  gold explanations exactly; the misses are the two variants above. Confidence:
  probable (7 rows).

### C4. Hard output invariants
- **Rule:** `0 <= amount_safe_to_pay <= requested_amount`; `affordable_now` implies
  `earliest_date_for_full_payment == request_date`; `not_affordable` implies
  `payment_plan == none` **and** a blank `earliest_date_for_full_payment`.
- **Evidence:** 25/25, 3/3, 7/7 respectively.
- **Confidence:** confirmed.

### C5. Only six (status, method) pairs occur
- **Rule:** `affordable_now`/`full_payment` (3) · `affordable_with_plan`/`full_payment` (3) ·
  `affordable_with_plan`/`installments` (5) · `affordable_with_plan`/`partial_payment` (1) ·
  `affordable_later`/`wait` (6) · `not_affordable`/`not_recommended` (7).
- **Evidence:** all 25 gold rows. No `affordable_later` with a method other than `wait`, and
  no `not_affordable` with a plan.
- **Confidence:** probable for the eval set — 25 rows cannot rule out a rarer pairing.

---

## D. Cash-state facts

### D1. Blank-amount events: 16, and four are non-settled
- **Rule:** 16 events have a blank `amount`, each mapping 1:1 to a row in `images.csv`.
  Currency split is **14 INR / 1 IDR / 1 USD**. **Four are non-settled** and all four are
  debits that must be extracted from the image *and then* reserved in the forecast:
  `event_1442` (scheduled, rent, user_16), `event_6859` (scheduled, healthcare, user_73),
  `event_1786` (pending, utilities, user_20), `event_6033` (pending, groceries, user_64).
- **Evidence:** 16 blank rows; currency counter `{INR: 14, IDR: 1, USD: 1}`; status counter
  `{settled: 12, scheduled: 2, pending: 2}`. The set of 16 `related_event_id` values in
  `images.csv` equals the set of 16 blank-amount `event_id`s exactly. All 16 `.png` files
  exist. 15 of 16 are debits; `event_253` is a settled salary **credit** (IDR, user_03) — the
  one blank-amount inflow.
- **Confidence:** confirmed. *(Corrects an earlier count — see log.)*

### D2. Exchange-rate lookup is exact-match and must raise on a miss
- **Rule:** build the rate lookup as an exact `(settlement_date, from_currency,
  to_currency)` match that **raises** when a row is absent. No nearest-prior-date fallback,
  no inverting the reverse pair. The raise is the canary if this ever stops being true.
- **Evidence:** 140 events carry a currency different from the user's `home_currency` — 139
  credits and 1 debit, all cash, 132 settled and 8 scheduled, none with a blank
  `settlement_date`. **All 140 have an exact matching rate row. Zero misses.** The table's
  apparent sparsity (five one-directional pairs, mostly on the 15th, plus a single
  `2025-10-01` row) is irrelevant: the rows that exist are exactly the rows needed. Each pair
  is a single constant — `EUR→ZAR 20`, `USD→EUR 0.92`, `USD→IDR 15833.33`, `USD→INR 83.33`,
  `EUR→USD 1.09`.
- **Confidence:** confirmed. *(Corrects an earlier "sparse, needs a fallback rule" note —
  see log.)*

### D3. Salary cadence is per-user and must be detected
- **Rule:** do not assume payday is the 15th. Detect each user's cadence from their history.
- **Evidence:** across 1,690 salary rows the settlement day-of-month is 15th ×1,154, but also
  8th ×90, 20th ×87, 5th ×53, 12th ×50, 19th ×50, 24th ×50, 26th ×50, 22nd ×45, 21st ×25,
  and 4th/7th/11th/18th/23rd/25th/31st in smaller numbers. 1,643 settled, 47 scheduled.
- **Confidence:** confirmed.

### D4. Status distribution and what each means for cash
- **Rule:** reserve `pending` debits; never count `pending` credits, `unrealized` value,
  `cancelled` or `failed` rows as cash; count `scheduled` on its settlement date.
- **Evidence:** 25,342 rows — settled 25,148 · pending 71 · scheduled 70 · cancelled 22 ·
  failed 21 · unrealized 10. Directions: debit 23,609 · credit 1,723 · non_cash 10.
  `user_20` is the worked example: settled purchase `event_1784` (8,640) with its **pending**
  refund `event_1785` (8,640, must not count), plus pending debits `event_1786` and
  `event_1787`.
- **Confidence:** confirmed (counts); the treatment rules are from `problem_statement.md`.

---

## E. Structure and content

### E1. The problem is 275 independent single-user cases
- **Rule:** one user asks exactly one question. No cross-user or cross-request state. Every
  join is an explicit key, so no database and no similarity search is needed.
- **Evidence:** 250 eval requests → 250 distinct users (`user_26`…`user_275`); 25 sample
  requests → 25 distinct users (`user_01`…`user_25`); overlap 0; 250 + 25 = 275 = profile
  rows = distinct users in `financial_events.csv`. Every request user has both a profile and
  events. Events per user 56–129.
- **Confidence:** confirmed.

### E2. Languages
- **Rule:** `request_text` is **273 English, 1 Indonesian (`request_43`), 1 Spanish
  (`request_117`)** — two non-English requests, not one. `message_text` is **170 English,
  45 Indonesian**; no Spanish appears in messages.
- **Evidence:** stopword classification over all 275 requests and all 215 messages, spot
  checked. `request_43` opens *"Laptop yang saya incar harganya IDR 43.339.000…"*;
  `request_117` opens *"El depósito de alquiler es de EUR 880…"*.
- **Confidence:** confirmed.

### E3. Messages are one-per-user and often key-less
- **Rule:** at most one message per user. 76 have neither `request_id` nor
  `related_event_id` and must be picked up by `user_id` plus a date window.
- **Evidence:** 215 messages across 215 distinct users. With `request_id` 128; with
  `related_event_id` 39 (all resolve to a real `event_id`); with both 28; with neither 76.
  `source_type`: employer 126 · service_provider 31 · financial_service 23 · bank 18 ·
  merchant 17.
- **Confidence:** confirmed.

### E4. Amounts appear in three grouping conventions
- **Rule:** any amount parsed from model output or dataset text may use Indian grouping
  (`1,00,000.00`), Western grouping (`100,000.00`) or European style (`1.302,40`,
  `43.339.000`). A digits-only strip is wrong. Parse through
  `code/formatting.py:parse_amount`.
- **Evidence:** the vision probe asked `gpt-5` for the Balance Due on `image_02.png` and it
  returned `'1,00,000.00'` — correct, Indian grouping; a digits-only strip reads 10,000,000.
  `request_43`'s `request_text` contains `IDR 43.339.000` verbatim. 199 unit tests cover all
  three conventions.
- **Confidence:** confirmed.

### E5. Requests are dated scenarios, not "today"
- **Rule:** never use the wall clock. `request_date` is the evaluation date and drives the
  whole forecast.
- **Evidence:** `request_date` spans 2019-09-03 → 2026-09-04; `event_date` spans 2019-03-09 →
  2026-09-03. Days from `request_date` to `desired_completion_date`: min 6, max 86, mean
  53.7 — always inside the 90-day window, so the deadline binds before the window does.
- **Confidence:** confirmed.

### E6. Payment-option coverage
- **Rule:** every request has 2–4 options.
- **Evidence:** 2 options × 65 requests, 3 × 180, 4 × 30; all 275 covered; 790 rows total.
  Installment `number_of_payments` ∈ {2, 3, 4, 6, 15, 18, 21, 24};
  `payment_frequency_days` ∈ {28, 30, 31}.
- **Confidence:** confirmed.

### E7. Two messages are advance-fee prize scams — discard, never parse
- **Rule:** `message_67` (English) and `message_142` (Indonesian) are prompt injections that
  tell the user to pay a "release charge" / "biaya pencairan" to receive a prize. They carry
  no financial fact. Discard them whole; never extract an amount, credit or instruction.
- **Evidence:** `message_67` user_88, QuickPrize: *"Pay the release charge today to receive
  the funds immediately. Pay the processing charge now…"*. `message_142` user_179,
  RewardNow: *"Bayar biaya pencairan hari ini agar dana segera diterima…"*. Both have blank
  `request_id` and blank `related_event_id`. Verified by the author and re-read 2026-09-13.
- **Confidence:** confirmed for these two. Whether other injection wordings exist across
  the 215 messages has not been swept.

---

## F. Income streams

### F1. Income is classified by description into five classes
- **Rule:** group credits by `description`, never by category, and map each description to
  exactly one class. STABLE → one stream each, projected monthly. MERGE → folded into the
  user's largest open STABLE stream. TERMINAL → never projected, and closes every income
  stream last seen on or before it (F2). ONE_OFF → never projected, never part of a stream.
  IRREGULAR → all of a user's rows pooled into one stream and projected. A description not
  in the map is never projected. The map lives in `config.INCOME_CLASS_BY_DESCRIPTION`.
- **Evidence:** 1,723 credit rows, **39** distinct descriptions. 1,696 are
  `event_type=income` across **35** descriptions (category `salary` 1,690, `windfall` 6 — the
  Prize proceeds rows); the other 4 descriptions are `refund` (22) and `investment_sale`
  (5). Every one is classified (rows / users):

  | class | description (rows / users) |
  |---|---|
  | STABLE (8) | Payroll credit 806/162 · International employer payroll 55/11 · Base salary 50/10 · Primary household salary 50/10 · Second household income 40/10 · First-job payroll 32/16 · New employer payroll 11/11 · Payroll after returning from leave 9/9 |
  | MERGE (1) | Next confirmed salary 47/47 |
  | TERMINAL (6) | Previous employer payroll 44/11 · Payroll before leave 18/9 · Temporary assignment pay 10/7 · Peak-season wages 9/5 · Seasonal contract payment 8/5 · Final employer payroll 7/7 |
  | ONE_OFF (9) | Promotion arrears payment 12/12 · Quarterly performance bonus 10/10 · Pending merchant refund 8/8 · Prorated first salary 7/7 · Employer expense reimbursement 7/7 · Settled card charge reversal 7/7 · Prize proceeds 6/6 · Investment sale proceeds 5/5 · August 2019 net salary 1/1 |
  | IRREGULAR (15) | Delivery platform payout 64/11 · Driver platform payout 64/11 · Weekly app earnings 49/11 · Task marketplace payout 47/11 · Website project payment 29/17 · Content contract payment 28/16 · Freelance milestone payment 26/14 · Consulting invoice payment 26/15 · Independent work payment 25/17 · Application project payment 22/14 · Client retainer payment 20/14 · Account commission payment 19/9 · Performance commission 18/9 · Design contract payment 14/9 · Monthly sales commission 13/8 |

  IRREGULAR covers 40 users; 29 have no other income, 10 also have Base salary. user_09 has
  8 distinct project descriptions over 10 rows.
- **Flagged — placement not confident:**
  - *Second household income* (STABLE by name). In 10/10 users its last row is **45–48 days**
    before `request_date` with no terminal row after it, and its amount varies in 10/10.
    It looks ended, but nothing in the map says so. **Now handled** (author decision): an
    income stream silent for more than `INCOME_STALE_DAYS` = 40 days has ended. Every
    other open stable stream's last row is 5–23 days before the request. 7 of the 10 users
    also have a `remaining_household_salary` message (G2). `request_13` went from 941.6 to
    381.87 (gold 433.4).
  - *August 2019 net salary* (ONE_OFF). One row, user_03, `event_253`, blank amount (image).
    Dated 2019-08-31, after that month's Payroll credit on 08-15 of 4,365,000. It reads as
    a payslip restating August pay, not extra income. It is history either way, so it
    does not move the forecast.
  - *Commission descriptions* (IRREGULAR, projected by default). 9 of 10 commission users
    also have Base salary, and user_11's message says commission is pending. Projecting it
    is the default until messages are read.
  - *Pending merchant refund* and *Settled card charge reversal* are not income. ONE_OFF only
    keeps them out of streams; the pending rows are already excluded by status (D4).
- **Confidence:** confirmed for coverage and counts (39/39). The class per description is
  inferred from names plus the per-user patterns in F2–F5. Confidence is `probable` for the
  flagged four, and for IRREGULAR being projected at all.

### F2. A terminal row ends the streams before it
- **Rule:** a TERMINAL row closes every income stream whose last occurrence is on or before
  it. Refusing to project the terminal row alone changes nothing: it almost always has one
  row, and one row never forms a series.
- **Evidence:** **Final employer payroll** 7/7 users: Payroll credit ×4 → Final employer
  payroll ×1, last income 19–22 days before request. user_05 is this shape (Payroll credit
  14,740 ZAR 2025-06-15…09-15, Final 2025-10-15, request 2025-11-06). Gold 737; the old
  engine predicted 15,488 by projecting Payroll credit, and now predicts 0.
  **Previous employer payroll** 11/11: ×4 → New employer payroll ×1 (4 of them then Next
  confirmed salary). **Payroll before leave** 9/9: ×2 → Payroll after returning from leave ×1
  (1 then Next confirmed salary). **Seasonal / Peak-season / Temporary assignment**: 9 users,
  whose only income is those three descriptions, last row 78–84 days before request.
- **Confidence:** confirmed.

### F3. Next confirmed salary is the next occurrence of an existing stream
- **Rule:** merge it into the user's largest open STABLE stream (most rows, then most
  recent) before projecting. If no stable stream exists, it starts its own.
- **Evidence:** 47 rows, all `scheduled`, one per user, 47/47 dated on or after
  `request_date` (verified by the author and re-run). 47/47 land on the same day-of-month as
  the stream they join. Merge targets: Payroll credit 31 · International employer payroll 4
  · New employer payroll 4 · Primary household salary 3 · First-job payroll 2 · Base salary 1 ·
  Payroll after returning from leave 1 · none 1 (user_01, whose only prior income is Prorated
  first salary). The amount matches the target's latest row in 45/47. The two exceptions
  are user_01 (12,826 prorated → 23,320) and user_99 (44,444.4 → 56,980).
- **Confidence:** confirmed.

### F4. Stable streams project their latest amount, not their largest
- **Rule:** a STABLE stream recurs at its most recent observed amount, including a merged
  Next confirmed salary.
- **Evidence:** amounts vary within a stable stream for 33 users. For **23** it is Payroll
  credit, and every one is a single step **down** (user_06 1,441 ×3 → 1,037.52 ×2; user_08
  1,422.85 ×4 → 782.57). The other 10 are Second household income, which is noisy (F1).
  The old engine projected the maximum, overstating income for all 23.
  **Counter-evidence:** user_08's `message_06` says *"Your next salary is reduced to EUR
  1422.85… due to approved unpaid leave"*, and gold is `wait` until 2025-04-15. So at least
  that step-down is temporary. With latest-amount and no message handling, `request_08`
  went from 306.22 to 0 (gold 284.57).
- **Confidence:** probable — the latest is the safer reading of an unexplained step; messages
  can overturn it per user.

### F5. A moved payday shows in the latest row; monthly streams use the calendar day-of-month
- **Rule:** anchor a STABLE stream's day-of-month on its latest occurrence. Project monthly
  streams on that calendar day (`monthly_occurrences`), never last date + 30. Stable streams
  need no gap-consistency test: one observation is enough.
- **Evidence:** across the six stable descriptions with multi-row users, **806 of 814** gaps
  are 28–31 days. The 8 exceptions (38/39 days) are 8 users whose fifth Payroll credit
  landed on the **23rd** after four on the 15th: user_07, 68, 86, 95, 131, 167, 194, 212.
  user_07's gold `earliest_date_for_full_payment` is 2024-10-23. The old engine rejected
  those 8 series outright on the 4-day gap tolerance, so it projected no salary: request_07
  predicted 0 and a blank date, and now predicts 84,274.29 and 2024-10-23 (gold 87,170.56,
  2024-10-23). Calendar anchoring was already how `project_recurring` worked before this
  change; only the anchor day and the gap test changed for income.
- **Confidence:** confirmed (8/8 same shape, one gold row confirms the date).

---

## G. Messages

### G1. All 215 messages fall into 30 fixed templates plus one injection template
- **Rule:** every message is one of the intents in `config.MESSAGE_INTENTS`, written in a
  fixed English or Indonesian wording with varied greeting and reference boilerplate. The
  model extracts into that closed list. `code/evaluation/message_oracle.py` is an
  independent keyword reading of the same templates, used only to score the extractor.
  One message carries two facts: `message_86` has a receipt for an EV-charging payment and
  a confirmed USD 1,296 salary for 2026-09-15. A reading must therefore be a list of facts.
- **Evidence:** all 215 messages were read by hand on 2026-09-13, then matched by the
  oracle: **215/215 matched, 0 unmatched**, and one message (`message_86`) matches two
  templates. Counts:
  first_salary 27 · invoice_approved 15 · temporary_pay 10 · next_salary_reduced 10 ·
  salary_increase 9 · payout_pending 9 · base_salary_commission_pending 9 ·
  seasonal_contract_ended 9 · bonus_pending 8 · salary_resumes_with_childcare 8 ·
  salary_next_with_arrears 8 · payday_moved 7 · rent_increase_pct 7 (all 12%) ·
  refund_pending 7 · investment_value_unrealized 7 · remaining_household_salary 7 ·
  fx_salary_confirmed 7 · internal_transfer 6 · prize_proceeds_settled 6 ·
  fx_refund_processing 6 · dispute_open 6 · prize_claim_processing 4 · employment_ended 4 ·
  failed_debit_retry 4 · receipt_has_amount 3 · investment_sale_settled 3 ·
  reimbursement_closed 3 · fx_bill_pending 3 · injection 2 · two_card_minimums 2 ·
  salary_next_confirmed_no_amount 1. Language 170 English / 45 Indonesian (agrees with E2).
  There are 118 explicit amounts, each written as a currency code and plain number with no
  thousands separators (EUR 35, INR 25, IDR 22, USD 21, ZAR 15).
  The first oracle pass read `message_92` (investment sale settled, Indonesian) as also
  "payout pending", because "masih tertunda" appears in *"tidak ada hasil penjualan yang
  masih tertunda"*. That was an oracle bug, fixed by anchoring on "pembayaran berikutnya
  dari … masih tertunda".
- **Confidence:** confirmed for this dataset. The research spec's "214/215 coverage" is
  superseded: it is 215/215.

### G2. What each cash-moving intent lines up with in the events
- **Rule:** how a message fact relates to the user's own rows, per intent. This is the
  evidence for how facts get applied; the application rules themselves are pending the
  author's decisions.
- **Evidence:** readings are the model's (cached); rows are from `financial_events.csv`.
  - *temporary_pay* **10/10**: the message amount equals the user's **latest** income level,
    reached after exactly one step down (user_06 1,441 → 1,037.52; message 1,037.52).
  - *next_salary_reduced* **10/10**: the message amount equals the **earlier, higher**
    level. The latest row is already the reduced one at 55% of it (user_08 1,422.85 →
    782.57; message 1,422.85). Gold `request_08` (`wait` until 2025-04-15, safe 284.57) is
    consistent with the next salary being the message amount. **So F4's latest-amount
    rule is wrong for these 10 users.**
  - *payday_moved* **7/7**: the message date falls on the same day as the latest payroll
    row (the 23rd). F5 already anchors there.
  - *remaining_household_salary* **7/7**: these users have Primary household salary ×5 and
    Second household income ×4. The message amount is **1.6129 × the latest Primary row**
    in all 7 (user_42 148,000 vs 91,760). It is not the sum of the two streams.
  - *base_salary_commission_pending* **9/9**: the message amount is **1.6667 × the latest
    Base salary row** in all 9 (user_11 38,760,000 vs 23,256,000). All 9 also have
    commission rows.
  - *salary_increase* 9/9 · *first_salary* 27/27 · *fx_salary_confirmed* 7/7 ·
    *invoice_approved* 15/15 · *salary_resumes_with_childcare* 8/8: the effective date is on or
    after `request_date`, and **no event row exists on that date** in any of the 66, so
    applying them cannot double-count a scheduled row. first_salary equals the latest
    income in 21/27. salary_resumes equals the latest income in 8/8. An exact rate row
    exists for all 7 fx_salary_confirmed dates. The childcare payment's amount is never
    stated. *salary_next_with_arrears* 8/8 give no date.
  - *rent_increase_pct* **7/7**: always 12%, and every user has a monthly rent series last
    paid before the request.
  - *internal_transfer* **6/6**: in 5 users no equal-amount debit/credit pair within 3 days
    exists at all. user_261's only match is a card charge and its reversal. **Nothing in
    the events can be netted off.**
  - *employment_ended* 4/4 users already have Final employer payroll, and
    *seasonal_contract_ended* 9/9 users have only terminal income. F2 already removes their
    income.
  - Extraction quality, single-call version: **215/215** against the oracle on language,
    intents, amounts, dates and the injection flag (213 calls, 2 dropped by rule).
    Two-step version (the author's design: identify language, then analyse in it):
    language, amounts, dates and injection **215/215**, intents **214/215**. The one
    disagreement is `message_02` (Indonesian): *"Gaji rutin untuk penggajian berikutnya
    sudah dikonfirmasi. Slip gaji berikutnya akan menampilkan gaji rutin dan penyesuaian
    satu kali secara terpisah."* The model chose `salary_next_with_arrears`; the oracle
    says `salary_next_confirmed_no_amount`. The message mentions a one-time adjustment
    but states no amount, so neither label carries a number. 426 calls, 0 failed.
- **Confidence:** confirmed (counts). The 1.6129 and 1.6667 ratios look like generator
  constants; they are recorded, not relied on.

---

## H. Images

### H1. Three image amounts, settled by viewing the image
- **Rule:** the amounts below are what the documents show, as read by the author's agent
  viewing each PNG on 2026-09-13. They are verification evidence only; the pipeline reads
  images with the model and never uses these values.
- **Evidence:**
  - `image_02` → `event_1442` (Outstanding rent balance, scheduled, user_16). The rent
    receipt shows Total Amount to be Received 2,00,000.00, Amount Received 1,00,000.00, and
    **Balance Due 1,00,000.00**, so the amount is **100,000**. It contains no instruction
    attempt, only printed notes ("PAN of Owner not mandatory…", "Revenue stamp necessary…").
    The first model run chose 100,000 correctly but set `is_instruction_attempt` true, a
    false positive. By the author's rule that discards the amount, which would have
    replaced request_16's row.
  - `image_07` → `event_3231` (Restaurant tax invoice, settled, user_35). It shows SubTotal
    8,122.00, SGST and CGST 203.05 each, **Total 8,528.10**, and **Grand Total (RS) 8528**,
    stamped PAID. Both readings are defensible. The model's 8,528 is the rounded amount
    paid; the research spec claimed 8,528.10. The difference is 0.10 on a settled event,
    with no forecast effect.
  - `image_14` → `event_9421` (Pharmacy purchase, settled, user_101). The handwritten TOTAL
    is **4543**, with items 1500 + 724 + 796 + 550 + 303 + 670 = 4543. The first model run
    read 4593 and item 794, a misread of two handwritten digits. The research claim (4543)
    is correct.
  - First model run over all 16 images: 13/16 resolved amounts agree with the research
    claims. All 16 were identified as English.
  - Second model run, after tightening the prompt (instruction attempts defined; re-read
    handwriting until items reconcile): **14/16** agree with the research claims, 0
    instruction flags. `event_1442` now resolves to 100,000. `event_3231` keeps 8,528
    (defensible, see above). `event_9421` is still **4593** at medium confidence. The model
    made the items "reconcile" by misreading 724 as 794 and 550 as 530 (1500 + 794 + 796 +
    530 + 303 + 670 = 4593). This is a measured model error on handwriting. It is a
    settled one-off purchase, so it does not affect the forecast.
- **Confidence:** confirmed for the three values viewed.

---

## I. Variable spending

### I1. Variable spending runs on one exact gap per category; descriptions are only labels
- **Rule:** for every user, each variable category (groceries, transport, dining) is one
  purchase stream with a single fixed gap. The descriptions ("Bulk pantry shop", "Grocery
  delivery", …) are labels drawn at random onto those purchases, so grouping by
  description destroys the cadence. Amounts scatter around the stream's mean. The next
  purchase is always due within one gap of request_date.
- **Evidence:** 800 (user, category) series of settled variable debits before request_date,
  9–36 rows each. **800/800 have every gap identical.**
  - Groceries: 7 days (124), 10 (131), 14 (20).
  - Transport: 5 (46), 7 (99), 14 (68), 21 (62).
  - Dining: 7 (46), 14 (118), 21 (86).
  - Amount ÷ series mean over 14,895 purchases: min 0.650, p1 0.718, p50 1.000, p99 1.298,
    max 1.432. This is consistent with the claim "base × Uniform(0.74, 1.26)" when the
    base is estimated by the sample mean.
  - Next purchase due within one gap of the request: 800/800.
  - On the 25 samples (scratch, before the safety check), safe amounts measured against
    gold, request-day purchase not projected:

    | model | within 1% | within 5% | median error | overstated |
    |---|---|---|---|---|
    | flat daily (90d) | 5 | 10 | 7.0% | 11 |
    | per-description cadence | 2 | 6 | 40% | — |
    | **per-category cadence, mean** | **7** | **13** | **4.9%** | **9** |

    Projecting a purchase due on request_date turns request_15 from 26% into 99% error
    and request_25 from 17% into 90%, so it is excluded. The horizon end (89 vs 90 days)
    makes no difference.
- **Confidence:** confirmed for the cadence structure (800/800). The request-day exclusion
  is probable (measured on 25 samples).

---

## Corrections log

Append here whenever a fact above is changed. Never delete the superseded statement —
record what it was and why it was wrong.

### 2026-09-13 — file created, seeded from verified checks
Every rule above was re-run against the dataset on this date before being written down.

- **D1 blank-amount events.** Previously recorded as "13 INR, 1 USD" with
  "two are non-settled". Both wrong: the split is **14 INR / 1 IDR / 1 USD** (the IDR row,
  `event_253`, had been missed entirely), and **four** are non-settled — `event_6859` had
  been omitted alongside `event_1442`, `event_1786` and `event_6033`.
- **D2 exchange rates.** Previously flagged as "sparse; how to resolve a missing (date, pair)
  is an open decision". Wrong: all 140 foreign-currency events have an exact rate row, zero
  misses. Exact-match-and-raise is now the rule, and the open question is withdrawn.
- **E2 languages.** Two separate errors corrected. An earlier note recorded messages as
  English + Indonesian without counts (now 170/45), and a later note called `request_117`
  "the only non-English request" — it is the only **Spanish** one, but `request_43` is
  Indonesian, so there are two.
- **C3 explanation templates.** Previously recorded as "six shapes". Measured at **23/25**
  against six regexes; `request_04` and `request_09` use variant wordings. Downgraded to
  `probable` and restated as "at least eight".
- **A4 both-lists tie-break.** Recorded at `probable` on 1 instance, with the note that the
  situation occurs on all 225 `reducible_or_stoppable` rows across 45 users — so it is
  load-bearing on the eval set despite the single example.

### 2026-09-13 — income streams (section F added, E7 added)
- **Income description count.** The instruction for this change said 34 income descriptions.
  The data has **35** `event_type=income` descriptions and **39** credit descriptions in all;
  two of the named ONE-OFFs (Investment sale proceeds, Employer expense reimbursement) are
  `investment_sale` / `refund` rows, not income. The map covers all 39.
- **Income recurrence key.** An earlier instruction to key income on (user_id, category,
  direction) was retracted by the author, because it merges terminal income into ongoing
  streams. For the record, the code keyed on (user, category, description) already. The
  defects were the ≥3-occurrence and 4-day-gap tests, the dominant day-of-month, and the
  maximum amount. F2, F4 and F5 give what each cost. Income now goes through
  `engine/income.py`, and `detect_recurring` handles debits only.
- **D3 is unaffected.** Its salary day-of-month counts describe settlement days, not the
  grouping key.

### 2026-09-13 — messages applied (G1, G2 added; F1 and F4 updated)
- **F1 Second household income:** "not handled, no staleness rule" is superseded. The
  author decided an income stream silent for more than 40 days has ended.
- **F4 latest amount:** still the default, but G2 shows it is wrong for the 10
  `next_salary_reduced` users. Their message restores the earlier level, and
  `engine/evidence.py` now applies it (request_08 went from 0 to 306.22, gold 284.57).

### 2026-09-13 — duplicates, single-case cuts, one-step messages (G2, H1, F1 updated by this entry)
- **Possible duplicate card charges.** Previously the pending duplicate was reserved as a
  real debit and the settled original dropped. Now a pending debit linked to a settled debit
  with the same amount and category is dropped. Evidence: in all 275 users exactly 6
  pending-debit children of settled-debit parents exist, all "Original card charge" →
  "Possible duplicate card charge", 6/6 same amount and same category. The rule is
  structural and touches nothing else.
- **F1 map.** "August 2019 net salary" (1 row, 1 user) was removed from the class map. It
  falls to the unknown default (never projected), with identical behaviour. The map now
  holds 38 of 39 credit descriptions.
- **G2 extraction.** At the author's request, messages are read in **one** call with the
  user's profile as trusted context: language first, then amounts, flag and facts. The
  two-step version is superseded. Scored against the oracle: language, amounts, dates and
  injection 215/215, intents 214/215 (the same `message_02` no-amount disagreement).
- **H1 images.** The handwriting digit hint (written for `image_14`) was removed from the
  image prompt. Third run: **15/16** agree with the research claims. `event_9421` now reads
  4,543 (correct); only `event_3231` (8,528 vs 8,528.10, defensible) differs.
