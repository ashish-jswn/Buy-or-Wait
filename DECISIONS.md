# Architectural decisions

Six decisions that shaped the system, each with the evidence behind it and the condition
that would make me change my mind. Supporting facts are cited from
[`DATASET_FACTS.md`](DATASET_FACTS.md); measurements from [`METHOD.md`](METHOD.md).

---

### 1. No RAG, no embeddings, no database

**Decision.** Join everything on explicit keys (`user_id`, `request_id`,
`related_event_id`) and hold it in plain in-memory dicts grouped by user.

**Evidence.** `DATASET_FACTS.md` E1 — 275 independent single-user cases, 56–129 events each;
`financial_events.csv` is 25,342 rows total. E3 — the 76 key-less messages still carry
`user_id`, so relevance is a filter, not a similarity search.

**Why.** Every file joins on a key I can see, so there is nothing to search for. And 25k
rows is small enough to just hold in memory. Adding a vector store here would be
architecture for its own sake: it would make retrieval approximate where it is currently
exact, and add a failure mode the problem does not have.

**Where.** [`code/data/loader.py`](code/data/loader.py)

**Would revisit if.** Users stopped being independent — any rule needing evidence across
users, or a dataset large enough that a full load stops fitting in memory.

---

### 2. Deterministic engine, narrow model role

**Decision.** The 90-day forecast, plan generation, ranking and validation are plain Python.
The model only reads messages and images for facts, and nothing else.

**Evidence.** `problem_statement.md` states the ranking order, conflict-resolution order and
plan format exhaustively. There is no judgment left to delegate.

**Why.** The problem statement already gives the exact ranking and conflict rules. Handing
that to a model would only add randomness to something I can compute — and would make every
answer unverifiable. This is financial advice; a number I cannot trace to a line of code and
a row of data is not one I would ship.

**Where.** [`code/engine/forecast.py`](code/engine/forecast.py),
[`code/engine/plans.py`](code/engine/plans.py)

**Would revisit if.** The task added a genuinely open judgment — ambiguous user priorities,
or a recommendation that has to weigh preferences the data does not encode.

---

### 3. Exchange rates: exact match, raise on a miss

**Decision.** Look up `(settlement_date, from_currency, to_currency)` exactly. If the row is
absent, raise. No nearest-prior-date fallback, no inverting the reverse pair.

**Evidence.** `DATASET_FACTS.md` D2 — all 140 foreign-currency events have an exact matching
rate row. Zero misses.

**Why.** I checked all 140 FX events and every rate exists. A fallback would only paper over
a bug if that ever changed — it would silently convert at the wrong rate instead of telling
me the data assumption broke.

**Where.** [`code/common/currency.py`](code/common/currency.py)

**Would revisit if.** Rates became live or sparse, at which point the fallback policy
becomes a real decision with its own evidence rather than a way to avoid crashing.

---

### 4. `reduce_to` reads `minimum_allowed_amount`, it is not computed

**Decision.** The amount in `reduce_to:<event_id>:<amount>` is copied from the event's
`minimum_allowed_amount` column. Never calculate a "just enough to make it safe" figure.

**Evidence.** `DATASET_FACTS.md` A1 — 2/2 labelled rows. `event_1816` → 23.5 → written
`23.50`; `event_989` → 665950.0 → written `665950`.

**Why.** I nearly computed this. Reading the labelled rows showed it is just a column I had
overlooked — the optimisation I was about to write would have produced a different, wrong
number on every row.

**Where.** [`code/engine/spending.py`](code/engine/spending.py)

**Would revisit if.** A labelled row ever showed a `reduce_to` amount that is not the
column value. Confidence here rests on 2 instances, which is thin.

---

### 5. Model verifier built, measured, then disabled by default

**Decision.** Every output row passes a deterministic validation function. The model
verifier stays in the codebase behind `VERIFIER_ENABLED`, default off.

**Evidence.** Measured in [`code/evaluation/probe.py`](code/evaluation/probe.py) check 6.
The verifier caught 3/3 corrupted rows but wrongly rejected 1/3 correct rows — twice, with
identical reasoning. At 5 requests per minute it costs ~50 minutes on 250 rows. All three
errors it caught are structural and are caught instantly by the deterministic checks with
no false positives.

**Why.** I built it, measured it, and it wrongly rejected a correct row 1 time in 3. My
deterministic checks caught the same errors instantly and for free, so the model was costing
50 minutes to add noise. Shipping it switched on would have been adding an LLM to the
pipeline because it looks impressive, not because it works.

**Where.** [`code/output/guards.py`](code/output/guards.py), flag in
[`code/config/settings.py`](code/config/settings.py)

**Would revisit if.** The false-reject cause is fixed — the prompt never told it that
`earliest_date_for_full_payment` is the *unaided* date, which is exactly what it tripped on.
That fix is worth testing, and then the disagreement policy needs deciding on evidence.

---

### 6. Explanations are templated in code, not written by the model

**Decision.** Generate `decision_explanation` from fixed templates keyed on the
recommendation. No model call.

**Evidence.** `DATASET_FACTS.md` C3 — 23/25 labelled rows match six regexes; the other two
are minor wording variants. Amounts use thousands separators and a currency code; dates are
long-form (`15 November 2019`).

**Why.** The labelled explanations follow fixed shapes. Code matches a shape more reliably
than a model writes prose — and it costs nothing per row. Currently 17/25 exact, and the
misses are wording, not wrong reasoning.

**Where.** [`code/output/explanations.py`](code/output/explanations.py)

**Would revisit if.** Explanations needed to be genuinely personalised rather than
schematic, at which point a model writing prose over computed facts is the right shape —
with the facts still computed.

---

## Open questions

Decisions that need evidence I do not have, rather than defaults I have quietly picked.

- **Both-verbs tie-break.** When an event is `reducible_or_stoppable` and its category is in
  both lists, the labelled data chose `reduce_to` — but that rests on one example
  (`DATASET_FACTS.md` A4) and fires on all 225 such rows.
- **Which flexible event to change** when several are eligible, and how to guarantee `stop:`
  and `reduce_to:` never target the same event.
- **Plan ranking order**, which the 25 samples cannot validate (`DATASET_FACTS.md` B8).
  Implemented from the spec text and explicitly untested — see `METHOD.md` limitation 2.
- **Verifier disagreement policy** — keep the primary silently, force a re-decision, or flag
  it in the explanation. This is a policy choice and should not be left as a default.
- **Forecast window for plan feasibility** — 90 days per the problem statement, or to
  `desired_completion_date` as the labelled data implies. See `METHOD.md` limitation 3.
