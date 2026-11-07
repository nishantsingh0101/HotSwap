import argparse
from datetime import date

from ledger.export import to_csv
from ledger.filters import filter_invoices
from ledger.store import load_invoices, next_id, save_invoices


def cmd_list(args: argparse.Namespace) -> int:
    for inv in load_invoices():
        print(f"{inv['id']}  {inv['date']}  {inv['customer']:<14} {inv['amount']:>10.2f}  {inv['status']}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    invoices = load_invoices()
    invoice = {
        "id": next_id(invoices),
        "customer": args.customer,
        "date": args.date,
        "amount": float(args.amount),
        "status": args.status,
    }
    invoices.append(invoice)
    save_invoices(invoices)
    print(f"added {invoice['id']}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    invoices = filter_invoices(load_invoices(), start, end, args.status)
    print(to_csv(invoices), end="")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledger")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list all invoices").set_defaults(func=cmd_list)

    add = sub.add_parser("add", help="add an invoice")
    add.add_argument("--customer", required=True)
    add.add_argument("--date", required=True)
    add.add_argument("--amount", required=True)
    add.add_argument("--status", choices=["open", "paid"], default="open")
    add.set_defaults(func=cmd_add)

    export = sub.add_parser("export", help="print invoices as CSV")
    export.add_argument("--from", dest="start")
    export.add_argument("--to", dest="end")
    export.add_argument("--status", choices=["open", "paid"])
    export.set_defaults(func=cmd_export)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
