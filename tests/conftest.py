import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

FIXTURES = REPO_ROOT / "bench" / "fixtures"


@pytest.fixture
def fixture_copy(tmp_path):
    """Copy a bench fixture's target into a temp dir and return the path."""
    counter = {"n": 0}

    def _copy(name: str) -> Path:
        counter["n"] += 1
        dest = tmp_path / f"{name}-{counter['n']}"
        shutil.copytree(FIXTURES / name / "target", dest)
        return dest
    return _copy
