"""Make the src/ layout importable when running `pytest` from the repo root
without installing the package. The modules under test (ids, reports, config,
logutil) pull in only the standard library, so the suite needs no third-party
deps and never touches Slack, Sheets, or Anthropic."""
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
