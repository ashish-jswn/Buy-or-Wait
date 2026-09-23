"""Score the LLM message readings against the independent rule oracle.

Checks, per message: language, intent set, amounts mentioned (currency + value), stated
ISO dates carried into facts, and the injection flag. Prints the rate for each and every
disagreement, so a wrong reading is visible before the engine consumes it.

Run:
    python code/evaluation/message_extraction_check.py            # compare cached readings
    python code/evaluation/message_extraction_check.py --extract  # read uncached messages first (model calls)
"""

import argparse
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import MESSAGE_FACTS_CACHE_PATH, MESSAGES_PATH  # noqa: E402
from data.loader import load_messages, load_profiles  # noqa: E402
from evaluation.message_oracle import read_message as oracle_read  # noqa: E402
from extraction.client import PrimaryClient  # noqa: E402
from extraction.messages import extract_messages, load_cache, readings_from_cache  # noqa: E402


def main() -> int:
    """Compare readings with the oracle and print per-check rates and disagreements."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extract", action="store_true", help="read uncached messages with the model first")
    args = parser.parse_args()

    messages = [message for group in load_messages(MESSAGES_PATH).values() for message in group]
    if args.extract:
        readings = extract_messages(messages, PrimaryClient(), load_profiles())
    else:
        readings = readings_from_cache(load_cache(MESSAGE_FACTS_CACHE_PATH))

    checks = {"read": 0, "language": 0, "intents": 0, "amounts": 0, "dates": 0, "injection": 0}
    problems: list[str] = []
    for message in sorted(messages, key=lambda item: int(item.message_id.split("_")[1])):
        reading = readings.get(message.message_id)
        oracle = oracle_read(message.message_text)
        if reading is None or reading.source == "failed":
            problems.append(f"{message.message_id}: not read ({reading.error if reading else 'missing'})")
            continue
        checks["read"] += 1
        oracle_injection = "injection" in oracle["intents"]
        if reading.is_instruction_attempt == oracle_injection:
            checks["injection"] += 1
        else:
            problems.append(f"{message.message_id}: injection flag {reading.is_instruction_attempt} vs oracle {oracle_injection}")
        if oracle_injection:
            for key in ("language", "intents", "amounts", "dates"):
                checks[key] += 1
            continue

        if reading.language == oracle["language"]:
            checks["language"] += 1
        else:
            problems.append(f"{message.message_id}: language {reading.language} vs oracle {oracle['language']}")

        model_intents = {fact.intent for fact in reading.facts}
        if model_intents == set(oracle["intents"]):
            checks["intents"] += 1
        else:
            problems.append(f"{message.message_id}: intents {sorted(model_intents)} vs oracle {sorted(oracle['intents'])}")

        model_amounts = sorted((currency, amount.normalize()) for amount, currency in reading.amounts_mentioned)
        oracle_amounts = sorted((currency, Decimal(value).normalize()) for currency, value in oracle["amounts"])
        if model_amounts == oracle_amounts:
            checks["amounts"] += 1
        else:
            problems.append(f"{message.message_id}: amounts {model_amounts} vs oracle {oracle_amounts}")

        fact_dates = {fact.effective_date.isoformat() for fact in reading.facts if fact.effective_date}
        if set(oracle["dates"]) <= fact_dates:
            checks["dates"] += 1
        else:
            problems.append(f"{message.message_id}: dates {sorted(fact_dates)} vs oracle {oracle['dates']}")

    total = len(messages)
    print(f"messages {total}")
    for key, passed in checks.items():
        print(f"  {key:10s} {passed:3d}/{total}  {passed / total:.1%}")
    print(f"\ndisagreements ({len(problems)}):")
    for line in problems:
        print("  " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
