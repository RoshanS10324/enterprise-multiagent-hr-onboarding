import pytest

from helpers import Env


@pytest.fixture
def env() -> Env:
    return Env()
