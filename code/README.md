# Buy or Wait? — solution

An AI-powered financial agent that decides, for each request in `dataset/requests.csv`,
whether the user should pay in full, pay part now, use an installment option, wait, or not
proceed — from a 90-day forward simulation of their balance.

> This is the module-by-module tour. For the design rationale, results and known
> limitations, start at the [repository README](../README.md).

## Layout

Everything lives under `code/`. Only `README.md`, `requirements.txt` and the entry point
`main.py` sit at the top level; every other module is in a folder with one responsibility.

| folder | responsibility |
|---|---|
| `config/` | Every tunable in one place: forecast horizon, thresholds, retry counts, file paths, and the mapping from `.env` variable to model role. Nothing is re-typed as a literal elsewhere. |
| `common/` | Shared primitives with no project logic: number formatting and parsing, date helpers, currency conversion. |
| `data/` | Loading the nine CSVs into typed records grouped by `user_id`, plus join validation. Pure I/O — takes paths, returns data, computes nothing. |
| `engine/` | The deterministic financial core: cash-state reconstruction, the 90-day forecast, plan generation and ranking, spending changes. Pure functions — no file reads, no model calls, no printing. |
| `extraction/` | All model access. Every call goes through `extraction/usage.py`, which logs and persists its cost. Message and image interpretation live here. |
| `output/` | Output row building, deterministic validation, and CSV writing. |
| `evaluation/` | The scorer, the floor baseline, the forecast-only runner, the provider probe, and `usage_report.md`. |
| `tests/` | Mirrors the folders above. Runs with no API key present. |

`main.py` is deliberately thin: argument parsing and delegation, nothing else. It is the
single entry point for a full run.

## Setup

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r code/requirements.txt   # Windows
.venv/bin/python -m pip install -r code/requirements.txt           # macOS / Linux
cp .env.example .env                                               # then fill it in
```

`.env` holds every credential and model name. Nothing is hardcoded, so switching a model
or adding one touches `.env` and `config/settings.py` only — never a logic file.

| variable | role |
|---|---|
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_API_VERSION`, `GPT_DEPLOYMENT` | primary — reasoning, image extraction, `decision_explanation` |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | verifier — reviews a finalized decision; a different model family from the primary by policy |
| `VERIFIER_ENABLED` | switches the verifier on; default off |

## Run

```bash
python code/main.py                       # full run over dataset/requests.csv -> output.csv (repo root)
python code/main.py --limit 5             # first N requests only
python code/main.py --dry-run             # process without writing any file
python code/main.py --sample              # the 25 labelled samples -> code/evaluation/sample_predictions.csv
python code/main.py --no-extract          # use cached message readings only (no model calls)
```

Message readings are cached in `code/extraction/cache/message_facts.json`, so a re-run
with an unchanged dataset and prompts makes no model calls. A row that fails any output
check is replaced by a safe `not_affordable` row, and the run prints how many.

## Evaluate

```bash
# score any predictions file against the 25 labelled samples
python code/evaluation/main.py --predictions <csv> --gold dataset/sample_requests.csv

# the all-not_affordable floor, to see what a given score is worth
python code/evaluation/baseline.py

# forecast engine alone: fills only amount_safe_to_pay and earliest_date_for_full_payment
python code/evaluation/forecast_runner.py
```

The scorer always exits 0 — it measures, it does not gate. Run it after every meaningful
change and read every column, not just the headline one.

## Test

```bash
python -m pytest code/tests -q
```

339 tests, and they pass with no API key present. Anything needing a live model call is
behind a stub injected in, never a real network call inside a unit test.

## Provider probe

```bash
python code/evaluation/probe.py
```

Standalone provider diagnostic: checks primary reachability, structured
output, vision and multilingual reading, then verifier structured output and whether the
verifier actually catches deliberately corrupted decisions. Never crashes; prints
PASS/FAIL with model, latency and token counts per check.

## Architecture

- **Data layer** (`data/`) — loads and joins `dataset/*.csv` by `user_id` / `request_id` /
  `related_event_id`. No database, no vector search; every join uses an explicit key.
  `validate_joins` asserts the structural facts in `DATASET_FACTS.md` E1/E6 on load and
  raises loudly rather than letting a broken join corrupt every downstream number.
- **Financial state reconstruction** (`engine/state.py`) — decides which events move cash
  and when (D4), converts them to the home currency (D2), detects which expense series
  recur and on what per-user cadence (D3), and resolves conflicting records. Blank amounts
  are reported as unresolved, never treated as zero.
- **Income stream detection** (`engine/income.py`) — groups credits by description, never
  by category, and classifies each description as stable, merge, terminal, one-off or
  irregular via the map in `config/settings.py` (`DATASET_FACTS.md` F1-F5). A terminal row
  such as `Final employer payroll` closes the streams before it.
- **90-day forecast engine** (`engine/forecast.py`) — projects daily net cash flow:
  known dated flows, recurring bills on their calendar day, income streams, and variable
  spending projected as one purchase per category on that user's exact gap
  (`VARIABLE_SPEND_MODEL = category_cadence`, DATASET_FACTS I1). `amount_safe_to_pay` comes
  from the worst dip over the full 90 days. `earliest_date_for_full_payment` and plan
  feasibility are checked up to `desired_completion_date` (`PLAN_CHECK_WINDOW = deadline`;
  set `horizon` to check all 90 days). Both are computed *before* optional spending changes.
- **Safety check** (`engine/safety.py`) — a plan within 5% of the minimum balance is
  re-decided under stress (variable spend ×1.1, message-raised income removed). It can only
  move toward caution; a barely-safe pay-now answer gets the smallest permitted spending
  change. Each row's trace is written to `evaluation/safety_trace.jsonl`.
- **Multimodal extraction** (`extraction/`) — resolves the 16 blank-amount events from
  their linked image, and reads messages for amendments, cancellations and confirmations.
- **Plan generation & ranking** (`engine/`) — builds eligible plans and ranks them by the
  six-criteria order in `problem_statement.md`.
- **Guards** (`output/`) — bounds, plan arithmetic, installment-matches-a-supplied-option,
  and flexible-only spending changes, checked on every row before it is written.
- **Token usage tracking** (`extraction/usage.py`) — every call to any model, in any role,
  is logged with model, tokens, latency and estimated cost from the first call onward, and
  persisted to `evaluation/usage_log.jsonl` so a crash cannot lose the accounting.
  `evaluation/usage_report.md` is generated from that log.

## Status

Built:
- config, usage tracking, the data layer, state reconstruction, and income detection by
  description class
- message extraction in one model call per message, with the user's profile as trusted
  context: language first, then amounts, instruction flag and facts
- message facts applied to the forecast, and the forecast engine
- plan generation and ranking, spending changes, and template explanations
- output guards with fallback, and `main.py` writing `output.csv`
- scorer, baseline and probe

Also built: image extraction for the 16 blank amounts, one vision call per image with
language identification in the same step, cached in `code/extraction/cache/image_facts.json`.

Not built: the verifier path (disabled by design; see DECISIONS.md #5).

Measured scores, the adoption log and numbered known limitations are in
[`METHOD.md`](../METHOD.md); verified dataset facts are in
[`DATASET_FACTS.md`](../DATASET_FACTS.md).

## Token usage and cost

See `evaluation/usage_report.md`, generated from the final full-dataset run. It carries a
per-model breakdown, an overall total, and a per-call-purpose split. No API keys or
configuration values appear in it.
