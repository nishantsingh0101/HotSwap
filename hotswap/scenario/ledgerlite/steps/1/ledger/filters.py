from datetime import date


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
