import json
import math
import pickle
from types import SimpleNamespace

import pytest

from halcyon.openai_credentials import (
    CredentialError,
    CredentialErrorCode,
    CredentialSource,
    KEYRING_ACCOUNT,
    KEYRING_SERVICE,
    KeyringSecretBackend,
    MAX_OPENAI_API_KEY_LENGTH,
    OPENAI_API_KEY_ENV,
    OpenAICredentialStore,
    StoredCredentialState,
    UnavailableSecretBackend,
    normalize_openai_api_key,
)


class MemoryBackend:
    def __init__(self, value: str | None = None, *, available: bool = True) -> None:
        self.available = available
        self.value = value
        self.read_calls = 0
        self.writes: list[str] = []
        self.delete_calls = 0
        self.fail_read: BaseException | None = None
        self.fail_write: BaseException | None = None
        self.fail_delete: BaseException | None = None

    def read(self) -> str | None:
        self.read_calls += 1
        if self.fail_read is not None:
            raise self.fail_read
        return self.value

    def write(self, secret: str) -> None:
        if self.fail_write is not None:
            raise self.fail_write
        self.writes.append(secret)
        self.value = secret

    def delete(self) -> None:
        if self.fail_delete is not None:
            raise self.fail_delete
        self.delete_calls += 1
        self.value = None


class NativeBackendBase:
    priority = 5

    def __init__(self, value: str | None = None) -> None:
        self.value = value
        self.calls: list[tuple[object, ...]] = []

    def get_password(self, service: str, account: str) -> str | None:
        self.calls.append(("get", service, account))
        return self.value

    def set_password(self, service: str, account: str, password: str) -> None:
        self.calls.append(("set", service, account, password))
        self.value = password

    def delete_password(self, service: str, account: str) -> None:
        self.calls.append(("delete", service, account))
        self.value = None


def native_backend(module: str, value: str | None = None, *, priority: float = 5):
    backend_class = type(
        "NativeBackend",
        (NativeBackendBase,),
        {"__module__": module, "priority": priority},
    )
    return backend_class(value)


def keyring_module(backend: object) -> SimpleNamespace:
    return SimpleNamespace(get_keyring=lambda: backend)


def chainer_backend(*backends: object, priority: float = 10):
    chainer_class = type(
        "ChainerBackend",
        (NativeBackendBase,),
        {"__module__": "keyring.backends.chainer", "priority": priority},
    )
    chainer = chainer_class()
    chainer.backends = tuple(backends)
    return chainer


def assert_error(callable_, code: CredentialErrorCode, private_marker: str | None = None) -> None:
    with pytest.raises(CredentialError) as caught:
        callable_()
    assert caught.value.code is code
    if private_marker is not None:
        assert private_marker not in str(caught.value)
        assert private_marker not in repr(caught.value)


def test_stable_keyring_identity_and_direct_backend_calls() -> None:
    native = native_backend("keyring.backends.macOS", "stored-opaque")
    module = keyring_module(native)
    backend = KeyringSecretBackend(keyring_module=module, platform_name="darwin")

    assert backend.read() == "stored-opaque"
    backend.write("replacement-opaque")
    backend.delete()
    backend.delete()

    assert KEYRING_SERVICE == "com.github.m1Pablo.project-halcyon"
    assert KEYRING_ACCOUNT == "openai-api-key"
    assert native.calls == [
        ("get", KEYRING_SERVICE, KEYRING_ACCOUNT),
        ("set", KEYRING_SERVICE, KEYRING_ACCOUNT, "replacement-opaque"),
        ("get", KEYRING_SERVICE, KEYRING_ACCOUNT),
        ("delete", KEYRING_SERVICE, KEYRING_ACCOUNT),
        ("get", KEYRING_SERVICE, KEYRING_ACCOUNT),
    ]

    replacement = native_backend("keyrings.alt.file", "PRIVATE_REPLACEMENT")
    module.get_keyring = lambda: replacement
    backend.write("still-original")
    assert native.value == "still-original"
    assert replacement.value == "PRIVATE_REPLACEMENT"


@pytest.mark.parametrize(
    ("platform_name", "module_name"),
    [
        ("darwin", "keyring.backends.macOS"),
        ("win32", "keyring.backends.Windows"),
        ("linux", "keyring.backends.SecretService"),
        ("linux2", "keyring.backends.kwallet"),
        ("linux", "keyring.backends.libsecret"),
    ],
)
def test_only_expected_native_backend_families_are_accepted(
    platform_name: str,
    module_name: str,
) -> None:
    backend = KeyringSecretBackend(
        keyring_module=keyring_module(native_backend(module_name)),
        platform_name=platform_name,
    )
    assert backend.available


@pytest.mark.parametrize(
    ("platform_name", "module_name", "priority"),
    [
        ("darwin", "keyring.backends.Windows", 5),
        ("win32", "keyring.backends.macOS", 5),
        ("linux", "keyring.backends.macOS", 5),
        ("darwin", "keyrings.alt.file", 5),
        ("darwin", "keyring.backends.null", 5),
        ("darwin", "keyring.backends.fail", 5),
        ("darwin", "custom.secureish", 5),
        ("darwin", "keyring.backends.macOS", 0),
        ("darwin", "keyring.backends.macOS", -1),
        ("darwin", "keyring.backends.macOS", math.nan),
        ("freebsd", "keyring.backends.macOS", 5),
    ],
)
def test_unknown_cross_platform_insecure_and_nonviable_backends_fail_closed(
    platform_name: str,
    module_name: str,
    priority: float,
) -> None:
    assert_error(
        lambda: KeyringSecretBackend(
            keyring_module=keyring_module(native_backend(module_name, priority=priority)),
            platform_name=platform_name,
        ),
        CredentialErrorCode.STORAGE_UNAVAILABLE,
    )


def test_chainer_is_accepted_only_when_all_nested_backends_are_secure_and_acyclic() -> None:
    first = native_backend("keyring.backends.macOS")
    second = native_backend("keyring.backends.macOS")
    nested = chainer_backend(second)
    accepted = chainer_backend(first, nested)
    assert KeyringSecretBackend(
        keyring_module=keyring_module(accepted), platform_name="darwin"
    ).available

    for rejected in (
        chainer_backend(),
        chainer_backend(first, native_backend("keyrings.alt.file")),
        chainer_backend(first, native_backend("keyring.backends.Windows")),
    ):
        assert_error(
            lambda rejected=rejected: KeyringSecretBackend(
                keyring_module=keyring_module(rejected),
                platform_name="darwin",
            ),
            CredentialErrorCode.STORAGE_UNAVAILABLE,
        )

    cyclic = chainer_backend()
    cyclic.backends = (cyclic,)
    assert_error(
        lambda: KeyringSecretBackend(keyring_module=keyring_module(cyclic), platform_name="darwin"),
        CredentialErrorCode.STORAGE_UNAVAILABLE,
    )


def test_backend_attestation_property_failures_are_fixed_unavailable_errors() -> None:
    marker = "PRIVATE_ATTESTATION_FAILURE"

    class HostilePriority:
        __module__ = "keyring.backends.macOS"

        @property
        def priority(self) -> float:
            raise RuntimeError(marker)

    assert_error(
        lambda: KeyringSecretBackend(
            keyring_module=keyring_module(HostilePriority()),
            platform_name="darwin",
        ),
        CredentialErrorCode.STORAGE_UNAVAILABLE,
        marker,
    )


def test_keyring_initialization_and_operations_use_fixed_errors_only() -> None:
    marker = "PRIVATE_BACKEND_DIAGNOSTIC"
    assert_error(
        lambda: KeyringSecretBackend(
            keyring_module=SimpleNamespace(
                get_keyring=lambda: (_ for _ in ()).throw(RuntimeError(marker))
            ),
            platform_name="darwin",
        ),
        CredentialErrorCode.STORAGE_UNAVAILABLE,
        marker,
    )

    class FailingNative(NativeBackendBase):
        __module__ = "keyring.backends.macOS"

        def get_password(self, service: str, account: str) -> str | None:
            raise RuntimeError(marker)

        def set_password(self, service: str, account: str, password: str) -> None:
            raise RuntimeError(f"{marker}:{password}")

        def delete_password(self, service: str, account: str) -> None:
            raise RuntimeError(marker)

    failing = KeyringSecretBackend(
        keyring_module=keyring_module(FailingNative()), platform_name="darwin"
    )
    assert_error(failing.read, CredentialErrorCode.READ_FAILED, marker)
    assert_error(lambda: failing.write(marker), CredentialErrorCode.WRITE_FAILED, marker)

    failing._backend.get_password = lambda service, account: "exists"  # type: ignore[method-assign]
    assert_error(failing.delete, CredentialErrorCode.DELETE_FAILED, marker)


def test_keyring_rejects_non_text_reads() -> None:
    native = native_backend("keyring.backends.macOS")
    native.get_password = lambda service, account: b"not-text"  # type: ignore[method-assign]
    backend = KeyringSecretBackend(keyring_module=keyring_module(native), platform_name="darwin")
    assert_error(backend.read, CredentialErrorCode.READ_FAILED)


def test_environment_precedes_stored_key_without_copying_it() -> None:
    backend = MemoryBackend("stored-opaque")
    environment = {OPENAI_API_KEY_ENV: "  environment-opaque  "}
    store = OpenAICredentialStore(backend=backend, environment=environment)

    assert store.current.secret_for_client() == "environment-opaque"
    assert store.current.status.source is CredentialSource.ENVIRONMENT
    assert store.current.status.stored is StoredCredentialState.UNKNOWN
    assert backend.read_calls == 0
    assert backend.writes == []

    loaded = store.load()
    assert loaded.secret_for_client() == "environment-opaque"
    assert loaded.status.source is CredentialSource.ENVIRONMENT
    assert loaded.status.stored is StoredCredentialState.PRESENT
    assert backend.read_calls == 1
    assert backend.writes == []


def test_default_store_defers_system_backend_discovery_until_operation(monkeypatch) -> None:
    calls: list[str] = []
    backend = MemoryBackend("stored-opaque")

    def build_backend() -> MemoryBackend:
        calls.append("discover")
        return backend

    monkeypatch.setattr(
        "halcyon.openai_credentials.system_secret_backend",
        build_backend,
    )
    store = OpenAICredentialStore(environment={})

    assert calls == []
    assert store.current.status.storage_available
    assert store.load().secret_for_client() == "stored-opaque"
    assert calls == ["discover"]
    store.load()
    assert calls == ["discover"]


@pytest.mark.parametrize("environment_value", [None, "", " ", "\t\n"])
def test_absent_or_blank_environment_falls_through_to_keyring(
    environment_value: str | None,
) -> None:
    environment = {} if environment_value is None else {OPENAI_API_KEY_ENV: environment_value}
    store = OpenAICredentialStore(
        backend=MemoryBackend("stored-without-prefix"),
        environment=environment,
    )
    loaded = store.load()
    assert loaded.secret_for_client() == "stored-without-prefix"
    assert loaded.status.source is CredentialSource.SYSTEM_KEYRING
    assert loaded.status.configured


def test_invalid_nonblank_environment_blocks_stored_fallback() -> None:
    store = OpenAICredentialStore(
        backend=MemoryBackend("stored-opaque"),
        environment={OPENAI_API_KEY_ENV: "invalid internal whitespace"},
    )
    loaded = store.load()
    assert loaded.secret_for_client() is None
    assert not loaded.status.configured
    assert loaded.status.source is CredentialSource.ENVIRONMENT
    assert loaded.status.stored is StoredCredentialState.PRESENT


def test_absent_everywhere_and_invalid_stored_value_are_safe_aggregate_states() -> None:
    absent = OpenAICredentialStore(backend=MemoryBackend(), environment={}).load()
    assert absent.secret_for_client() is None
    assert absent.status.source is CredentialSource.NONE
    assert absent.status.stored is StoredCredentialState.ABSENT
    assert absent.status.storage_available

    invalid = OpenAICredentialStore(
        backend=MemoryBackend("invalid stored value"), environment={}
    ).load()
    assert invalid.secret_for_client() is None
    assert invalid.status.source is CredentialSource.NONE
    assert invalid.status.stored is StoredCredentialState.PRESENT
    assert invalid.status.storage_available


def test_load_failure_is_nonthrowing_and_does_not_displace_environment() -> None:
    marker = "PRIVATE_READ_FAILURE"
    backend = MemoryBackend("stored")
    backend.fail_read = RuntimeError(marker)
    store = OpenAICredentialStore(
        backend=backend,
        environment={OPENAI_API_KEY_ENV: "environment-opaque"},
    )

    loaded = store.load()
    assert loaded.secret_for_client() == "environment-opaque"
    assert loaded.status.source is CredentialSource.ENVIRONMENT
    assert loaded.status.stored is StoredCredentialState.UNKNOWN
    assert not loaded.status.storage_available
    assert marker not in repr(loaded)


def test_unavailable_backend_loads_safely_but_mutations_fail_closed() -> None:
    store = OpenAICredentialStore(
        backend=UnavailableSecretBackend(),
        environment={OPENAI_API_KEY_ENV: "environment-opaque"},
    )
    loaded = store.load()
    assert loaded.secret_for_client() == "environment-opaque"
    assert not loaded.status.storage_available

    assert_error(
        lambda: store.save("new-opaque"),
        CredentialErrorCode.STORAGE_UNAVAILABLE,
    )
    assert_error(store.delete, CredentialErrorCode.STORAGE_UNAVAILABLE)
    assert store.current is loaded


def test_save_is_atomic_and_environment_remains_the_effective_override() -> None:
    backend = MemoryBackend("old-stored")
    environment = {OPENAI_API_KEY_ENV: "environment-opaque"}
    store = OpenAICredentialStore(backend=backend, environment=environment)
    before = store.load()

    saved = store.save("  saved-without-prefix  ")
    assert saved is store.current
    assert saved is not before
    assert backend.value == "saved-without-prefix"
    assert backend.writes == ["saved-without-prefix"]
    assert saved.secret_for_client() == "environment-opaque"
    assert saved.status.source is CredentialSource.ENVIRONMENT
    assert saved.status.stored is StoredCredentialState.PRESENT

    environment.clear()
    reloaded = store.load()
    assert reloaded.secret_for_client() == "saved-without-prefix"
    assert reloaded.status.source is CredentialSource.SYSTEM_KEYRING


def test_save_failure_retains_current_resolution_and_uses_fixed_error() -> None:
    marker = "PRIVATE_WRITE_FAILURE"
    backend = MemoryBackend("old-stored")
    store = OpenAICredentialStore(backend=backend, environment={})
    before = store.load()
    backend.fail_write = RuntimeError(f"{marker}:new-private")

    assert_error(
        lambda: store.save("new-private"),
        CredentialErrorCode.WRITE_FAILED,
        marker,
    )
    assert store.current is before
    assert store.current.secret_for_client() == "old-stored"


def test_delete_is_idempotent_and_retains_environment_override() -> None:
    backend = MemoryBackend("stored")
    environment = {OPENAI_API_KEY_ENV: "environment-opaque"}
    store = OpenAICredentialStore(backend=backend, environment=environment)
    store.load()

    first = store.delete()
    second = store.delete()
    assert backend.delete_calls == 2
    assert first.secret_for_client() == "environment-opaque"
    assert second.secret_for_client() == "environment-opaque"
    assert second.status.source is CredentialSource.ENVIRONMENT
    assert second.status.stored is StoredCredentialState.ABSENT

    environment.clear()
    third = store.delete()
    assert third.secret_for_client() is None
    assert third.status.source is CredentialSource.NONE


def test_delete_failure_retains_current_resolution_and_uses_fixed_error() -> None:
    marker = "PRIVATE_DELETE_FAILURE"
    backend = MemoryBackend("old-stored")
    store = OpenAICredentialStore(backend=backend, environment={})
    before = store.load()
    backend.fail_delete = RuntimeError(marker)

    assert_error(store.delete, CredentialErrorCode.DELETE_FAILED, marker)
    assert store.current is before
    assert store.current.secret_for_client() == "old-stored"


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        123,
        "",
        " ",
        "internal whitespace",
        "line\nbreak",
        "tab\tinside",
        "null\0inside",
        "x" * (MAX_OPENAI_API_KEY_LENGTH + 1),
    ],
)
def test_validation_rejects_invalid_values_before_storage(invalid: object) -> None:
    backend = MemoryBackend()
    store = OpenAICredentialStore(backend=backend, environment={})
    assert_error(lambda: store.save(invalid), CredentialErrorCode.INVALID_SECRET)
    assert backend.writes == []


def test_validation_is_bounded_and_does_not_require_an_sk_prefix() -> None:
    opaque = "z" * MAX_OPENAI_API_KEY_LENGTH
    assert normalize_openai_api_key(f"  {opaque}  ") == opaque

    backend = MemoryBackend()
    resolution = OpenAICredentialStore(backend=backend, environment={}).save(
        "opaque_future_format-123_ABC"
    )
    assert resolution.secret_for_client() == "opaque_future_format-123_ABC"


def test_resolution_is_redacted_nonserializable_and_status_is_safe_json() -> None:
    secret = "PRIVATE_OPENAI_SECRET"
    resolution = OpenAICredentialStore(backend=MemoryBackend(secret), environment={}).load()

    assert resolution.secret_for_client() == secret
    assert secret not in repr(resolution)
    assert secret not in str(resolution)
    assert secret not in repr(resolution.status)
    public = resolution.status.to_public_dict()
    assert public == {
        "configured": True,
        "source": "system_keyring",
        "stored": "present",
        "storage_available": True,
        "busy": False,
    }
    assert secret not in json.dumps(public)
    with pytest.raises(TypeError):
        json.dumps(resolution)
    with pytest.raises(TypeError, match="non-serializable"):
        pickle.dumps(resolution)
    with pytest.raises(TypeError):
        vars(resolution)
