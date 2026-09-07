"""Historical static price table. Fixture numbers are not live quotes."""

import subprocess
import sys
import tomllib
from decimal import Decimal
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "issue15"
REPO = Path(__file__).resolve().parents[4]
GENERATOR = REPO / "scripts" / "gen_prices.py"


def _generate(out: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(GENERATOR),
            "--from-file",
            str(FIXTURES / "api.json"),
            "--out",
            str(out),
            "--snapshot-date",
            "2026-08-26",
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO,
    )


def test_generator_from_file_keeps_opencode_prices_from_crossing(tmp_path):
    out = tmp_path / "prices.toml"
    result = _generate(out)
    assert result.returncode == 0, result.stderr
    text = out.read_text()
    assert "Copyright (c) 2025 models.dev" in text
    assert "MIT" in text
    assert "Python-urllib" not in text
    data = tomllib.loads(text)
    go = data["endpoints"]["opencode-go"]["models"]["deepseek-v4-flash"]
    zen = data["endpoints"]["opencode"]["models"]["deepseek-v4-flash"]
    assert go["input"] == 0.22
    assert go["output"] == 0.66
    assert zen["input"] == 0.14
    assert zen["output"] == 0.28
    assert go["input"] != zen["input"]
    assert "no-cost" not in data["endpoints"]["opencode-go"]["models"]
    assert data["endpoints"]["opencode-go"]["models"]["gpt-5.1"]["tiered"] is True
    assert data["endpoints"]["opencode-go"]["models"]["free-demo"]["free"] is True
    assert "cache_write" not in go
    assert '"gpt-5.1"' in text


def test_static_book_quotes_by_endpoint_and_model(tmp_path):
    from agent_alfred.pricing import StaticPriceBook

    out = tmp_path / "prices.toml"
    assert _generate(out).returncode == 0
    book = StaticPriceBook.load(out)
    go = book.quote("opencode-go", "deepseek-v4-flash", "uncached_input")
    zen = book.quote("opencode", "deepseek-v4-flash", "uncached_input")
    assert go is not None and zen is not None
    assert go.unit_price == Decimal("0.22")
    assert zen.unit_price == Decimal("0.14")
    assert go.unit_price != zen.unit_price
    assert go.source == "model_static"
    dotted = book.quote("opencode-go", "gpt-5.1", "output")
    assert dotted is not None
    assert dotted.tiered is True
    assert book.quote("opencode-go", "missing", "output") is None
    free = book.quote("opencode-go", "free-demo", "uncached_input")
    assert free is not None
    assert free.source == "free_rule"
    assert free.unit_price == Decimal("0.0")


def test_packaged_snapshot_is_historical_and_keeps_endpoints_distinct():
    from agent_alfred.pricing import StaticPriceBook

    book = StaticPriceBook.packaged()
    go = book.quote("opencode-go", "deepseek-v4-flash", "uncached_input")
    zen = book.quote("opencode", "deepseek-v4-flash", "uncached_input")
    assert book.snapshot_date is not None
    assert go is not None and zen is not None
    assert go.unit_price != zen.unit_price
    assert go.source == "model_static"
    assert zen.source == "model_static"


def test_packaged_notice_retains_models_dev_license():
    from importlib.resources import files

    notice = files("agent_alfred.data").joinpath("NOTICE").read_text(encoding="utf-8")
    assert "Copyright (c) 2025 models.dev" in notice
    assert "MIT" in notice
