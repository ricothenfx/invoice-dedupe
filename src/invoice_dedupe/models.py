"""Core data models: Invoice, NormalizedInvoice, PairResult."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

FLAG = "flag"
REVIEW = "review"
PASS = "pass"


@dataclass(frozen=True)
class Invoice:
    id: str
    vendor_name: str
    invoice_no: str
    amount: int
    currency: str = "USD"
    invoice_date: date | None = None
    tax_id: str = ""
    duplicate_of: str | None = None
    variant: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "vendor_name": self.vendor_name,
            "invoice_no": self.invoice_no,
            "amount": self.amount,
            "currency": self.currency,
            "invoice_date": self.invoice_date.isoformat() if self.invoice_date else None,
            "tax_id": self.tax_id,
            "duplicate_of": self.duplicate_of,
            "variant": self.variant,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Invoice":
        raw_date = d.get("invoice_date")
        return cls(
            id=d["id"],
            vendor_name=d.get("vendor_name", ""),
            invoice_no=d.get("invoice_no", ""),
            amount=int(d["amount"]),
            currency=d.get("currency", "USD"),
            invoice_date=date.fromisoformat(raw_date) if raw_date else None,
            tax_id=d.get("tax_id", ""),
            duplicate_of=d.get("duplicate_of"),
            variant=d.get("variant"),
        )


@dataclass(frozen=True)
class NormalizedInvoice:
    id: str
    invoice: Invoice
    vendor: str
    vendor_key: str
    invoice_no: str
    amount: int | None
    date: date | None
    tax_id: str
    billing_period: str


@dataclass(frozen=True)
class PairResult:
    id_a: str
    id_b: str
    score: float
    label: str
    breakdown: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id_a": self.id_a,
            "id_b": self.id_b,
            "score": round(self.score, 4),
            "label": self.label,
            "breakdown": {
                k: (round(v, 4) if isinstance(v, float) else v)
                for k, v in self.breakdown.items()
            },
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PairResult":
        return cls(
            id_a=d["id_a"],
            id_b=d["id_b"],
            score=float(d["score"]),
            label=d["label"],
            breakdown=dict(d.get("breakdown", {})),
        )
