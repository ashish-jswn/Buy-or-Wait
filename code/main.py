"""Pipeline entry point: every request in dataset/requests.csv → output.csv.

This file only wires modules together; each step lives in its own module:

1. load the dataset                                  (data/)
2. read messages: language, then facts; cached       (extraction/messages.py — model calls)
3. decide each request                               (engine/pipeline.py — deterministic)
4. explain, check, fall back on a failed check       (output/)
5. write output.csv and evaluation/usage_report.md

A row that fails a check, or whose decision raises, is replaced by a safe not_affordable
row and counted (author decision), so a submission file always exists and every failure
is printed.

Run:
    python code/main.py                     full run → output.csv at the repo root
    python code/main.py --limit 5           first N requests only
    python code/main.py --dry-run           everything except writing files
    python code/main.py --sample            the 25 labelled samples → evaluation/sample_predictions.csv
    python code/main.py --no-extract        use cached message readings only (no model calls)
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.formatting import format_safe_amount  # noqa: E402
from config import (  # noqa: E402
    EVALUATION_DIR,
    MESSAGE_FACTS_CACHE_PATH,
    PREDICTIONS_OUTPUT_PATH,
    REQUESTS_PATH,
    SAFETY_TRACE_PATH,
)
from data.loader import load_dataset, load_sample_dataset  # noqa: E402
from engine.pipeline import spending_changes_text  # noqa: E402
from engine.safety import OUTCOME_DOWNGRADED, OUTCOME_HELD, OUTCOME_REQUIRED_CHANGE, safe_decide_request  # noqa: E402
from extraction.client import PrimaryClient  # noqa: E402
from extraction.images import extract_images, load_image_cache, resolve_amount  # noqa: E402
from extraction.messages import (  # noqa: E402
    SOURCE_FAILED,
    extract_messages,
    injection_phrase,
    load_cache,
    readings_from_cache,
)
from extraction.usage import write_usage_report  # noqa: E402
from output.explanations import explain, not_affordable_explanation  # noqa: E402
from output.guards import check_row, fallback_row  # noqa: E402
from output.rows import build_output_row, write_output_csv  # noqa: E402

SAMPLE_OUTPUT_PATH = EVALUATION_DIR / "sample_predictions.csv"


def run(
    sample: bool = False,
    out: Optional[Path] = None,
    extract: bool = True,
    limit: Optional[int] = None,
    dry_run: bool = False,
    requests_path: Path = REQUESTS_PATH,
) -> int:
    """Run the pipeline and return a process exit code (0 on success)."""
    dataset = load_sample_dataset() if sample else load_dataset(requests_path)
    requests = dataset.requests[:limit] if limit else list(dataset.requests)
    out = out or (SAMPLE_OUTPUT_PATH if sample else PREDICTIONS_OUTPUT_PATH)

    users = {request.user_id for request in requests}
    messages = [message for user in sorted(users) for message in dataset.messages_for(user)]
    if extract:
        readings = extract_messages(messages, PrimaryClient(), dataset.profiles)
    else:
        readings = readings_from_cache(load_cache(MESSAGE_FACTS_CACHE_PATH))
    # Injections are discarded by rule and carry no reading or cache entry by design.
    unread = [
        message.message_id
        for message in messages
        if injection_phrase(message.message_text) is None
        and (message.message_id not in readings or readings[message.message_id].source == SOURCE_FAILED)
    ]

    events_by_id = {event.event_id: event for events in dataset.events_by_user.values() for event in events}
    descriptions = {event_id: event.description for event_id, event in events_by_id.items()}

    images = {event_id: image for event_id, image in dataset.images_by_event.items() if image.user_id in users}
    if extract:
        image_readings = extract_images(images, events_by_id, PrimaryClient())
    else:
        image_readings = {key: value for key, value in load_image_cache().items() if key in images}
    image_amounts = {
        event_id: amount
        for event_id, reading in image_readings.items()
        if (amount := resolve_amount(reading, events_by_id[event_id])) is not None
    }

    rows, fallbacks, mix, traces = [], [], Counter(), []
    for request in requests:
        profile = dataset.profiles[request.user_id]
        options = dataset.options_by_request.get(request.request_id, [])
        amount_safe = "0"
        try:
            decision, trace = safe_decide_request(request, dataset, readings, image_amounts)
            traces.append(trace)
            amount_safe = format_safe_amount(max(0, min(decision.amount_safe_to_pay, request.requested_amount)))
            row = build_output_row(
                request.request_id, decision, spending_changes_text(decision), explain(request, profile, decision, descriptions)
            )
            problems = check_row(row, request, profile, options, events_by_id)
            safe_for_text = decision.amount_safe_to_pay
        except Exception as error:  # noqa: BLE001 - a failed row is replaced and counted, never fatal
            problems = [f"decision raised {type(error).__name__}: {error}"]
            safe_for_text = 0
        if problems:
            fallbacks.append((request.request_id, problems))
            row = fallback_row(request, amount_safe, not_affordable_explanation(request, profile, safe_for_text))
            second = check_row(row, request, profile, options, events_by_id)
            if second:
                raise RuntimeError(f"fallback row for {request.request_id} failed its own checks: {second}")
        mix[(row["affordability_status"], row["recommended_payment_method"])] += 1
        rows.append(row)

    if len({row["request_id"] for row in rows}) != len(requests):
        raise RuntimeError("output must have exactly one row per request_id")

    print(f"requests {len(requests)}  messages {len(messages)} (unread {len(unread)})  images {len(images)} (amount resolved {len(image_amounts)})")
    for (status, method), count in sorted(mix.items()):
        print(f"  {count:3d}  {status} / {method}")
    print(f"rows replaced by fallback: {len(fallbacks)}")
    for request_id, problems in fallbacks:
        print(f"  {request_id}: {'; '.join(problems)}")
    held = [trace.request_id for trace in traces if trace.outcome == OUTCOME_HELD]
    downgraded = [trace for trace in traces if trace.outcome in (OUTCOME_DOWNGRADED, OUTCOME_REQUIRED_CHANGE)]
    required = [trace for trace in traces if trace.outcome == OUTCOME_REQUIRED_CHANGE]
    message_income = [trace for trace in traces if trace.outcome == "downgraded_without_message_income"]
    print(f"message-raised income stress: downgraded {len(message_income)}")
    print(
        f"safety check: near-floor plans {len(held) + len(downgraded)}, held under stress {len(held)}, "
        f"downgraded {len(downgraded) - len(required)}, marginal pay-now given a spending change {len(required)}"
    )
    for trace in downgraded:
        print(f"  {trace.request_id}: {trace.before[0]}/{trace.before[1]} -> {trace.after[0]}/{trace.after[1]} (margin {trace.margin:.2f} < {trace.threshold:.2f})")

    if dry_run:
        print("dry run: nothing written")
        return 0
    trace_path = SAFETY_TRACE_PATH if not sample else EVALUATION_DIR / "sample_safety_trace.jsonl"
    with trace_path.open("w", encoding="utf-8") as handle:
        for trace in traces:
            handle.write(json.dumps({
                "request_id": trace.request_id, "flags": list(trace.flags),
                "margin": None if trace.margin is None else str(trace.margin), "threshold": str(trace.threshold),
                "outcome": trace.outcome, "before": list(trace.before), "after": list(trace.after),
            }) + "\n")
    write_output_csv(rows, out)
    print(f"wrote {len(rows)} rows to {out}")
    if not sample and not limit:
        print(f"wrote {write_usage_report(len(requests))}")
    return 0


def parse_arguments(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments for the pipeline."""
    parser = argparse.ArgumentParser(description="Buy or Wait? — produce output.csv for every request.")
    parser.add_argument("--requests", default=str(REQUESTS_PATH), help="requests CSV to answer")
    parser.add_argument("--out", default=None, help="where to write the CSV")
    parser.add_argument("--limit", type=int, default=None, help="process only the first N requests")
    parser.add_argument("--dry-run", action="store_true", help="process without writing files")
    parser.add_argument("--sample", action="store_true", help="run the 25 labelled samples instead")
    parser.add_argument("--no-extract", action="store_true", help="use cached message readings only")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    """Run the pipeline from the command line."""
    arguments = parse_arguments(argv)
    return run(
        sample=arguments.sample,
        out=Path(arguments.out) if arguments.out else None,
        extract=not arguments.no_extract,
        limit=arguments.limit,
        dry_run=arguments.dry_run,
        requests_path=Path(arguments.requests),
    )


if __name__ == "__main__":
    sys.exit(main())
