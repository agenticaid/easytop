import pytest

from easytop import analytics


@pytest.fixture(autouse=True)
def strict_analytics():
    """Every test runs with a strict emitter: an analytics event that breaks the schema fails the test."""
    analytics.configure(env="test", key=b"test-key", strict=True)
    yield
    analytics.configure(env="test", key=b"test-key", strict=True)
