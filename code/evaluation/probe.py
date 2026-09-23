"""Standalone provider probe: verifies model configuration before a run.

Throwaway diagnostic, not part of the pipeline. Reads model configuration from
.env only, makes one call per check, and never raises: every check prints
PASS/FAIL with the model name, latency, and input/output token counts.

Checks:
  1. primary  - reachability, plain text
  2. primary  - structured output in a fixed JSON shape
  3. primary  - vision, reading dataset/media/images/image_02.png
  4. primary  - multilingual, reading the Indonesian message_01
  5. verifier - structured output, same JSON shape as check 2
  6. verifier - usefulness: three correct gold decisions and three corrupted
                copies, reported as a confusion matrix

Run:
    python code/evaluation/probe.py
"""

import base64
import csv
import json
import os
import re
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = REPO_ROOT / "dataset"
IMAGES_DIR = DATASET_DIR / "media" / "images"

# --- fixtures the probe asserts against -----------------------------------
VISION_IMAGE_ID = "image_02"
VISION_PROMPT = "What is the Balance Due on this receipt? Reply with only the number."
VISION_EXPECTED = "100000"

MULTILINGUAL_MESSAGE_ID = "message_01"
MULTILINGUAL_EXPECTED_AMOUNT = "42750000"
MULTILINGUAL_EXPECTED_DATE = "2025-08-15"

# The shared structured-output shape both roles are probed against.
STRUCTURE_PROMPT = (
    "Reply with only a JSON object, no prose and no code fence, with exactly these "
    'keys: {"status": one of "affordable_now"/"not_affordable", "amount": a number, '
    '"reason": a short string}. Use status "affordable_now", amount 1234.5, and any reason.'
)
STRUCTURE_REQUIRED_KEYS = ("status", "amount", "reason")

VERIFIER_SYSTEM = (
    "You review a financial agent's decision for a single request. Reply with only a "
    'JSON object: {"verdict": "accept" or "reject", "reason": "<one short sentence>"}. '
    "Reject when the decision contradicts the user's financial position, the rules "
    "stated in the context, or its own arithmetic. Accept when it is sound."
)

MAX_OUTPUT_TOKENS = 2000
FORECAST_EVENT_SAMPLE = 25


@dataclass
class CallResult:
    """One model call: what came back, what it cost, and whether it worked."""

    ok: bool
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    latency_seconds: float = 0.0
    error: str = ""


@dataclass
class ProbeOutcome:
    """One probe check's verdict, for the end-of-run summary."""

    name: str
    passed: bool
    detail: str = ""


OUTCOMES: list[ProbeOutcome] = []


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


@dataclass
class ProviderConfig:
    """Model configuration for both roles, as read from .env."""

    primary_endpoint: str = ""
    primary_api_key: str = ""
    primary_api_version: str = ""
    primary_model: str = ""
    verifier_api_key: str = ""
    verifier_model: str = ""
    missing: list[str] = field(default_factory=list)


def load_config() -> ProviderConfig:
    """Read model configuration from .env; record which variables are absent."""
    load_dotenv(REPO_ROOT / ".env")
    config = ProviderConfig(
        primary_endpoint=os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip(),
        primary_api_key=os.environ.get("AZURE_OPENAI_API_KEY", "").strip(),
        primary_api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "").strip(),
        primary_model=os.environ.get("GPT_DEPLOYMENT", "").strip(),
        verifier_api_key=os.environ.get("GEMINI_API_KEY", "").strip(),
        verifier_model=os.environ.get("GEMINI_MODEL", "").strip(),
    )
    for name, value in [
        ("AZURE_OPENAI_ENDPOINT", config.primary_endpoint),
        ("AZURE_OPENAI_API_KEY", config.primary_api_key),
        ("GPT_DEPLOYMENT", config.primary_model),
        ("GEMINI_API_KEY", config.verifier_api_key),
    ]:
        if not value:
            config.missing.append(name)
    return config


# ---------------------------------------------------------------------------
# clients
# ---------------------------------------------------------------------------


def build_primary_client(config: ProviderConfig) -> Any:
    """Build the primary (Azure OpenAI) client, or return None on failure.

    An endpoint ending in /openai/v1 is Azure's v1 surface, which the plain
    OpenAI client talks to via base_url; anything else uses AzureOpenAI.
    """
    try:
        if config.primary_endpoint.rstrip("/").endswith("/openai/v1"):
            from openai import OpenAI

            return OpenAI(base_url=config.primary_endpoint, api_key=config.primary_api_key)
        from openai import AzureOpenAI

        return AzureOpenAI(
            azure_endpoint=config.primary_endpoint,
            api_key=config.primary_api_key,
            api_version=config.primary_api_version or "2025-08-07",
        )
    except Exception as error:  # noqa: BLE001 - a probe never crashes
        print(f"  could not construct primary client: {error}")
        return None


def build_verifier_client(config: ProviderConfig) -> Any:
    """Build the verifier (Gemini) client, or return None on failure."""
    try:
        from google import genai

        return genai.Client(api_key=config.verifier_api_key)
    except Exception as error:  # noqa: BLE001 - a probe never crashes
        print(f"  could not construct verifier client: {error}")
        return None


def call_primary(
    client: Any, model: str, content: Any, system: Optional[str] = None
) -> CallResult:
    """Send one chat completion to the primary model and capture usage.

    `content` is either a string or an OpenAI content-parts list (for vision).
    Retries once without the optional token cap if the model rejects it.
    """
    messages: list[dict[str, Any]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": content})

    for extra in ({"max_completion_tokens": MAX_OUTPUT_TOKENS}, {}):
        started = time.monotonic()
        try:
            response = client.chat.completions.create(model=model, messages=messages, **extra)
            usage = getattr(response, "usage", None)
            return CallResult(
                ok=True,
                text=(response.choices[0].message.content or "").strip(),
                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                latency_seconds=time.monotonic() - started,
            )
        except Exception as error:  # noqa: BLE001 - a probe never crashes
            last = CallResult(
                ok=False,
                latency_seconds=time.monotonic() - started,
                error=f"{type(error).__name__}: {error}",
            )
    return last


def call_verifier(client: Any, model: str, prompt: str) -> CallResult:
    """Send one generation to the verifier model and capture usage."""
    started = time.monotonic()
    try:
        response = client.models.generate_content(model=model, contents=prompt)
        usage = getattr(response, "usage_metadata", None)
        return CallResult(
            ok=True,
            text=(response.text or "").strip(),
            input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
            output_tokens=getattr(usage, "candidates_token_count", 0) or 0,
            latency_seconds=time.monotonic() - started,
        )
    except Exception as error:  # noqa: BLE001 - a probe never crashes
        return CallResult(
            ok=False,
            latency_seconds=time.monotonic() - started,
            error=f"{type(error).__name__}: {error}",
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def report(name: str, model: str, result: CallResult, passed: bool, detail: str = "") -> None:
    """Print one check's result line and record it for the summary."""
    verdict = "PASS" if passed else "FAIL"
    print(f"[{verdict}] {name}")
    print(f"       model={model}  latency={result.latency_seconds:.2f}s  "
          f"in={result.input_tokens} out={result.output_tokens}")
    if result.error:
        print(f"       error: {result.error}")
    if detail:
        for line in detail.splitlines():
            print(f"       {line}")
    OUTCOMES.append(ProbeOutcome(name=name, passed=passed, detail=detail.splitlines()[0] if detail else ""))


def extract_json(text: str) -> Optional[dict[str, Any]]:
    """Pull the first JSON object out of a model reply, tolerating a code fence."""
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match is None:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def parse_number(text: object) -> Optional[float]:
    """Parse the first number out of a model reply, tolerating grouping separators.

    Handles both Western and Indian digit grouping ("100,000" and "1,00,000.00")
    and a leading currency code or symbol. Returns None when nothing parses.
    """
    if isinstance(text, (int, float)):
        return float(text)
    match = re.search(r"-?[\d, \s]*\d(?:\.\d+)?", str(text or ""))
    if match is None:
        return None
    cleaned = re.sub(r"[, \s]", "", match.group(0))
    try:
        return float(cleaned)
    except ValueError:
        return None


def numbers_equal(expected: str, returned: object) -> bool:
    """True when a returned amount equals the expected one, within a cent."""
    parsed = parse_number(returned)
    return parsed is not None and abs(parsed - float(expected)) <= 0.01


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    """Read a UTF-8 CSV into a list of dicts with stripped values."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(handle)]


def encode_image_data_url(image_id: str) -> Optional[str]:
    """Base64-encode a dataset image as a data URL, or None when absent."""
    path = IMAGES_DIR / f"{image_id}.png"
    if not path.is_file():
        return None
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


# ---------------------------------------------------------------------------
# checks 1-4: primary
# ---------------------------------------------------------------------------


def probe_primary_text(client: Any, model: str) -> None:
    """Check 1 - does the primary respond to a plain text call at all."""
    result = call_primary(client, model, "Reply with exactly the word: ready")
    passed = result.ok and "ready" in result.text.lower()
    report("1. primary / reachability (plain text)", model, result,
           passed, f"reply: {result.text!r}")


def probe_primary_structured(client: Any, model: str) -> None:
    """Check 2 - does the primary return the requested JSON shape, parseable."""
    result = call_primary(client, model, STRUCTURE_PROMPT)
    parsed = extract_json(result.text)
    passed = bool(
        result.ok and parsed is not None and all(k in parsed for k in STRUCTURE_REQUIRED_KEYS)
    )
    detail = f"reply: {result.text!r}\nparsed: {parsed!r}"
    report("2. primary / structured output (JSON shape)", model, result, passed, detail)


def probe_primary_vision(client: Any, model: str) -> None:
    """Check 3 - can the primary read an amount off a dataset receipt image."""
    data_url = encode_image_data_url(VISION_IMAGE_ID)
    if data_url is None:
        report("3. primary / vision (image_02.png)", model, CallResult(ok=False,
               error=f"{VISION_IMAGE_ID}.png not found"), False)
        return
    content = [
        {"type": "text", "text": VISION_PROMPT},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]
    result = call_primary(client, model, content)
    passed = result.ok and numbers_equal(VISION_EXPECTED, result.text)
    detail = (
        f"expected: {VISION_EXPECTED}\n"
        f"returned verbatim: {result.text!r}\n"
        f"parsed as number: {parse_number(result.text)!r}"
    )
    report("3. primary / vision (image_02.png)", model, result, passed, detail)


def probe_primary_multilingual(client: Any, model: str) -> None:
    """Check 4 - can the primary read the Indonesian message_01 accurately."""
    messages = read_csv_rows(DATASET_DIR / "messages.csv")
    message = next((m for m in messages if m["message_id"] == MULTILINGUAL_MESSAGE_ID), None)
    if message is None:
        report("4. primary / multilingual (message_01, Indonesian)", model,
               CallResult(ok=False, error=f"{MULTILINGUAL_MESSAGE_ID} not found"), False)
        return
    prompt = (
        "Read this payroll message and reply with only a JSON object, no prose and no "
        'code fence: {"new_monthly_salary": <number, digits only>, '
        '"effective_date": "<YYYY-MM-DD>"}.\n\nMessage:\n' + message["message_text"]
    )
    result = call_primary(client, model, prompt)
    parsed = extract_json(result.text) or {}
    got_amount = parse_number(parsed.get("new_monthly_salary", ""))
    got_date = str(parsed.get("effective_date", "")).strip()
    passed = (
        result.ok
        and numbers_equal(MULTILINGUAL_EXPECTED_AMOUNT, got_amount)
        and got_date == MULTILINGUAL_EXPECTED_DATE
    )
    detail = (
        f"expected: amount={MULTILINGUAL_EXPECTED_AMOUNT} date={MULTILINGUAL_EXPECTED_DATE}\n"
        f"returned verbatim: {result.text!r}\n"
        f"parsed: amount={got_amount!r} date={got_date!r}"
    )
    report("4. primary / multilingual (message_01, Indonesian)", model, result, passed, detail)


# ---------------------------------------------------------------------------
# check 5: verifier structured output
# ---------------------------------------------------------------------------


def probe_verifier_structured(client: Any, model: str) -> None:
    """Check 5 - does the verifier return the same JSON shape as check 2."""
    result = call_verifier(client, model, STRUCTURE_PROMPT)
    parsed = extract_json(result.text)
    passed = bool(
        result.ok and parsed is not None and all(k in parsed for k in STRUCTURE_REQUIRED_KEYS)
    )
    detail = f"reply: {result.text!r}\nparsed: {parsed!r}"
    report("5. verifier / structured output (JSON shape)", model, result, passed, detail)


# ---------------------------------------------------------------------------
# check 6: verifier usefulness
# ---------------------------------------------------------------------------


def build_user_context(user_id: str, request_row: dict[str, str]) -> str:
    """Assemble a compact financial context block for one user and request."""
    profiles = {r["user_id"]: r for r in read_csv_rows(DATASET_DIR / "financial_profiles.csv")}
    events = [
        r for r in read_csv_rows(DATASET_DIR / "financial_events.csv") if r["user_id"] == user_id
    ]
    options = [
        r
        for r in read_csv_rows(DATASET_DIR / "request_payment_options.csv")
        if r["request_id"] == request_row["request_id"]
    ]
    profile = profiles[user_id]
    recent = sorted(events, key=lambda r: r["event_date"])[-FORECAST_EVENT_SAMPLE:]

    lines = [
        "USER PROFILE",
        f"  home_currency={profile['home_currency']} "
        f"available_balance={profile['current_available_balance']} "
        f"minimum_balance_to_keep={profile['minimum_balance_to_keep']}",
        f"  protected_categories={profile['expense_categories_to_protect']}",
        f"  willing_to_reduce={profile['expense_categories_user_is_willing_to_reduce'] or '(none)'}"
        f"  willing_to_stop={profile['expense_categories_user_is_willing_to_stop'] or '(none)'}",
        f"  payment_methods_user_will_consider={profile['payment_methods_user_will_consider']}"
        f"  max_installment_months={profile['max_installment_months'] or '(none)'}",
        "",
        "REQUEST",
        f"  {request_row['request_id']} date={request_row['request_date']} "
        f"type={request_row['request_type']} amount={request_row['requested_amount']} "
        f"complete_by={request_row['desired_completion_date']} "
        f"allows_partial_payment={request_row['allows_partial_payment']}",
        "",
        "SUPPLIED PAYMENT OPTIONS",
    ]
    for option in options:
        lines.append(
            f"  {option['payment_option_id']} {option['payment_method']} "
            f"amount={option['payment_amount']} x{option['number_of_payments']} "
            f"from={option['first_payment_date']} every={option['payment_frequency_days']}d "
            f"fee={option['financing_fee']} total={option['total_payable_amount']}"
        )
    lines += ["", f"MOST RECENT {len(recent)} FINANCIAL EVENTS (of {len(events)})"]
    for event in recent:
        lines.append(
            f"  {event['event_id']} {event['event_date']} {event['category']} "
            f"{event['direction']} {event['amount'] or '(blank)'} {event['currency']} "
            f"{event['status']} flexibility={event['flexibility']}"
            + (f" min_allowed={event['minimum_allowed_amount']}"
               if event["minimum_allowed_amount"] else "")
        )
    lines += [
        "",
        "RULES THE DECISION MUST OBEY",
        "  - The balance must never fall below minimum_balance_to_keep over the 90-day forecast.",
        "  - 0 <= amount_safe_to_pay <= requested_amount.",
        "  - For partial_payment the two payments must add up to exactly requested_amount.",
        "  - spending_changes_needed may only target events whose flexibility is not 'fixed',",
        "    and only in a category the user is willing to reduce or stop.",
        "  - affordable_now requires earliest_date_for_full_payment == request_date.",
    ]
    return "\n".join(lines)


def format_decision(row: dict[str, str]) -> str:
    """Render a decision as the verifier sees it."""
    return (
        "DECISION UNDER REVIEW\n"
        f"  amount_safe_to_pay={row['amount_safe_to_pay']}\n"
        f"  affordability_status={row['affordability_status']}\n"
        f"  recommended_payment_method={row['recommended_payment_method']}\n"
        f"  payment_plan={row['payment_plan']}\n"
        f"  earliest_date_for_full_payment={row['earliest_date_for_full_payment'] or '(empty)'}\n"
        f"  spending_changes_needed={row['spending_changes_needed']}\n"
        f"  decision_explanation={row['decision_explanation']}"
    )


def build_verifier_cases() -> list[tuple[str, str, bool, dict[str, str]]]:
    """Three correct gold decisions and three corrupted copies.

    Returns (label, user_id, is_corrupt, decision_row) tuples. The three
    corruptions are one per defect class named in the addendum: a wrong
    affordability_status, a payment_plan that does not sum to requested_amount,
    and a spending change pointing at a fixed event.
    """
    gold = {r["request_id"]: r for r in read_csv_rows(DATASET_DIR / "sample_requests.csv")}
    clean_16, clean_19, clean_21 = gold["request_16"], gold["request_19"], gold["request_21"]

    # request_16 gold is affordable_now / full_payment; claim it is unaffordable.
    corrupt_status = dict(clean_16)
    corrupt_status["affordability_status"] = "not_affordable"
    corrupt_status["recommended_payment_method"] = "not_recommended"

    # request_19 gold is 28820 + 10840 = 39660; break the arithmetic.
    corrupt_plan = dict(clean_19)
    corrupt_plan["payment_plan"] = "2024-09-04:28820|2024-09-15:5000"

    # request_21 gold stops a stoppable subscription; point at fixed rent instead.
    corrupt_change = dict(clean_21)
    corrupt_change["spending_changes_needed"] = "stop:event_1818"

    return [
        ("request_16 gold (correct)", "user_16", False, clean_16),
        ("request_19 gold (correct)", "user_19", False, clean_19),
        ("request_21 gold (correct)", "user_21", False, clean_21),
        ("request_16 corrupted affordability_status", "user_16", True, corrupt_status),
        ("request_19 corrupted payment_plan (does not sum)", "user_19", True, corrupt_plan),
        ("request_21 corrupted spending change (fixed event)", "user_21", True, corrupt_change),
    ]


def probe_verifier_usefulness(client: Any, model: str) -> None:
    """Check 6 - does the verifier actually catch corrupted decisions."""
    print("[....] 6. verifier / usefulness (3 correct + 3 corrupted)")
    caught, missed, wrongly_rejected, accepted, failed_calls = 0, 0, 0, 0, 0
    total_in, total_out = 0, 0

    for label, user_id, is_corrupt, decision in build_verifier_cases():
        prompt = (
            VERIFIER_SYSTEM
            + "\n\n"
            + build_user_context(user_id, decision)
            + "\n\n"
            + format_decision(decision)
        )
        result = call_verifier(client, model, prompt)
        total_in += result.input_tokens
        total_out += result.output_tokens
        parsed = extract_json(result.text) or {}
        verdict = str(parsed.get("verdict", "")).strip().lower()
        reason = str(parsed.get("reason", "")).strip()

        if not result.ok or verdict not in {"accept", "reject"}:
            failed_calls += 1
            mark = "ERROR"
        elif is_corrupt and verdict == "reject":
            caught += 1
            mark = "caught"
        elif is_corrupt:
            missed += 1
            mark = "MISSED"
        elif verdict == "accept":
            accepted += 1
            mark = "accepted"
        else:
            wrongly_rejected += 1
            mark = "FALSE REJECT"

        print(f"       {label}")
        print(f"         expected={'reject' if is_corrupt else 'accept'} got={verdict or '(none)'} "
              f"-> {mark}  latency={result.latency_seconds:.2f}s "
              f"in={result.input_tokens} out={result.output_tokens}")
        if result.error:
            print(f"         error: {result.error}")
        if reason:
            print(f"         reason: {reason}")

    print()
    print("       CONFUSION MATRIX")
    print(f"         corrupted rows caught (true reject) : {caught}/3")
    print(f"         corrupted rows missed (false accept): {missed}/3")
    print(f"         correct rows accepted (true accept) : {accepted}/3")
    print(f"         correct rows wrongly rejected       : {wrongly_rejected}/3")
    print(f"         unusable replies / call errors      : {failed_calls}/6")
    print(f"         tokens for this check: in={total_in} out={total_out}")

    passed = caught == 3 and wrongly_rejected == 0 and failed_calls == 0
    OUTCOMES.append(
        ProbeOutcome(
            name="6. verifier / usefulness (3 correct + 3 corrupted)",
            passed=passed,
            detail=f"caught {caught}/3 corrupt, {wrongly_rejected}/3 false rejects, "
                   f"{failed_calls}/6 errors",
        )
    )


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def run_check(label: str, action: Callable[[], None]) -> None:
    """Run one check, converting any unexpected error into a FAIL line."""
    try:
        action()
    except Exception as error:  # noqa: BLE001 - a probe never crashes
        print(f"[FAIL] {label}")
        print(f"       unexpected error: {type(error).__name__}: {error}")
        traceback.print_exc(file=sys.stderr)
        OUTCOMES.append(ProbeOutcome(name=label, passed=False, detail=str(error)))


def main() -> int:
    """Run every probe check. Always returns 0 — this is a diagnostic."""
    config = load_config()
    print("=" * 92)
    print("Buy or Wait? - provider probe")
    print(f"primary  : {config.primary_model or '(unset)'}  endpoint={config.primary_endpoint or '(unset)'}")
    print(f"verifier : {config.verifier_model or '(unset)'}  (different model family from primary)")
    if config.missing:
        print(f"MISSING .env variables: {', '.join(config.missing)}")
    print("=" * 92)
    print()

    primary = build_primary_client(config) if not config.missing else None
    verifier = build_verifier_client(config) if config.verifier_api_key else None

    if primary is None:
        print("[FAIL] primary client unavailable; checks 1-4 skipped\n")
    else:
        for label, action in [
            ("1. primary / reachability", lambda: probe_primary_text(primary, config.primary_model)),
            ("2. primary / structured output",
             lambda: probe_primary_structured(primary, config.primary_model)),
            ("3. primary / vision", lambda: probe_primary_vision(primary, config.primary_model)),
            ("4. primary / multilingual",
             lambda: probe_primary_multilingual(primary, config.primary_model)),
        ]:
            run_check(label, action)
            print()

    if verifier is None:
        print("[FAIL] verifier client unavailable; checks 5-6 skipped\n")
    else:
        run_check("5. verifier / structured output",
                  lambda: probe_verifier_structured(verifier, config.verifier_model))
        print()
        run_check("6. verifier / usefulness",
                  lambda: probe_verifier_usefulness(verifier, config.verifier_model))
        print()

    print("=" * 92)
    print("SUMMARY")
    for outcome in OUTCOMES:
        print(f"  [{'PASS' if outcome.passed else 'FAIL'}] {outcome.name}"
              + (f" - {outcome.detail}" if outcome.detail else ""))
    print("=" * 92)
    return 0


if __name__ == "__main__":
    sys.exit(main())
