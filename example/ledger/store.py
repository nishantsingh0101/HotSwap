import json
import os
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "invoices.json"


def data_path() -> Path:
    return Path(os.environ.get("LEDGER_DATA", DEFAULT_PATH))


def load_invoices() -> list[dict]:
    path = data_path()
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def save_invoices(invoices: list[dict]) -> None:
    data_path().write_text(json.dumps(invoices, indent=2) + "\n", encoding="utf-8")


def next_id(invoices: list[dict]) -> str:
    return f"INV-{len(invoices) + 1:04d}"
