"""Compare image readings with the research prototype's claimed values — evaluation only.

The claims below come from the pre-implementation image analysis and are treated
as claims to verify, not labels. The pipeline never reads this file. Any disagreement is
settled by looking at the image and recording the verdict in DATASET_FACTS.md.

Run:
    python code/evaluation/image_extraction_check.py [--extract]
"""

import argparse
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.loader import load_dataset  # noqa: E402
from extraction.client import PrimaryClient  # noqa: E402
from extraction.images import extract_images, load_image_cache, resolve_amount  # noqa: E402

RESEARCH_CLAIMS = {
    "event_253": "4365000", "event_1442": "100000", "event_1545": "41272", "event_1700": "2854",
    "event_1786": "704.05", "event_3051": "1995", "event_3231": "8528.10", "event_4535": "15339",
    "event_5170": "723", "event_6033": "79679.26", "event_6859": "3650", "event_7307": "33.50",
    "event_7941": "2298", "event_9421": "4543", "event_9806": "9968", "event_10521": "393.22",
}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # document labels include symbols such as ₹
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extract", action="store_true", help="read uncached images with the model first")
    arguments = parser.parse_args()
    dataset = load_dataset()
    events_by_id = {event.event_id: event for events in dataset.events_by_user.values() for event in events}
    if arguments.extract:
        readings = extract_images(dataset.images_by_event, events_by_id, PrimaryClient())
    else:
        readings = load_image_cache()
    agree = 0
    for event_id in sorted(RESEARCH_CLAIMS, key=lambda item: int(item.split("_")[1])):
        reading = readings.get(event_id)
        event = events_by_id[event_id]
        used = resolve_amount(reading, event) if reading else None
        claim = Decimal(RESEARCH_CLAIMS[event_id])
        ok = used is not None and used == claim
        agree += ok
        seen = "; ".join(f"{label}={amount}" for label, amount in (reading.amounts_seen if reading else ()))
        print(f"{'OK ' if ok else 'DIFF'} {event_id:12} {event.description[:34]:34} {event.status:9} used={used} claim={claim} "
              f"chosen={reading.chosen_amount if reading else None} ({reading.chosen_label if reading else ''}, {reading.confidence if reading else ''}, lang={reading.language if reading else ''})")
        if not ok:
            print(f"      seen: {seen}")
    print(f"\nagree with research claims: {agree}/{len(RESEARCH_CLAIMS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
