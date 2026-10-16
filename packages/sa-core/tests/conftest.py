"""Shared fixtures. Offline by default: `online` tests run only with SA_ONLINE=1."""

from __future__ import annotations

import os

import pytest
from hypothesis import HealthCheck, settings

from sa_core.keys import DeviceKey, SAKey

settings.register_profile(
    "default",
    max_examples=40,
    deadline=None,  # scrypt/EVM signing time varies
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
settings.register_profile("thorough", parent=settings.get_profile("default"), max_examples=300)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("SA_ONLINE") == "1":
        return
    skip = pytest.mark.skip(reason="network test: set SA_ONLINE=1 to run")
    for item in items:
        if "online" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def device() -> DeviceKey:
    return DeviceKey.generate()


@pytest.fixture(scope="session")
def sa() -> SAKey:
    return SAKey.generate()
