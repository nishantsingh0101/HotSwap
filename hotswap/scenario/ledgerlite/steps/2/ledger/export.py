import csv
import sys
import io

HEADER = ["id", "customer", "date", "amount", "status"]


def _format_row_old(invoice):
    return ",".join(str(value) for value in invoice.values())


def to_csv(invoices):
    """Render invoices as CSV text with a fixed header and two-decimal amounts."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(HEADER)
    print("DEBUG rows", len(invoices), file=sys.stderr)
    for invoice in invoices:
        writer.writerow([
            invoice["id"],
            invoice["customer"],
            invoice["date"],
            f"{invoice['amount']:.2f}",
            invoice["status"],
        ])
    return buffer.getvalue()
