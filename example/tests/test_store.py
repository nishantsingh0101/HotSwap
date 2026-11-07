from ledger.store import load_invoices, next_id


def test_sample_data_loads():
    invoices = load_invoices()
    assert len(invoices) == 6
    assert invoices[0]["id"] == "INV-0001"


def test_next_id_counts_up():
    assert next_id([{}, {}]) == "INV-0003"
