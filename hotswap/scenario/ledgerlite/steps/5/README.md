# ledgerlite

A tiny command line tool for tracking invoices.

## Usage

```
python -m ledger list
python -m ledger add --customer "Acme" --date 2026-10-12 --amount 1200 --status open
```

### Export to CSV

```
python -m ledger export --from 2026-10-01 --to 2026-10-31
python -m ledger export --from 2026-10-01 --to 2026-10-31 --status open
```

Both dates are inclusive. Rows are sorted by date. An invalid date exits with code 2.

Invoices are stored in `data/invoices.json`. Set `LEDGER_DATA` to use another file.
