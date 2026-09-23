"""Rule-based message oracle — evaluation only, never imported by the pipeline.

Every message template seen in messages.csv, matched by bilingual key phrases. It
exists for two jobs: verify the intent inventory with real counts (DATASET_FACTS G1), and
score the LLM message extractor against an independent reading. The pipeline itself uses
the LLM (DECISIONS: messages are read by the model, applied by code).

Run:
    python code/evaluation/message_oracle.py
"""

import csv
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import MESSAGES_PATH  # noqa: E402

# (intent, pattern). A message may match more than one (message_86 does).
TEMPLATES: list[tuple[str, str]] = [
    ("injection", r"release charge|biaya pencairan"),
    ("salary_increase", r"salary has increased to|gaji bulanan anda naik menjadi"),
    ("salary_next_with_arrears", r"one-time arrears adjustment|penyesuaian tunggakan satu kali"),
    ("salary_next_confirmed_no_amount", r"gaji rutin untuk penggajian berikutnya sudah dikonfirmasi"),
    ("temporary_pay", r"temporary monthly pay is|gaji bulanan sementara"),
    ("next_salary_reduced", r"next salary is reduced to"),
    ("payday_moved", r"now expected on|kini diperkirakan masuk pada"),
    ("payout_pending", r"payout is still pending|pembayaran berikutnya dari \S+ masih tertunda"),
    ("base_salary_commission_pending", r"confirmed base salary is|gaji pokok yang dikonfirmasi"),
    ("seasonal_contract_ended", r"seasonal contract has ended|kontrak musiman saat ini telah berakhir"),
    ("employment_ended", r"your employment has ended|hubungan kerja anda telah berakhir"),
    ("salary_resumes_with_childcare", r"resumes on"),
    ("first_salary", r"first salary|gaji pertama"),
    ("rent_increase_pct", r"increases monthly rent by|menaikkan biaya sewa bulanan sebesar"),
    ("internal_transfer", r"transfer between your two accounts|transfer antara dua rekening"),
    ("refund_pending", r"refund has been initiated|pengembalian dana sudah diproses"),
    ("investment_value_unrealized", r"displayed market value|displayed value of the investment|nilai investasi yang ditampilkan"),
    ("prize_claim_processing", r"still in payment processing|masih dalam proses pembayaran"),
    ("prize_proceeds_settled", r"prize proceeds have reached"),
    ("invoice_approved", r"approved an invoice payment|menyetujui pembayaran faktur"),
    ("remaining_household_salary", r"remaining confirmed monthly salary|sisa gaji bulanan"),
    ("fx_salary_confirmed", r"your salary of [A-Z]{3} [\d.]+ is confirmed for|gaji sebesar [A-Z]{3} [\d.]+ dikonfirmasi untuk|confirmed a [A-Z]{3} [\d.]+ salary credit"),
    ("bonus_pending", r"quarterly bonus is still subject|bonus kuartalan anda masih menunggu"),
    ("failed_debit_retry", r"previous debit attempt failed"),
    ("dispute_open", r"still being investigated|masih dalam penyelidikan"),
    ("investment_sale_settled", r"investment sale have settled|hasil penjualan investasi"),
    ("reimbursement_closed", r"reimbursement for your earlier work expense|penggantian atas biaya kerja"),
    ("fx_refund_processing", r"foreign-currency refund is still processing"),
    ("fx_bill_pending", r"charged in a foreign currency|dikenakan dalam mata uang asing"),
    ("two_card_minimums", r"two separate card accounts"),
    ("receipt_has_amount", r"receipt (has|contains) the final"),
]

INDONESIAN_MARKERS = re.compile(r"\b(anda|yang|dan|untuk|telah|dari|adalah|belum)\b", re.IGNORECASE)
AMOUNT = re.compile(r"\b([A-Z]{3}) (\d+(?:\.\d+)?)\b")
ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
PERCENT = re.compile(r"(\d+(?:\.\d+)?)%")


def read_message(text: str) -> dict:
    """The oracle's reading of one message: language, intents, amounts, dates, percent."""
    intents = [name for name, pattern in TEMPLATES if re.search(pattern, text, re.IGNORECASE)]
    percent: Optional[str] = next(iter(PERCENT.findall(text)), None)
    return {
        "language": "id" if INDONESIAN_MARKERS.search(text) else "en",
        "intents": intents,
        "amounts": [(currency, value) for currency, value in AMOUNT.findall(text)],
        "dates": ISO_DATE.findall(text),
        "percent": percent,
    }


def main() -> int:
    """Print template counts, language counts, and any message matching zero templates."""
    with open(MESSAGES_PATH, encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    intents: Counter = Counter()
    languages: Counter = Counter()
    currencies: Counter = Counter()
    unmatched, multi = [], []
    for row in rows:
        reading = read_message(row["message_text"])
        languages[reading["language"]] += 1
        for currency, _ in reading["amounts"]:
            currencies[currency] += 1
        for name in reading["intents"]:
            intents[name] += 1
        if not reading["intents"]:
            unmatched.append(row["message_id"])
        if len(reading["intents"]) > 1:
            multi.append((row["message_id"], reading["intents"]))
    print(f"messages {len(rows)}  languages {dict(languages)}  amount currencies {dict(currencies)}")
    for name, count in intents.most_common():
        print(f"  {count:3d}  {name}")
    print("unmatched:", unmatched)
    print("multi-intent:", multi)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
