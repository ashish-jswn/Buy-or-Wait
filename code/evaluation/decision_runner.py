"""Sample-set predictions through the real pipeline, for scoring during development.

Thin wrapper over ``main.run`` with the 25 labelled samples, writing to the evaluation
folder instead of the repo-root output.csv. Makes no model call when messages are cached.

Run:
    python code/evaluation/decision_runner.py [--full] [--out <csv>]
"""

import argparse
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import run  # noqa: E402

DEFAULT_OUTPUT_PATH = Path(__file__).resolve().parent / "decision_predictions.csv"


def main(argv: Optional[list[str]] = None) -> int:
    """Run the pipeline on the samples (or the full set) and write predictions."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(DEFAULT_OUTPUT_PATH))
    parser.add_argument("--full", action="store_true", help="run over dataset/requests.csv")
    arguments = parser.parse_args(argv)
    return run(sample=not arguments.full, out=Path(arguments.out), extract=False)


if __name__ == "__main__":
    sys.exit(main())
