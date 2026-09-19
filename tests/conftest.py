"""Suite-wide safety fixtures."""

from collections.abc import Iterator

import pytest

import halcyon.openai_credentials as openai_credentials


@pytest.fixture(autouse=True)
def _never_touch_the_real_system_keyring(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Require credential tests to inject a fake backend or key explicitly."""

    monkeypatch.delenv(openai_credentials.OPENAI_API_KEY_ENV, raising=False)
    monkeypatch.setattr(
        openai_credentials,
        "system_secret_backend",
        openai_credentials.UnavailableSecretBackend,
    )
    yield
