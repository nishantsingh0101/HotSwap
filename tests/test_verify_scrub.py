import shutil
from pathlib import Path

from hotswap.config import EXAMPLE_DIR, SCENARIO_DIR
from hotswap.scrub import find_debris
from hotswap.verify import mutate_source

TASK_DIR = SCENARIO_DIR / "ledgerlite"


def build_repo(tmp: Path, steps=(1, 2, 3, 4, 5)) -> Path:
    repo = tmp / "repo"
    shutil.copytree(EXAMPLE_DIR, repo)
    for step in steps:
        source = TASK_DIR / "steps" / str(step)
        for file in source.rglob("*"):
            if file.is_file():
                target = repo / file.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file, target)
    return repo


def test_scrubber_finds_the_debris_left_in_the_recorded_run(tmp_path):
    repo = build_repo(tmp_path)
    export = (repo / "ledger" / "export.py").read_text().splitlines()
    added = {
        "ledger/export.py": export,
        "export_scratch.tmp": ["scratch notes from export work"],
        "ledger/config.py": ["EXPORT_DEBUG = True"],
    }
    plan_files = {"ledger/filters.py", "ledger/export.py", "ledger/cli.py", "README.md"}
    found = find_debris(repo, added, plan_files, {"to_csv", "HEADER", "filter_invoices", "parse_day", "cmd_export"})
    reasons = {p["reason"] for p in found}
    assert "debug output" in reasons
    assert "unused helper _format_row_old()" in reasons
    assert "unused import sys" in reasons
    assert any("scratch file" in r for r in reasons)
    assert any("outside the plan" in r for r in reasons)


def test_scrubber_leaves_clean_code_alone(tmp_path):
    repo = build_repo(tmp_path, steps=(1,))
    added = {"ledger/filters.py": (repo / "ledger" / "filters.py").read_text().splitlines()}
    assert find_debris(repo, added, {"ledger/filters.py"}, {"filter_invoices"}) == []


def test_mutation_flips_an_inclusive_bound():
    source = "def f(a, b):\n    return a < b\n"
    mutants = [mutate_source(source, i) for i in range(2)]
    flipped = [m for m in mutants if m and "Lt -> LtE" in m[1]]
    assert flipped and "<=" in flipped[0][0]
    assert mutate_source(source, 2) is None
