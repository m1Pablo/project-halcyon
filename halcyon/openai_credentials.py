"""Fail-closed OpenAI credential resolution backed by the operating-system keyring."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import importlib
import math
import os
import sys
import threading
import unicodedata
from typing import Mapping, Protocol, runtime_checkable

KEYRING_SERVICE = "com.github.m1Pablo.project-halcyon"
KEYRING_ACCOUNT = "openai-api-key"
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
MAX_OPENAI_API_KEY_LENGTH = 512


class CredentialErrorCode(str, Enum):
    """Stable, secret-free error identifiers for credential operations."""

    INVALID_SECRET = "INVALID_SECRET"
    STORAGE_UNAVAILABLE = "STORAGE_UNAVAILABLE"
    READ_FAILED = "READ_FAILED"
    WRITE_FAILED = "WRITE_FAILED"
    DELETE_FAILED = "DELETE_FAILED"


_ERROR_MESSAGES = {
    CredentialErrorCode.INVALID_SECRET: (
        "OpenAI API key must be non-empty, at most "
        f"{MAX_OPENAI_API_KEY_LENGTH} characters, and contain no whitespace or control characters."
    ),
    CredentialErrorCode.STORAGE_UNAVAILABLE: "Secure credential storage is unavailable.",
    CredentialErrorCode.READ_FAILED: "Could not read the saved OpenAI credential.",
    CredentialErrorCode.WRITE_FAILED: (
        "Could not save the OpenAI credential securely; the current configuration is unchanged."
    ),
    CredentialErrorCode.DELETE_FAILED: (
        "Could not delete the saved OpenAI credential; the current configuration is unchanged."
    ),
}


class CredentialError(RuntimeError):
    """Typed credential failure whose text never includes a key or backend exception."""

    def __init__(self, code: CredentialErrorCode) -> None:
        self.code = code
        super().__init__(_ERROR_MESSAGES[code])


class CredentialSource(str, Enum):
    """Source selected for new OpenAI operations."""

    ENVIRONMENT = "environment"
    SYSTEM_KEYRING = "system_keyring"
    NONE = "none"


class StoredCredentialState(str, Enum):
    """Safe knowledge about the persistent credential without identifying it."""

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CredentialStatus:
    """Credential metadata safe for a local settings surface."""

    configured: bool
    source: CredentialSource
    stored: StoredCredentialState
    storage_available: bool
    busy: bool = False

    def to_public_dict(self) -> dict[str, bool | str]:
        """Return only fixed aggregate fields; no secret-derived identifier is exposed."""

        return {
            "configured": self.configured,
            "source": self.source.value,
            "stored": self.stored.value,
            "storage_available": self.storage_available,
            "busy": self.busy,
        }


class CredentialResolution:
    """An effective secret plus safe status, deliberately resistant to serialization."""

    __slots__ = ("_secret", "status")

    def __init__(self, secret: str | None, status: CredentialStatus) -> None:
        if status.configured is not (secret is not None):
            raise ValueError("credential status does not match effective configuration")
        self._secret = secret
        self.status = status

    def secret_for_client(self) -> str | None:
        """Return the secret only at the explicit OpenAI-client integration boundary."""

        return self._secret

    def __repr__(self) -> str:
        return f"CredentialResolution(secret=<redacted>, status={self.status!r})"

    __str__ = __repr__

    def __reduce__(self) -> object:
        raise TypeError("CredentialResolution is intentionally non-serializable")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("CredentialResolution is intentionally non-serializable")


@runtime_checkable
class SecretBackend(Protocol):
    """Minimal persistence contract injected into the credential resolver."""

    available: bool

    def read(self) -> str | None: ...

    def write(self, secret: str) -> None: ...

    def delete(self) -> None: ...


class _KeyringBackend(Protocol):
    priority: float

    def get_password(self, service: str, account: str) -> str | None: ...

    def set_password(self, service: str, account: str, password: str) -> None: ...

    def delete_password(self, service: str, account: str) -> None: ...


class _KeyringModule(Protocol):
    def get_keyring(self) -> _KeyringBackend: ...


_CHAINER_MODULE = "keyring.backends.chainer"
_ALLOWED_BACKEND_MODULES = {
    "darwin": frozenset({"keyring.backends.macOS"}),
    "windows": frozenset({"keyring.backends.Windows"}),
    "linux": frozenset(
        {
            "keyring.backends.SecretService",
            "keyring.backends.kwallet",
            "keyring.backends.libsecret",
        }
    ),
}


def _platform_family(platform_name: str) -> str | None:
    if platform_name == "darwin":
        return "darwin"
    if platform_name.startswith("win") or platform_name == "cygwin":
        return "windows"
    if platform_name.startswith("linux"):
        return "linux"
    return None


def _positive_priority(backend: object) -> bool:
    try:
        priority = getattr(backend, "priority")
        if isinstance(priority, bool):
            return False
        numeric = float(priority)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(numeric) and numeric > 0


def _is_attested_backend(
    backend: object,
    *,
    platform_name: str,
    ancestors: frozenset[int] = frozenset(),
) -> bool:
    """Return whether an object is a recursively attested OS credential backend."""

    family = _platform_family(platform_name)
    if family is None or not _positive_priority(backend):
        return False

    identity = id(backend)
    if identity in ancestors:
        return False
    next_ancestors = ancestors | {identity}
    backend_module = type(backend).__module__

    if backend_module == _CHAINER_MODULE:
        try:
            nested = tuple(getattr(backend, "backends"))
        except (AttributeError, TypeError):
            return False
        return bool(nested) and all(
            _is_attested_backend(
                candidate,
                platform_name=platform_name,
                ancestors=next_ancestors,
            )
            for candidate in nested
        )

    return backend_module in _ALLOWED_BACKEND_MODULES[family]


class KeyringSecretBackend:
    """Secret backend pinned to one recursively attested OS keyring instance."""

    available = True

    def __init__(
        self,
        *,
        keyring_module: _KeyringModule | None = None,
        platform_name: str | None = None,
    ) -> None:
        try:
            api = keyring_module or importlib.import_module("keyring")
            backend = api.get_keyring()
        except Exception:
            raise CredentialError(CredentialErrorCode.STORAGE_UNAVAILABLE) from None
        selected_platform = sys.platform if platform_name is None else platform_name
        try:
            attested = _is_attested_backend(
                backend,
                platform_name=selected_platform,
            )
        except Exception:
            attested = False
        if not attested:
            raise CredentialError(CredentialErrorCode.STORAGE_UNAVAILABLE)
        self._backend = backend

    def read(self) -> str | None:
        try:
            value = self._backend.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        except Exception:
            raise CredentialError(CredentialErrorCode.READ_FAILED) from None
        if value is not None and not isinstance(value, str):
            raise CredentialError(CredentialErrorCode.READ_FAILED)
        return value

    def write(self, secret: str) -> None:
        try:
            self._backend.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, secret)
        except Exception:
            raise CredentialError(CredentialErrorCode.WRITE_FAILED) from None

    def delete(self) -> None:
        """Delete idempotently without trusting backend-specific missing-item errors."""

        try:
            existing = self._backend.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
            if existing is None:
                return
            self._backend.delete_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        except Exception:
            raise CredentialError(CredentialErrorCode.DELETE_FAILED) from None


class UnavailableSecretBackend:
    """Fail-closed backend used when no audited operating-system store is available."""

    available = False

    def read(self) -> str | None:
        raise CredentialError(CredentialErrorCode.STORAGE_UNAVAILABLE)

    def write(self, secret: str) -> None:
        del secret
        raise CredentialError(CredentialErrorCode.STORAGE_UNAVAILABLE)

    def delete(self) -> None:
        raise CredentialError(CredentialErrorCode.STORAGE_UNAVAILABLE)


def system_secret_backend() -> SecretBackend:
    """Construct the audited OS backend or a fail-closed unavailable backend."""

    try:
        return KeyringSecretBackend()
    except CredentialError:
        return UnavailableSecretBackend()


def normalize_openai_api_key(secret: object) -> str:
    """Validate one opaque API key without depending on a provider prefix."""

    if not isinstance(secret, str):
        raise CredentialError(CredentialErrorCode.INVALID_SECRET)
    normalized = secret.strip()
    if not normalized or len(normalized) > MAX_OPENAI_API_KEY_LENGTH:
        raise CredentialError(CredentialErrorCode.INVALID_SECRET)
    if any(
        character.isspace() or unicodedata.category(character).startswith("C")
        for character in normalized
    ):
        raise CredentialError(CredentialErrorCode.INVALID_SECRET)
    return normalized


def _backend_available(backend: SecretBackend | None) -> bool:
    if backend is None:
        return False
    try:
        return backend.available is True
    except Exception:
        return False


class OpenAICredentialStore:
    """Resolve and mutate one persistent key without exposing it in status objects."""

    def __init__(
        self,
        *,
        backend: SecretBackend | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        # Defer native backend discovery until load/save/delete so a Textual caller can
        # keep both discovery and credential I/O inside one thread worker.
        self._backend = backend
        self._uses_system_backend = backend is None
        self._environment = environment if environment is not None else os.environ
        self._lock = threading.RLock()
        environment_present, environment_secret = self._environment_candidate()
        self._current = self._resolution(
            environment_present=environment_present,
            environment_secret=environment_secret,
            stored_secret=None,
            stored=StoredCredentialState.UNKNOWN,
            storage_available=True if self._uses_system_backend else _backend_available(backend),
        )

    @property
    def current(self) -> CredentialResolution:
        """Return the last complete resolution; mutations replace it only on success."""

        with self._lock:
            return self._current

    def load(self) -> CredentialResolution:
        """Resolve environment, then keyring, degrading safely when storage cannot be read."""

        with self._lock:
            backend = self._resolve_backend()
            environment_present, environment_secret = self._environment_candidate()
            if not _backend_available(backend):
                self._current = self._resolution(
                    environment_present=environment_present,
                    environment_secret=environment_secret,
                    stored_secret=None,
                    stored=StoredCredentialState.UNKNOWN,
                    storage_available=False,
                )
                return self._current
            try:
                raw_stored = backend.read()
            except Exception:
                self._current = self._resolution(
                    environment_present=environment_present,
                    environment_secret=environment_secret,
                    stored_secret=None,
                    stored=StoredCredentialState.UNKNOWN,
                    storage_available=False,
                )
                return self._current

            if raw_stored is None:
                stored_secret = None
                stored = StoredCredentialState.ABSENT
            else:
                stored = StoredCredentialState.PRESENT
                try:
                    stored_secret = normalize_openai_api_key(raw_stored)
                except CredentialError:
                    stored_secret = None
            self._current = self._resolution(
                environment_present=environment_present,
                environment_secret=environment_secret,
                stored_secret=stored_secret,
                stored=stored,
                storage_available=True,
            )
            return self._current

    def save(self, secret: object) -> CredentialResolution:
        """Persist a validated key and publish it only after storage reports success."""

        normalized = normalize_openai_api_key(secret)
        with self._lock:
            backend = self._resolve_backend()
            if not _backend_available(backend):
                raise CredentialError(CredentialErrorCode.STORAGE_UNAVAILABLE)
            try:
                backend.write(normalized)
            except Exception:
                raise CredentialError(CredentialErrorCode.WRITE_FAILED) from None
            environment_present, environment_secret = self._environment_candidate()
            self._current = self._resolution(
                environment_present=environment_present,
                environment_secret=environment_secret,
                stored_secret=normalized,
                stored=StoredCredentialState.PRESENT,
                storage_available=True,
            )
            return self._current

    def delete(self) -> CredentialResolution:
        """Idempotently delete the stored key and retain any environment override."""

        with self._lock:
            backend = self._resolve_backend()
            if not _backend_available(backend):
                raise CredentialError(CredentialErrorCode.STORAGE_UNAVAILABLE)
            try:
                backend.delete()
            except Exception:
                raise CredentialError(CredentialErrorCode.DELETE_FAILED) from None
            environment_present, environment_secret = self._environment_candidate()
            self._current = self._resolution(
                environment_present=environment_present,
                environment_secret=environment_secret,
                stored_secret=None,
                stored=StoredCredentialState.ABSENT,
                storage_available=True,
            )
            return self._current

    def _resolve_backend(self) -> SecretBackend:
        backend = self._backend
        if backend is None:
            backend = system_secret_backend()
            self._backend = backend
        return backend

    def _environment_candidate(self) -> tuple[bool, str | None]:
        try:
            raw = self._environment.get(OPENAI_API_KEY_ENV, "")
        except Exception:
            return False, None
        if not isinstance(raw, str) or not raw.strip():
            return False, None
        try:
            return True, normalize_openai_api_key(raw)
        except CredentialError:
            # A nonblank environment value owns precedence but fails closed.
            return True, None

    @staticmethod
    def _resolution(
        *,
        environment_present: bool,
        environment_secret: str | None,
        stored_secret: str | None,
        stored: StoredCredentialState,
        storage_available: bool,
    ) -> CredentialResolution:
        if environment_present:
            effective_secret = environment_secret
            source = CredentialSource.ENVIRONMENT
        elif stored_secret is not None:
            effective_secret = stored_secret
            source = CredentialSource.SYSTEM_KEYRING
        else:
            effective_secret = None
            source = CredentialSource.NONE
        return CredentialResolution(
            effective_secret,
            CredentialStatus(
                configured=effective_secret is not None,
                source=source,
                stored=stored,
                storage_available=storage_available,
            ),
        )


__all__ = [
    "CredentialError",
    "CredentialErrorCode",
    "CredentialResolution",
    "CredentialSource",
    "CredentialStatus",
    "KEYRING_ACCOUNT",
    "KEYRING_SERVICE",
    "KeyringSecretBackend",
    "MAX_OPENAI_API_KEY_LENGTH",
    "OPENAI_API_KEY_ENV",
    "OpenAICredentialStore",
    "SecretBackend",
    "StoredCredentialState",
    "UnavailableSecretBackend",
    "normalize_openai_api_key",
    "system_secret_backend",
]
