"""The single gate every model call in this project passes through.

Nothing calls a provider SDK directly. A call goes through :func:`record_call`, which
appends one JSON line to ``evaluation/usage_log.jsonl`` before returning — so a crash
mid-run loses at most the call in flight, not the accounting. ``usage_report.md`` is
generated from that log by :func:`write_usage_report` after the final run.

The report covers providers and model names, call counts, input and output tokens,
totals and per-request averages, and estimated cost, with a per-model breakdown whenever
more than one model family is in use. Verifier calls are real billable calls and are
logged exactly like the primary's.
"""

import json
import threading
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, TypeVar

from config import (
    MODEL_COST_PER_MILLION_TOKENS,
    USAGE_LOG_PATH,
    USAGE_REPORT_CURRENCY,
    USAGE_REPORT_PATH,
    cost_for,
)

T = TypeVar("T")

# Roles a call can be made in; the report breaks costs down by these.
ROLE_PRIMARY = "primary"
ROLE_VERIFIER = "verifier"

# Serializes appends from extraction worker threads.
_APPEND_LOCK = threading.Lock()


@dataclass
class CallRecord:
    """One model call: what it cost and what it was for."""

    timestamp: str
    role: str
    provider: str
    model: str
    tag: str
    input_tokens: int
    output_tokens: int
    latency_seconds: float
    estimated_cost: Optional[float]
    ok: bool
    request_id: Optional[str] = None
    error: str = ""
    attempts: int = 1


@dataclass
class UsageLog:
    """Append-only usage log backed by a JSONL file.

    Each :meth:`append` flushes to disk immediately; the in-memory list is a
    convenience for the current process only.
    """

    path: Path = USAGE_LOG_PATH
    records: list[CallRecord] = field(default_factory=list)

    def append(self, record: CallRecord) -> None:
        """Add a record and flush it to disk as one JSON line.

        Serialized by a lock: extraction runs calls on worker threads, and interleaved
        writes would corrupt the accounting.
        """
        with _APPEND_LOCK:
            self.records.append(record)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")

    def load(self) -> list[CallRecord]:
        """Read every record previously written to disk, ignoring corrupt lines."""
        if not self.path.is_file():
            return []
        loaded: list[CallRecord] = []
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    loaded.append(CallRecord(**json.loads(line)))
                except (json.JSONDecodeError, TypeError):
                    continue
        return loaded

    def reset(self) -> None:
        """Delete the log. Use only when starting a fresh full run."""
        self.records.clear()
        if self.path.is_file():
            self.path.unlink()


# The process-wide log. Tests construct their own UsageLog against a tmp path.
USAGE_LOG = UsageLog()


def record_call(
    call: Callable[[], T],
    extract_usage: Callable[[T], tuple[int, int]],
    *,
    role: str,
    provider: str,
    model: str,
    tag: str,
    request_id: Optional[str] = None,
    attempts: int = 1,
    log: Optional[UsageLog] = None,
) -> T:
    """Run one model call, log its usage, and return its result.

    `call` performs the request; `extract_usage` pulls (input_tokens, output_tokens)
    out of whatever it returns, so this module stays provider-agnostic. `tag` says
    what the call was for (e.g. "image_extraction", "explanation", "verify").

    A failure is logged with ok=False and the exception re-raised — the caller's retry
    policy decides what happens next, and every attempt appears in the log.
    """
    target = log if log is not None else USAGE_LOG
    started = time.monotonic()
    try:
        result = call()
    except Exception as error:
        target.append(
            CallRecord(
                timestamp=datetime.now(timezone.utc).isoformat(),
                role=role,
                provider=provider,
                model=model,
                tag=tag,
                input_tokens=0,
                output_tokens=0,
                latency_seconds=time.monotonic() - started,
                estimated_cost=None,
                ok=False,
                request_id=request_id,
                error=f"{type(error).__name__}: {error}",
                attempts=attempts,
            )
        )
        raise
    latency = time.monotonic() - started
    input_tokens, output_tokens = extract_usage(result)
    target.append(
        CallRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            role=role,
            provider=provider,
            model=model,
            tag=tag,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_seconds=latency,
            estimated_cost=cost_for(model, input_tokens, output_tokens),
            ok=True,
            request_id=request_id,
            attempts=attempts,
        )
    )
    return result


@dataclass
class ModelTotals:
    """Aggregated usage for one model."""

    provider: str = ""
    model: str = ""
    calls: int = 0
    failed_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    cost_known: bool = True

    @property
    def total_tokens(self) -> int:
        """Input plus output tokens."""
        return self.input_tokens + self.output_tokens


def summarize(records: Iterable[CallRecord]) -> dict[str, ModelTotals]:
    """Aggregate call records by model name, in first-seen order."""
    totals: dict[str, ModelTotals] = {}
    for record in records:
        entry = totals.setdefault(
            record.model, ModelTotals(provider=record.provider, model=record.model)
        )
        entry.calls += 1
        if not record.ok:
            entry.failed_calls += 1
        entry.input_tokens += record.input_tokens
        entry.output_tokens += record.output_tokens
        if record.estimated_cost is None:
            if record.model not in MODEL_COST_PER_MILLION_TOKENS:
                entry.cost_known = False
        else:
            entry.cost += record.estimated_cost
    return totals


def _format_money(value: float) -> str:
    """Render a cost with enough precision to be useful at these volumes."""
    return f"{value:.4f}"


def render_usage_report(records: list[CallRecord], request_count: int) -> str:
    """Build the markdown body of evaluation/usage_report.md.

    `request_count` is the number of dataset requests the run covered, used for the
    per-request averages.
    """
    by_model = summarize(records)
    grand = ModelTotals(provider="all", model="all")
    for entry in by_model.values():
        grand.calls += entry.calls
        grand.failed_calls += entry.failed_calls
        grand.input_tokens += entry.input_tokens
        grand.output_tokens += entry.output_tokens
        grand.cost += entry.cost
        grand.cost_known = grand.cost_known and entry.cost_known

    def per_request(value: float) -> str:
        return f"{value / request_count:,.1f}" if request_count else "n/a"

    lines = [
        "# Token usage and cost",
        "",
        f"Generated from `evaluation/usage_log.jsonl`, covering **{request_count}** requests "
        f"and **{grand.calls}** model calls.",
        "",
        "## Per model",
        "",
        "| provider | model | role(s) | calls | input tokens | output tokens | total tokens |"
        f" est. cost ({USAGE_REPORT_CURRENCY}) |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    roles_by_model: dict[str, set[str]] = defaultdict(set)
    for record in records:
        roles_by_model[record.model].add(record.role)
    for entry in by_model.values():
        cost = _format_money(entry.cost) if entry.cost_known else "unknown"
        lines.append(
            f"| {entry.provider} | {entry.model} | {', '.join(sorted(roles_by_model[entry.model]))}"
            f" | {entry.calls:,} | {entry.input_tokens:,} | {entry.output_tokens:,}"
            f" | {entry.total_tokens:,} | {cost} |"
        )
    lines += [
        "",
        "## Overall",
        "",
        f"- Model calls: **{grand.calls:,}**"
        + (f" ({grand.failed_calls:,} failed)" if grand.failed_calls else ""),
        f"- Input tokens: **{grand.input_tokens:,}**",
        f"- Output tokens: **{grand.output_tokens:,}**",
        f"- Total tokens: **{grand.total_tokens:,}**",
        f"- Average tokens per request: **{per_request(grand.total_tokens)}**",
        f"- Average calls per request: **{per_request(grand.calls)}**",
        f"- Estimated total cost: **{USAGE_REPORT_CURRENCY} "
        + (_format_money(grand.cost) if grand.cost_known else "unknown")
        + "**",
        f"- Estimated cost per request: **{USAGE_REPORT_CURRENCY} "
        + (
            _format_money(grand.cost / request_count)
            if grand.cost_known and request_count
            else "unknown"
        )
        + "**",
        "",
        "## By call purpose",
        "",
        "| tag | calls | input tokens | output tokens |",
        "|---|---:|---:|---:|",
    ]
    by_tag: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for record in records:
        entry_tag = by_tag[record.tag]
        entry_tag[0] += 1
        entry_tag[1] += record.input_tokens
        entry_tag[2] += record.output_tokens
    for tag, (calls, tokens_in, tokens_out) in sorted(by_tag.items()):
        lines.append(f"| {tag} | {calls:,} | {tokens_in:,} | {tokens_out:,} |")
    if not by_tag:
        lines.append("| (no calls recorded) | 0 | 0 | 0 |")
    lines += [
        "",
        "No API keys, credentials or configuration values appear in this report.",
        "",
    ]
    return "\n".join(lines)


def write_usage_report(
    request_count: int, log: Optional[UsageLog] = None, path: Path = USAGE_REPORT_PATH
) -> Path:
    """Render the usage report from the persisted log and write it to `path`."""
    source = log if log is not None else USAGE_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_usage_report(source.load(), request_count), encoding="utf-8")
    return path
