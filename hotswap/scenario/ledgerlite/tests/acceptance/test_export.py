"""Acceptance tests written from the task request, before any implementation."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SAMPLE = [
    {"id": "INV-0001", "customer": "Acme Foods", "date": "2026-09-30", "amount": 900.0, "status": "paid"},
    {"id": "INV-0002", "customer": "Blue Harbor", "date": "2026-10-01", "amount": 1250.5, "status": "open"},
    {"id": "INV-0003", "customer": "Cedar Labs", "date": "2026-10-15", "amount": 480.0, "status": "paid"},
    {"id": "INV-0004", "customer": "Acme Foods", "date": "2026-10-31", "amount": 2200.0, "status": "open"},
    {"id": "INV-0005", "customer": "Delta Print", "date": "2026-11-01", "amount": 75.25, "status": "paid"},
    {"id": "INV-0006", "customer": "Blue Harbor", "date": "2026-10-09", "amount": 333.333, "status": "paid"},
]


def run_cli(*args):
    env = dict(os.environ, LEDGER_DATA=str(ROOT / "data" / "invoices.json"), PYTHONPATH=str(ROOT))
    return subprocess.run(
        [sys.executable, "-m", "ledger", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30
    )


def test_filter_range_is_inclusive():
    from datetime import date

    from ledger.filters import filter_invoices

    ids = [i["id"] for i in filter_invoices(SAMPLE, date(2026, 10, 1), date(2026, 10, 31))]
    assert "INV-0002" in ids and "INV-0004" in ids
    assert "INV-0001" not in ids and "INV-0005" not in ids


def test_filter_by_status():
    from ledger.filters import filter_invoices

    kept = filter_invoices(SAMPLE, status="paid")
    assert {i["status"] for i in kept} == {"paid"}
    assert len(kept) == 4


def test_filter_sorted_by_date_then_id():
    from ledger.filters import filter_invoices

    keys = [(i["date"], i["id"]) for i in filter_invoices(SAMPLE)]
    assert keys == sorted(keys)


def test_csv_header_and_rows():
    from ledger.export import to_csv

    lines = to_csv(SAMPLE[:2]).splitlines()
    assert lines[0] == "id,customer,date,amount,status"
    assert lines[1] == "INV-0001,Acme Foods,2026-09-30,900.00,paid"


def test_csv_amount_two_decimals():
    from ledger.export import to_csv

    text = to_csv([SAMPLE[5]])
    assert "333.33" in text
    assert "333.333" not in text


def test_cli_export_prints_csv():
    result = run_cli("export", "--from", "2026-10-01", "--to", "2026-10-31")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "id,customer,date,amount,status"
    assert [line.split(",")[0] for line in lines[1:]] == ["INV-0002", "INV-0006", "INV-0003", "INV-0004"]


def test_cli_export_status_filter():
    result = run_cli("export", "--from", "2026-10-01", "--to", "2026-10-31", "--status", "open")
    assert result.returncode == 0, result.stderr
    assert [line.split(",")[0] for line in result.stdout.strip().splitlines()[1:]] == ["INV-0002", "INV-0004"]


def test_cli_invalid_date_exits_2():
    result = run_cli("export", "--from", "2026-13-01")
    assert result.returncode == 2
    assert "2026-13-01" in result.stderr


def test_cli_empty_range_prints_header_only():
    result = run_cli("export", "--from", "2030-01-01", "--to", "2030-01-31")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "id,customer,date,amount,status"


def test_readme_documents_export():
    assert "ledger export" in (ROOT / "README.md").read_text(encoding="utf-8")
