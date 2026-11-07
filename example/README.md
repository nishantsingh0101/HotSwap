# ledgerlite

A tiny command line tool for tracking invoices.

## Usage

```
python -m ledger list
python -m ledger add --customer "Acme" --date 2026-10-12 --amount 1200 --status open
```

Invoices are stored in `data/invoices.json`. Set `LEDGER_DATA` to use another file.
