from datetime import date


def parse_day(value):
    """Parse YYYY-MM-DD, with a clear message when the value is not a real date."""
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"invalid date {value!r}, expected YYYY-MM-DD") from None


def filter_invoices(invoices, start=None, end=None, status=None):
    """Keep invoices inside [start, end] (both inclusive) and with the given status."""
    kept = []
    for invoice in invoices:
        day = date.fromisoformat(invoice["date"])
        if start is not None and day < start:
            continue
        if end is not None and day > end:
            continue
        if status is not None and invoice["status"] != status:
            continue
        kept.append(invoice)
    return sorted(kept, key=lambda invoice: (invoice["date"], invoice["id"]))
