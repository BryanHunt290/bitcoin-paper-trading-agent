import sys
import os
from pathlib import Path


def _remove_cdk_build_paths() -> None:
    cdk_output = os.path.abspath('cdk.out')
    sys.path[:] = [
        path for path in sys.path
        if not path or cdk_output not in os.path.abspath(path)
    ]


_remove_cdk_build_paths()


def pytest_sessionstart(session):
    _remove_cdk_build_paths()

# Ensure project src is importable as a package during tests
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.portfolio.portfolio import Portfolio
from src.broker.paper_broker import PaperBroker


def make_portfolio_and_broker():
    p = Portfolio()
    b = PaperBroker(p)
    return p, b
