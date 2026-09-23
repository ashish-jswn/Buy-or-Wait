"""Every tunable in the project, read once, referenced by name everywhere else.

Thresholds, retry counts, buffers and the mapping from config variable to model role
live here and are never re-typed as a bare literal in a logic file. Model names and credentials come from .env only — nothing is hardcoded, so
swapping or adding a model touches .env and this file, nothing else.

Import as ``from config import NAME`` (config/__init__.py re-exports everything).
"""

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------

# code/config/settings.py -> code/ -> repo root
CODE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = CODE_ROOT.parent

DATASET_DIR = REPO_ROOT / "dataset"
IMAGES_DIR = DATASET_DIR / "media" / "images"
ENV_PATH = REPO_ROOT / ".env"

PROFILES_PATH = DATASET_DIR / "financial_profiles.csv"
EVENTS_PATH = DATASET_DIR / "financial_events.csv"
REQUESTS_PATH = DATASET_DIR / "requests.csv"
SAMPLE_REQUESTS_PATH = DATASET_DIR / "sample_requests.csv"
PAYMENT_OPTIONS_PATH = DATASET_DIR / "request_payment_options.csv"
MESSAGES_PATH = DATASET_DIR / "messages.csv"
IMAGES_INDEX_PATH = DATASET_DIR / "images.csv"
EXCHANGE_RATES_PATH = DATASET_DIR / "exchange_rates.csv"
OUTPUT_TEMPLATE_PATH = DATASET_DIR / "output.csv"

PREDICTIONS_OUTPUT_PATH = REPO_ROOT / "output.csv"
EVALUATION_DIR = CODE_ROOT / "evaluation"
USAGE_LOG_PATH = EVALUATION_DIR / "usage_log.jsonl"
USAGE_REPORT_PATH = EVALUATION_DIR / "usage_report.md"

# --------------------------------------------------------------------------
# forecast and decision tunables
# --------------------------------------------------------------------------

# problem_statement.md "90-Day Safety Check": the forecast window, in days from
# request_date inclusive.
FORECAST_HORIZON_DAYS = 90

# Extra cushion held above minimum_balance_to_keep. Zero by default: the gold rows
# treat the minimum as a hard floor, not a soft one. Exists so the safety margin is
# a named dial rather than an edit to the arithmetic.
MINIMUM_BALANCE_BUFFER = 0.0

# Money is carried and compared to the cent.
MONEY_TOLERANCE = 0.01

# A series must appear at least this many times in history before it is treated as
# recurring and projected forward (DATASET_FACTS D3).
RECURRENCE_MIN_OCCURRENCES = 3
# A detected cadence must be within this many days of a regular interval to count.
RECURRENCE_CADENCE_TOLERANCE_DAYS = 4
# Interval bands a recurring series is snapped to, in days.
RECURRENCE_MONTHLY_DAYS = 30
RECURRENCE_WEEKLY_DAYS = 7
# Which amount a detected recurring expense is projected at.
#   max    — the largest amount seen in its history (conservative)
#   latest — its most recent amount (research spec §3.1 "fixed monthly streams: last
#            observed amount")
RECURRING_AMOUNT_MAX = "max"
RECURRING_AMOUNT_LATEST = "latest"
RECURRING_EXPENSE_AMOUNT = RECURRING_AMOUNT_MAX
# Categories that are high-frequency but not fixed line items: forecast from history
# conservatively rather than scheduling them (DATASET_FACTS D3).
VARIABLE_ESSENTIAL_CATEGORIES = frozenset({"groceries", "transport", "dining"})
# How far back to look when averaging variable spending, in days.
VARIABLE_SPEND_LOOKBACK_DAYS = 90
# Multiplier applied to averaged variable spending, so the forecast errs high on
# outflows rather than low.
VARIABLE_SPEND_CONSERVATISM = 1.0
# How variable spending is forecast (DATASET_FACTS I1).
#   flat_daily       — average daily outflow over VARIABLE_SPEND_LOOKBACK_DAYS, every day
#   category_cadence — one purchase per category on its exact gap from the last purchase,
#                      at the category's mean amount; a purchase due on request_date is
#                      treated as already in the balance
VARIABLE_MODEL_FLAT = "flat_daily"
VARIABLE_MODEL_CATEGORY_CADENCE = "category_cadence"
# Adopted after measurement (METHOD.md, adoption log): on the samples,
# category_cadence raised amount within 1% 4→5, within 5% 11→13, status 16→17, method 16→17,
# plan 15→16, explanation 10→11, with overstated amounts unchanged and 0 rows riskier than gold.
VARIABLE_SPEND_MODEL = VARIABLE_MODEL_CATEGORY_CADENCE
VARIABLE_FLOW_KIND = "variable"

# Income stream classes (DATASET_FACTS F1). Income is grouped by description, never by
# category: nearly every income row is category "salary", so a category key merges an
# ended payroll into an ongoing one (user_05).
INCOME_CLASS_STABLE = "stable"
INCOME_CLASS_MERGE = "merge"
INCOME_CLASS_TERMINAL = "terminal"
INCOME_CLASS_ONE_OFF = "one_off"
INCOME_CLASS_IRREGULAR = "irregular"
# Every recurring-pattern credit description seen across users (38 of the 39 distinct
# credit descriptions); anything else takes INCOME_CLASS_UNKNOWN_DEFAULT.
INCOME_CLASS_BY_DESCRIPTION: dict[str, str] = {
    # stable: one stream each, projected monthly
    "Payroll credit": INCOME_CLASS_STABLE,
    "International employer payroll": INCOME_CLASS_STABLE,
    "Base salary": INCOME_CLASS_STABLE,
    "Primary household salary": INCOME_CLASS_STABLE,
    "Second household income": INCOME_CLASS_STABLE,
    "First-job payroll": INCOME_CLASS_STABLE,
    "New employer payroll": INCOME_CLASS_STABLE,
    "Payroll after returning from leave": INCOME_CLASS_STABLE,
    # merge: the confirmed next occurrence of an existing stable stream
    "Next confirmed salary": INCOME_CLASS_MERGE,
    # terminal: the stream has ended; never projected, closes earlier streams
    "Final employer payroll": INCOME_CLASS_TERMINAL,
    "Previous employer payroll": INCOME_CLASS_TERMINAL,
    "Payroll before leave": INCOME_CLASS_TERMINAL,
    "Seasonal contract payment": INCOME_CLASS_TERMINAL,
    "Peak-season wages": INCOME_CLASS_TERMINAL,
    "Temporary assignment pay": INCOME_CLASS_TERMINAL,
    # one-off: never projected, never a recurring baseline
    "Prorated first salary": INCOME_CLASS_ONE_OFF,
    "Promotion arrears payment": INCOME_CLASS_ONE_OFF,
    "Quarterly performance bonus": INCOME_CLASS_ONE_OFF,
    "Prize proceeds": INCOME_CLASS_ONE_OFF,
    "Investment sale proceeds": INCOME_CLASS_ONE_OFF,
    "Employer expense reimbursement": INCOME_CLASS_ONE_OFF,
    "Settled card charge reversal": INCOME_CLASS_ONE_OFF,
    "Pending merchant refund": INCOME_CLASS_ONE_OFF,
    # "August 2019 net salary" (one row, one user) is deliberately not listed: it falls to
    # INCOME_CLASS_UNKNOWN_DEFAULT (never projected), with identical behaviour, and keeps
    # a single-row special case out of the map.
    # irregular: pooled into one stream per user and projected
    "Delivery platform payout": INCOME_CLASS_IRREGULAR,
    "Driver platform payout": INCOME_CLASS_IRREGULAR,
    "Weekly app earnings": INCOME_CLASS_IRREGULAR,
    "Task marketplace payout": INCOME_CLASS_IRREGULAR,
    "Website project payment": INCOME_CLASS_IRREGULAR,
    "Content contract payment": INCOME_CLASS_IRREGULAR,
    "Design contract payment": INCOME_CLASS_IRREGULAR,
    "Application project payment": INCOME_CLASS_IRREGULAR,
    "Freelance milestone payment": INCOME_CLASS_IRREGULAR,
    "Consulting invoice payment": INCOME_CLASS_IRREGULAR,
    "Independent work payment": INCOME_CLASS_IRREGULAR,
    "Client retainer payment": INCOME_CLASS_IRREGULAR,
    "Account commission payment": INCOME_CLASS_IRREGULAR,
    "Performance commission": INCOME_CLASS_IRREGULAR,
    "Monthly sales commission": INCOME_CLASS_IRREGULAR,
}
# A credit description not in the map is never projected: the financially safer reading.
INCOME_CLASS_UNKNOWN_DEFAULT = INCOME_CLASS_ONE_OFF
# Stable streams project from a single observation. 806 of 814 observed gaps across the
# stable descriptions are 28-31 days, so the description itself is the cadence evidence
# (New employer payroll and Payroll after returning from leave only ever have one row).
INCOME_STABLE_MIN_OCCURRENCES = 1
# Irregular income needs this many pooled rows before it is projected.
INCOME_IRREGULAR_MIN_OCCURRENCES = 3
# Series-key label for the pooled irregular stream.
INCOME_IRREGULAR_POOL_LABEL = "irregular income pool"
# An income stream whose last payment is more than this many days before request_date has
# ended (author decision, 2026-09-13). Second household income sits at 45-48 days in 10/10
# users; every other open stable stream's last payment is 5-23 days before the request.
INCOME_STALE_DAYS = 40

# Message facts applied to the forecast (engine/evidence.py).
RENT_CATEGORY = "rent"
# Kind and category suffix for cash flows produced from a message, so a message-adjusted
# salary never collides with the same-day de-duplication of an untouched series.
EVIDENCE_FLOW_KIND = "message"
EVIDENCE_CATEGORY_SUFFIX = ":message"

# Event statuses and what they mean for cash (DATASET_FACTS D4).
STATUS_SETTLED = "settled"
STATUS_PENDING = "pending"
STATUS_SCHEDULED = "scheduled"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"
STATUS_UNREALIZED = "unrealized"
# Never contribute cash in any direction.
IGNORED_STATUSES = frozenset({STATUS_CANCELLED, STATUS_FAILED, STATUS_UNREALIZED})
# Count as future cash movements when dated on or after request_date.
FUTURE_CASH_STATUSES = frozenset({STATUS_SETTLED, STATUS_PENDING, STATUS_SCHEDULED})
# Pending credits are never counted; pending debits always are.
DIRECTION_DEBIT = "debit"
DIRECTION_CREDIT = "credit"
DIRECTION_NON_CASH = "non_cash"

# Flexibility values eligible for a spending change (DATASET_FACTS A3/A5).
FLEXIBILITY_FIXED = "fixed"
REDUCIBLE_FLEXIBILITIES = frozenset({"reducible", "reducible_or_stoppable"})
STOPPABLE_FLEXIBILITIES = frozenset({"stoppable", "reducible_or_stoppable"})
MAX_SPENDING_CHANGES = 3

# Safety check for critical cases (author decisions, 2026-09-13). A recommended plan whose
# lowest projected balance sits within NEAR_FLOOR_FRACTION × minimum_balance_to_keep of the
# floor is re-tested with variable spending scaled by STRESS_VARIABLE_MULTIPLIER; if the
# plan no longer holds, the stressed (safer) decision replaces it and a trace is recorded.
# How far plan feasibility and earliest_date_for_full_payment are checked.
#   horizon  — every day of the 90-day window (problem statement's 90-day safety check)
#   deadline — up to desired_completion_date only; amount_safe_to_pay always uses the full
#              90-day window either way
PLAN_CHECK_WINDOW_HORIZON = "horizon"
PLAN_CHECK_WINDOW_DEADLINE = "deadline"
# Adopted after measurement (METHOD.md, adoption log): gold's own plans hold in our forecast
# 11/18 → 15/18 when checked to the deadline. On the samples: status 17→23, method 17→23,
# plan 16→22, earliest 16→21, explanation 11→17, all-7 2→4, wrongly refused 7→1, with 0 rows
# riskier than gold and no growth in large or overstated amount errors. Trade-off: a plan is
# no longer checked for dips after desired_completion_date (problem statement's 90-day check).
PLAN_CHECK_WINDOW = PLAN_CHECK_WINDOW_DEADLINE

NEAR_FLOOR_FRACTION = 0.05
STRESS_VARIABLE_MULTIPLIER = 1.1
# Message-raised income stress (author decision: stress-test it; scope set by measurement).
# A plan that depends on income a message raised or created is re-decided without those
# facts; if the plan changes, the more cautious decision is used.
#   off        — never
#   near_floor — only plans already within NEAR_FLOOR_FRACTION of the floor
#   all        — every non-refusal whose user has such a message
MESSAGE_INCOME_STRESS_OFF = "off"
MESSAGE_INCOME_STRESS_NEAR_FLOOR = "near_floor"
MESSAGE_INCOME_STRESS_ALL = "all"
# Measured (METHOD.md, adoption log): "all" broke request_02, whose gold installment plan
# relies on a message-confirmed salary increase (status 17→16 and four other columns fell,
# full-set not_affordable 101→120). "near_floor" left every sample column unchanged, kept
# 0 rows riskier than gold, and made 3 marginal full-set approvals more cautious. Adopted.
MESSAGE_INCOME_STRESS_SCOPE = MESSAGE_INCOME_STRESS_NEAR_FLOOR
INCOME_RAISING_INTENTS = frozenset(
    {
        "salary_increase",
        "first_salary",
        "salary_resumes_with_childcare",
        "fx_salary_confirmed",
        "invoice_approved",
        "salary_next_with_arrears",
        "next_salary_reduced",
        "base_salary_commission_pending",
        "remaining_household_salary",
    }
)
SAFETY_TRACE_PATH = EVALUATION_DIR / "safety_trace.jsonl"
# Kind for the savings flows a spending change adds to a trial forecast.
SPENDING_CHANGE_FLOW_KIND = "spending_change"

# Output vocabularies (problem_statement.md, output schema).
METHOD_FULL_PAYMENT = "full_payment"
METHOD_PARTIAL_PAYMENT = "partial_payment"
METHOD_INSTALLMENTS = "installments"
METHOD_WAIT = "wait"
METHOD_NOT_RECOMMENDED = "not_recommended"
STATUS_AFFORDABLE_NOW = "affordable_now"
STATUS_AFFORDABLE_WITH_PLAN = "affordable_with_plan"
STATUS_AFFORDABLE_LATER = "affordable_later"
STATUS_NOT_AFFORDABLE = "not_affordable"
PLAN_NONE = "none"
SPENDING_CHANGES_NONE = "none"
OUTPUT_COLUMNS: tuple[str, ...] = (
    "request_id",
    "amount_safe_to_pay",
    "affordability_status",
    "recommended_payment_method",
    "payment_plan",
    "earliest_date_for_full_payment",
    "spending_changes_needed",
    "decision_explanation",
)

# --------------------------------------------------------------------------
# model configuration — from .env only
# --------------------------------------------------------------------------

load_dotenv(ENV_PATH)


def _env(name: str, default: str = "") -> str:
    """Read an environment variable, stripped. Never returns None."""
    return (os.environ.get(name) or default).strip()


# Primary role: agent reasoning, image extraction, decision_explanation.
PRIMARY_ENDPOINT = _env("AZURE_OPENAI_ENDPOINT")
PRIMARY_API_KEY = _env("AZURE_OPENAI_API_KEY")
PRIMARY_API_VERSION = _env("AZURE_OPENAI_API_VERSION", "2025-08-07")
PRIMARY_MODEL = _env("GPT_DEPLOYMENT")
PRIMARY_PROVIDER = "azure_openai"
# Reasoning effort requested from the primary model on extraction calls. Extraction is
# reading, not planning, so low effort keeps latency and output tokens down.
MODEL_REASONING_EFFORT = _env("PRIMARY_REASONING_EFFORT", "low")

# Message extraction: results are cached per message id so a re-run makes no calls.
MESSAGE_FACTS_CACHE_PATH = CODE_ROOT / "extraction" / "cache" / "message_facts.json"
EXTRACTION_MAX_WORKERS = 8

# Image extraction for blank-amount events: cached per event, keyed on image bytes, event
# context, prompt and schema.
IMAGE_FACTS_CACHE_PATH = CODE_ROOT / "extraction" / "cache" / "image_facts.json"
# The model's chosen amount is used only at these confidence levels; below them the
# financially safer candidate is used (larger for a debit, smaller for a credit).
IMAGE_CONFIDENCE_ACCEPTED = frozenset({"high", "medium"})

# The closed intent vocabulary the model must map every message fact onto
# (DATASET_FACTS G1: all 215 messages fall into these 30 templates plus the injection).
MESSAGE_INTENTS: tuple[str, ...] = (
    "salary_increase",
    "salary_next_with_arrears",
    "salary_next_confirmed_no_amount",
    "temporary_pay",
    "next_salary_reduced",
    "payday_moved",
    "payout_pending",
    "base_salary_commission_pending",
    "seasonal_contract_ended",
    "employment_ended",
    "salary_resumes_with_childcare",
    "first_salary",
    "rent_increase_pct",
    "internal_transfer",
    "refund_pending",
    "investment_value_unrealized",
    "prize_claim_processing",
    "prize_proceeds_settled",
    "invoice_approved",
    "remaining_household_salary",
    "fx_salary_confirmed",
    "bonus_pending",
    "failed_debit_retry",
    "dispute_open",
    "investment_sale_settled",
    "reimbursement_closed",
    "fx_refund_processing",
    "fx_bill_pending",
    "two_card_minimums",
    "receipt_has_amount",
    "other",
)
# Injection pre-filter, applied before any model call (DATASET_FACTS E7). Each pattern
# pairs a demand to pay with a release/processing charge, so a legitimate message that
# merely mentions a processing fee is not discarded.
INJECTION_PATTERNS: tuple[str, ...] = (
    r"pay the (release|processing) charge",
    r"bayar biaya (pencairan|pemrosesan)",
)

# Verifier role: reviews a finalized decision. Different model family by policy
# (DECISIONS.md #5).
VERIFIER_API_KEY = _env("GEMINI_API_KEY")
VERIFIER_MODEL = _env("GEMINI_MODEL")

# Verifier runs only when explicitly switched on. Default off: its usefulness probe
# wrongly rejected 1 of 3 correct decisions, and the free tier caps throughput at
# VERIFIER_REQUESTS_PER_MINUTE (DECISIONS.md #5, METHOD.md limitation 9).
VERIFIER_ENABLED = _env("VERIFIER_ENABLED", "false").lower() in {"1", "true", "yes"}
VERIFIER_REQUESTS_PER_MINUTE = 5

# Retry and timeout policy, applied by the shared model client wrapper.
MODEL_MAX_ATTEMPTS = 3
MODEL_RETRY_BASE_SECONDS = 2.0
MODEL_RETRY_MAX_SECONDS = 60.0
MODEL_TIMEOUT_SECONDS = 120.0
MODEL_MAX_OUTPUT_TOKENS = 2000

# Cost per million tokens, by model name, for the usage report. Update alongside any
# model change; an unknown model is reported at zero cost and flagged in the report.
MODEL_COST_PER_MILLION_TOKENS: dict[str, tuple[float, float]] = {
    # model: (input cost per 1M, output cost per 1M) in USD
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gemini-3.6-flash": (0.30, 2.50),
    "gemini-2.5-flash": (0.30, 2.50),
}
USAGE_REPORT_CURRENCY = "USD"


def missing_primary_config() -> list[str]:
    """Names of the .env variables the primary role needs and does not have."""
    required = {
        "AZURE_OPENAI_ENDPOINT": PRIMARY_ENDPOINT,
        "AZURE_OPENAI_API_KEY": PRIMARY_API_KEY,
        "GPT_DEPLOYMENT": PRIMARY_MODEL,
    }
    return [name for name, value in required.items() if not value]


def missing_verifier_config() -> list[str]:
    """Names of the .env variables the verifier role needs and does not have."""
    required = {"GEMINI_API_KEY": VERIFIER_API_KEY, "GEMINI_MODEL": VERIFIER_MODEL}
    return [name for name, value in required.items() if not value]


def cost_for(model: str, input_tokens: int, output_tokens: int) -> Optional[float]:
    """Estimated USD cost of one call, or None when the model's pricing is unknown."""
    pricing = MODEL_COST_PER_MILLION_TOKENS.get(model)
    if pricing is None:
        return None
    input_rate, output_rate = pricing
    return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000
