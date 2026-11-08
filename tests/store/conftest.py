import pytest

from gsched.paths import Paths
from gsched.store import Store


@pytest.fixture
def paths(short_tmp):
    p = Paths.resolve(short_tmp)
    p.ensure_dirs()
    return p


@pytest.fixture
def store(paths, clock):
    s = Store(paths, clock)
    s.upsert_project("p", 1)
    s.upsert_project("q", 1)
    yield s
    s.close()
