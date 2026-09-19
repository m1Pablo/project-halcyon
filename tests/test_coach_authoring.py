from copy import deepcopy
from datetime import datetime, timezone
import json
import threading

import pytest

import halcyon.coach_authoring as coach_authoring
from halcyon.coach_authoring import (
    AUTHORING_JSON_SCHEMA,
    DEFAULT_COACH_MODEL,
    AuthoringControl,
    AuthoringProviderMalformed,
    AuthoringProviderRefusal,
    AuthoringProviderUnavailable,
    LessonService,
    OpenAILessonAuthoringProvider,
)
from halcyon.coach_models import (
    Acquisition,
    AuthorMode,
    Difficulty,
    Fidelity,
    Hand,
    LessonRequest,
)
from halcyon.coach_store import LessonStore

NOW = datetime(2026, 7, 12, tzinfo=timezone.utc)


def valid_proposal() -> dict:
    return {
        "schema_version": 1,
        "title": "Bounded Study",
        "composer": "Named Composer",
        "segment_label": "Main Melody",
        "difficulty": "foundation",
        "hand": "right",
        "ppq": 480,
        "max_polyphony": 1,
        "tempo_map": [{"absolute_tick": 0, "microseconds_per_quarter": 500_000}],
        "time_signatures": [{"absolute_tick": 0, "numerator": 4, "denominator": 4}],
        "events": [
            {
                "event_type": "note_on",
                "note": 60,
                "velocity": 80,
                "channel": 1,
                "absolute_tick": 0,
                "delta_tick": 0,
            },
            {
                "event_type": "note_off",
                "note": 60,
                "velocity": 0,
                "channel": 1,
                "absolute_tick": 480,
                "delta_tick": 480,
            },
        ],
    }


class FakeProvider:
    provider_name = "offline-fake"
    model_version = "fake-schema-1"

    def __init__(self, result=None, error=None) -> None:
        self.result = result if result is not None else valid_proposal()
        self.error = error
        self.calls = 0

    def propose(self, request, *, control):
        self.calls += 1
        control.checkpoint("fake.propose")
        if self.error is not None:
            raise self.error
        return deepcopy(self.result)


def proposal_limit_events(pair_count: int, duration_tick: int) -> list[dict]:
    events: list[dict] = []
    for index in range(pair_count):
        events.append(
            {
                "event_type": "note_on",
                "note": 60,
                "velocity": 80,
                "channel": 1,
                "absolute_tick": 0,
                "delta_tick": 0,
            }
        )
        end_tick = duration_tick if index == pair_count - 1 else 0
        events.append(
            {
                "event_type": "note_off",
                "note": 60,
                "velocity": 0,
                "channel": 1,
                "absolute_tick": end_tick,
                "delta_tick": end_tick,
            }
        )
    return events


def bounded_request(**changes) -> LessonRequest:
    values = {
        "query": "Named Work Main Melody",
        "segment": "Main Melody",
        "difficulty": Difficulty.FOUNDATION,
        "hand": Hand.RIGHT,
        "author": AuthorMode.BOUNDED,
    }
    values.update(changes)
    return LessonRequest(**values)


def service(tmp_path, provider) -> LessonService:
    return LessonService(
        store=LessonStore(root=tmp_path / "coach"),
        provider=provider,
        now=lambda: NOW,
    )


def test_valid_bounded_proposal_is_normalized_persisted_and_reloaded(tmp_path) -> None:
    provider = FakeProvider()
    lesson_service = service(tmp_path, provider)

    resolution = lesson_service.resolve(bounded_request())

    assert resolution.authored is True
    assert resolution.cache_hit is False
    assert resolution.lesson.provenance.acquisition is Acquisition.LOCALLY_AUTHORED_BOUNDED
    assert resolution.lesson.provenance.fidelity is Fidelity.APPROXIMATE
    assert resolution.lesson.provenance.provider == provider.provider_name
    assert resolution.lesson.provenance.model_version == provider.model_version
    assert "approximation" in resolution.lesson.provenance.citation
    assert resolution.lesson.events[0].source_port.startswith("halcyon://coach/")
    assert resolution.timeline.duration_us == 500_000
    assert provider.calls == 1


def test_authoring_canonicalizes_provider_status_from_event_type_and_channel(tmp_path) -> None:
    proposal = valid_proposal()
    for event in proposal["events"]:
        event["channel"] = 2
        event["status"] = 0x90  # Wrong channel, and wrong command for note_off.

    resolution = service(tmp_path, FakeProvider(result=proposal)).resolve(bounded_request())

    assert [event.status for event in resolution.lesson.events] == [0x91, 0x81]
    assert [event.channel for event in resolution.lesson.events] == [2, 2]
    assert resolution.cache_hit is False
    assert resolution.authored is True


def test_status_free_authoring_derives_channel_sixteen_status_bytes(tmp_path) -> None:
    proposal = valid_proposal()
    for event in proposal["events"]:
        event["channel"] = 16

    resolution = service(tmp_path, FakeProvider(result=proposal)).resolve(bounded_request())

    assert [event.status for event in resolution.lesson.events] == [0x9F, 0x8F]
    assert [event.channel for event in resolution.lesson.events] == [16, 16]


@pytest.mark.parametrize("status", (-1, 256, "144"))
def test_legacy_provider_status_must_remain_a_bounded_integer(tmp_path, status) -> None:
    proposal = valid_proposal()
    proposal["events"][0]["status"] = status
    lesson_service = service(tmp_path, FakeProvider(result=proposal))

    with pytest.raises(Exception) as caught:
        lesson_service.resolve(bounded_request())

    assert caught.value.code == "AUTHORING_INVALID"
    assert caught.value.field == "events[0].status"
    assert not (lesson_service.store.root / "lessons").exists()


def test_status_compatibility_does_not_allow_unknown_event_fields(tmp_path) -> None:
    proposal = valid_proposal()
    proposal["events"][0]["status"] = 0x90
    proposal["events"][0]["unexpected"] = "not allowed"
    lesson_service = service(tmp_path, FakeProvider(result=proposal))

    with pytest.raises(Exception) as caught:
        lesson_service.resolve(bounded_request())

    assert caught.value.code == "AUTHORING_INVALID"
    assert caught.value.field == "events[0]"
    assert not (lesson_service.store.root / "lessons").exists()


def test_event_validation_failure_has_provider_retry_guidance(tmp_path) -> None:
    proposal = valid_proposal()
    proposal["events"][0]["velocity"] = 0
    lesson_service = service(tmp_path, FakeProvider(result=proposal))

    with pytest.raises(Exception) as caught:
        lesson_service.resolve(bounded_request())

    assert caught.value.code == "AUTHORING_INVALID"
    assert caught.value.field == "events.status"
    assert caught.value.retryable is True
    assert caught.value.resolution == "Retry once; no lesson was saved."
    assert "schema-1" not in caught.value.resolution
    assert not (lesson_service.store.root / "lessons").exists()


def test_resolution_snapshots_provider_for_call_and_provenance(tmp_path) -> None:
    replacement = FakeProvider()
    replacement.provider_name = "replacement-provider"
    replacement.model_version = "replacement-model"
    holder: list[LessonService] = []

    class SwappingProvider(FakeProvider):
        provider_name = "starting-provider"
        model_version = "starting-model"

        def propose(self, request, *, control):  # type: ignore[no-untyped-def]
            holder[0].provider = replacement
            return super().propose(request, control=control)

    starting = SwappingProvider()
    lesson_service = service(tmp_path, starting)
    holder.append(lesson_service)

    resolution = lesson_service.resolve(bounded_request())

    assert lesson_service.provider is replacement
    assert resolution.lesson.provenance.provider == "starting-provider"
    assert resolution.lesson.provenance.model_version == "starting-model"
    assert starting.calls == 1
    assert replacement.calls == 0
    assert (
        lesson_service.store.load_revision(
            resolution.lesson.lesson_id, resolution.lesson.content_id
        )
        == resolution.lesson
    )


def test_second_request_is_cache_hit_and_never_calls_provider_again(tmp_path) -> None:
    provider = FakeProvider()
    first_service = service(tmp_path, provider)
    first = first_service.resolve(bounded_request())
    unavailable = FakeProvider(error=AuthoringProviderUnavailable("offline"))
    offline_service = LessonService(store=first_service.store, provider=unavailable)

    second = offline_service.resolve(
        bounded_request(author=AuthorMode.NEVER, difficulty=None, hand=None)
    )

    assert second.cache_hit is True
    assert second.authored is False
    assert second.lesson.content_id == first.lesson.content_id
    assert provider.calls == 1
    assert unavailable.calls == 0


def test_posthoc_equivalent_alias_rebuilds_offline_without_provider_call(tmp_path) -> None:
    provider = FakeProvider()
    first_service = service(tmp_path, provider)
    first = first_service.resolve(bounded_request())
    manifest_path = first_service.store.revision_path(
        first.lesson.lesson_id, first.lesson.content_id
    )
    manifest_bytes = manifest_path.read_bytes()
    first_service.store.persist(first.lesson, aliases=("Equivalent Request",))
    assert manifest_path.read_bytes() == manifest_bytes
    first_service.store.catalog_path.unlink()

    unavailable = FakeProvider(error=AssertionError("provider must stay offline"))
    offline_service = LessonService(
        store=LessonStore(root=first_service.store.root), provider=unavailable
    )
    equivalent = offline_service.resolve(
        bounded_request(
            query="Equivalent Request",
            author=AuthorMode.NEVER,
            difficulty=None,
            hand=None,
        )
    )
    original = offline_service.resolve(
        bounded_request(author=AuthorMode.NEVER, difficulty=None, hand=None)
    )

    assert equivalent.cache_hit is original.cache_hit is True
    assert equivalent.lesson == original.lesson == first.lesson
    assert unavailable.calls == 0
    assert manifest_path.read_bytes() == manifest_bytes


def test_named_work_miss_with_author_never_requires_source_without_provider_call(tmp_path) -> None:
    provider = FakeProvider()
    lesson_service = service(tmp_path, provider)

    with pytest.raises(Exception) as caught:
        lesson_service.resolve(bounded_request(author=AuthorMode.NEVER))

    assert caught.value.code == "SOURCE_REQUIRED"
    assert caught.value.retryable is True
    assert '--segment "main melody"' in caught.value.resolution
    assert "without --author never" in caught.value.resolution
    assert provider.calls == 0
    assert not lesson_service.store.root.exists()


def test_bounded_authoring_requires_explicit_segment(tmp_path) -> None:
    provider = FakeProvider()
    lesson_service = service(tmp_path, provider)

    with pytest.raises(Exception) as caught:
        lesson_service.resolve(bounded_request(segment=None))

    assert caught.value.code == "SOURCE_REQUIRED"
    assert caught.value.field == "segment"
    assert provider.calls == 0


def test_missing_provider_is_explicitly_unavailable(tmp_path) -> None:
    lesson_service = LessonService(store=LessonStore(root=tmp_path / "coach"))

    with pytest.raises(Exception) as caught:
        lesson_service.resolve(bounded_request())

    assert caught.value.code == "AUTHORING_UNAVAILABLE"


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (AuthoringProviderRefusal("no"), "AUTHORING_REFUSED"),
        (AuthoringProviderMalformed("bad"), "AUTHORING_INVALID"),
        (AuthoringProviderUnavailable("offline"), "AUTHORING_UNAVAILABLE"),
    ],
)
def test_provider_failures_are_typed_and_persist_nothing(tmp_path, error, expected_code) -> None:
    provider = FakeProvider(error=error)
    lesson_service = service(tmp_path, provider)

    with pytest.raises(Exception) as caught:
        lesson_service.resolve(bounded_request(query="Private request text"))

    assert caught.value.code == expected_code
    assert "Private request text" not in str(caught.value)
    assert provider.calls == 1
    assert not (lesson_service.store.root / "lessons").exists()


@pytest.mark.parametrize(
    "error",
    (
        AuthoringProviderRefusal("PRIVATE_REFUSAL_DETAIL sk-private"),
        AuthoringProviderMalformed("PRIVATE_MODEL_OUTPUT /private/path"),
    ),
)
def test_public_refusal_and_malformed_failures_suppress_private_provider_causes(
    tmp_path,
    error,
) -> None:
    with pytest.raises(Exception) as caught:
        service(tmp_path, FakeProvider(error=error)).resolve(
            bounded_request(query="PRIVATE_REQUEST_TEXT")
        )

    rendered = f"{caught.value} {caught.value.resolution}"
    assert caught.value.__suppress_context__ is True
    assert caught.value.__cause__ is None
    assert "PRIVATE_REFUSAL_DETAIL" not in rendered
    assert "PRIVATE_MODEL_OUTPUT" not in rendered
    assert "PRIVATE_REQUEST_TEXT" not in rendered
    assert "sk-private" not in rendered
    assert "/private/path" not in rendered


def test_malformed_closed_schema_output_is_never_persisted(tmp_path) -> None:
    proposal = valid_proposal()
    proposal["unexpected"] = "not allowed"
    lesson_service = service(tmp_path, FakeProvider(result=proposal))

    with pytest.raises(Exception) as caught:
        lesson_service.resolve(bounded_request())

    assert caught.value.code == "AUTHORING_INVALID"
    assert caught.value.field == "proposal"
    assert not (lesson_service.store.root / "lessons").exists()


def test_over_limit_provider_output_is_never_persisted(tmp_path) -> None:
    proposal = valid_proposal()
    events = []
    tick = 0
    for _ in range(257):
        events.extend(
            [
                {
                    "event_type": "note_on",
                    "note": 60,
                    "velocity": 80,
                    "channel": 1,
                    "status": 0x90,
                    "absolute_tick": tick,
                    "delta_tick": 0 if not events else 1,
                },
                {
                    "event_type": "note_off",
                    "note": 60,
                    "velocity": 0,
                    "channel": 1,
                    "status": 0x80,
                    "absolute_tick": tick + 1,
                    "delta_tick": 1,
                },
            ]
        )
        tick += 2
    proposal["events"] = events
    lesson_service = service(tmp_path, FakeProvider(result=proposal))

    with pytest.raises(Exception) as caught:
        lesson_service.resolve(bounded_request())

    assert caught.value.code == "AUTHORING_INVALID"
    assert caught.value.field == "events"
    assert not (lesson_service.store.root / "lessons").exists()


def test_authoring_accepts_exact_event_and_duration_boundaries(tmp_path) -> None:
    proposal = valid_proposal()
    proposal["ppq"] = 1
    proposal["tempo_map"] = [{"absolute_tick": 0, "microseconds_per_quarter": 1_000_000}]
    proposal["events"] = proposal_limit_events(256, 120)

    resolution = service(tmp_path, FakeProvider(result=proposal)).resolve(bounded_request())

    assert len(resolution.lesson.events) == 512
    assert resolution.lesson.duration_us == 120_000_000


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("segment_label", "Whole Song"),
        ("difficulty", "challenge"),
        ("hand", "left"),
    ],
)
def test_incompatible_provider_output_is_never_persisted(tmp_path, field, value) -> None:
    proposal = valid_proposal()
    proposal[field] = value
    lesson_service = service(tmp_path, FakeProvider(result=proposal))

    with pytest.raises(Exception) as caught:
        lesson_service.resolve(bounded_request())

    assert caught.value.code == "AUTHORING_INVALID"
    assert caught.value.field == field
    assert not (lesson_service.store.root / "lessons").exists()


def test_authoring_duration_and_musical_validation_fail_before_persistence(tmp_path) -> None:
    too_long = valid_proposal()
    too_long["ppq"] = 1
    too_long["tempo_map"] = [{"absolute_tick": 0, "microseconds_per_quarter": 1_000_000}]
    too_long["events"][1]["absolute_tick"] = 121
    too_long["events"][1]["delta_tick"] = 121

    lesson_service = service(tmp_path, FakeProvider(result=too_long))
    with pytest.raises(Exception) as caught:
        lesson_service.resolve(bounded_request())

    assert caught.value.code == "AUTHORING_INVALID"
    assert "120 seconds" not in str(caught.value)  # bounded public error, detail stays in field
    assert not (lesson_service.store.root / "lessons").exists()


def test_structured_output_schema_is_closed_strict_and_bounded() -> None:
    assert set(AUTHORING_JSON_SCHEMA["required"]) == set(AUTHORING_JSON_SCHEMA["properties"])
    assert AUTHORING_JSON_SCHEMA["additionalProperties"] is False
    assert AUTHORING_JSON_SCHEMA["properties"]["events"]["maxItems"] == 512
    event_schema = AUTHORING_JSON_SCHEMA["properties"]["events"]["items"]
    assert "status" not in event_schema["properties"]
    assert "status" not in event_schema["required"]

    def assert_closed(schema):
        assert "minLength" not in schema
        assert "maxLength" not in schema
        assert "const" not in schema
        if schema.get("type") == "object":
            assert schema["additionalProperties"] is False
            assert set(schema["required"]) == set(schema["properties"])
            for child in schema["properties"].values():
                assert_closed(child)
        if schema.get("type") == "array":
            assert_closed(schema["items"])

    assert_closed(AUTHORING_JSON_SCHEMA)


class FakeResponses:
    def __init__(self, response) -> None:
        self.response = response
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class FakeClient:
    def __init__(self, response) -> None:
        self.responses = FakeResponses(response)
        self.options = []

    def with_options(self, **kwargs):
        self.options.append(kwargs)
        return self


class RaisingResponses:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def create(self, **kwargs):
        del kwargs
        raise self.error


class RaisingClient:
    def __init__(self, error: Exception) -> None:
        self.responses = RaisingResponses(error)

    def with_options(self, **kwargs):
        del kwargs
        return self


def sdk_error(name: str, *, status: int | None = None, code: str | None = None) -> Exception:
    error = type(name, (Exception,), {})("PRIVATE_PROVIDER_DETAIL")
    error.status_code = status  # type: ignore[attr-defined]
    error.code = code  # type: ignore[attr-defined]
    return error


def test_openai_adapter_uses_responses_text_format_strict_schema_offline() -> None:
    proposal = valid_proposal()
    client = FakeClient(
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(proposal)}],
                }
            ],
        }
    )
    provider = OpenAILessonAuthoringProvider(client=client, model_version="injected-model")

    returned = provider.propose(
        type(
            "Request",
            (),
            {
                "query": "Named work",
                "segment": "Main Melody",
                "difficulty": None,
                "hand": None,
                "max_events": 512,
                "max_duration_seconds": 120,
            },
        )(),
        control=AuthoringControl(),
    )

    assert returned == proposal
    call = client.responses.calls[0]
    assert call["model"] == "injected-model"
    assert call["store"] is False
    assert client.options == [{"timeout": 60.0, "max_retries": 0}]
    assert call["text"]["format"] == {
        "type": "json_schema",
        "name": "halcyon_lesson_proposal",
        "strict": True,
        "schema": AUTHORING_JSON_SCHEMA,
    }
    assert "files" not in call["input"].lower()
    assert "url" not in call["input"].lower()


def test_openai_adapter_uses_low_reasoning_for_gpt5_lesson_models() -> None:
    client = FakeClient({"status": "completed", "output_text": json.dumps(valid_proposal())})
    provider = OpenAILessonAuthoringProvider(client=client, model_version="gpt-5.6-luna")

    provider.propose(
        type(
            "Request",
            (),
            {
                "query": "Original exercise",
                "segment": "main",
                "difficulty": None,
                "hand": None,
                "max_events": 8,
                "max_duration_seconds": 8,
            },
        )(),
        control=AuthoringControl(),
    )

    assert client.responses.calls[0]["reasoning"] == {"effort": "low"}


@pytest.mark.parametrize(
    ("error", "category"),
    (
        (sdk_error("AuthenticationError", status=401), "authentication"),
        (sdk_error("PermissionDeniedError", status=403), "permission"),
        (sdk_error("NotFoundError", status=404, code="model_not_found"), "model"),
        (sdk_error("RateLimitError", status=429, code="insufficient_quota"), "quota"),
        (sdk_error("RateLimitError", status=429, code="rate_limit_exceeded"), "rate_limit"),
        (sdk_error("APITimeoutError"), "timeout"),
        (sdk_error("APIConnectionError"), "connection"),
        (sdk_error("BadRequestError", status=400), "request"),
        (sdk_error("InternalServerError", status=500), "service"),
    ),
)
def test_openai_adapter_classifies_transport_failures_without_raw_text(
    error: Exception,
    category: str,
) -> None:
    provider = OpenAILessonAuthoringProvider(
        client=RaisingClient(error),
        model_version="injected",
    )
    request = type(
        "Request",
        (),
        {
            "query": "PRIVATE_REQUEST_TEXT",
            "segment": "main",
            "difficulty": None,
            "hand": None,
            "max_events": 8,
            "max_duration_seconds": 8,
        },
    )()

    with pytest.raises(AuthoringProviderUnavailable) as caught:
        provider.propose(request, control=AuthoringControl())

    assert caught.value.category == category
    assert str(caught.value) == "Responses request failed"
    assert "PRIVATE_PROVIDER_DETAIL" not in str(caught.value)
    assert "PRIVATE_REQUEST_TEXT" not in str(caught.value)


def test_openai_adapter_treats_hostile_error_metadata_as_generic_service_failure() -> None:
    class HostileStatus:
        def __eq__(self, other):
            del other
            raise AssertionError("status comparison must not run")

    class HostileCode(str):
        def lower(self):
            raise AssertionError("code normalization must not run")

    error = sdk_error("HostileProviderError")
    error.status_code = HostileStatus()  # type: ignore[attr-defined]
    error.code = HostileCode("model_not_found")  # type: ignore[attr-defined]
    provider = OpenAILessonAuthoringProvider(
        client=RaisingClient(error),
        model_version="injected",
    )
    request = type(
        "Request",
        (),
        {
            "query": "PRIVATE_REQUEST_TEXT",
            "segment": "main",
            "difficulty": None,
            "hand": None,
            "max_events": 8,
            "max_duration_seconds": 8,
        },
    )()

    with pytest.raises(AuthoringProviderUnavailable) as caught:
        provider.propose(request, control=AuthoringControl())

    assert caught.value.category == "service"


@pytest.mark.parametrize(
    ("category", "message_fragment", "resolution_fragment", "retryable"),
    (
        ("authentication", "rejected the configured API key", "Settings", False),
        ("permission", "cannot use the configured lesson model", "available", False),
        ("model", "lesson model is not available", "Remove HALCYON_COACH_MODEL", False),
        ("quota", "quota is unavailable", "billing", False),
        ("rate_limit", "rate-limited", "Wait briefly", True),
        ("timeout", "took longer", "Retry once", True),
        ("connection", "could not reach OpenAI", "network", True),
        ("request", "structured lesson request", "update Halcyon", False),
        ("service", "temporarily unavailable", "Retry shortly", True),
    ),
)
def test_lesson_service_exposes_only_fixed_provider_failure_guidance(
    tmp_path,
    category,
    message_fragment,
    resolution_fragment,
    retryable,
) -> None:
    failure = AuthoringProviderUnavailable(
        "PRIVATE_PROVIDER_DETAIL sk-private /private/path",
        category=category,
    )

    with pytest.raises(Exception) as caught:
        service(tmp_path, FakeProvider(error=failure)).resolve(
            bounded_request(query="PRIVATE_REQUEST_TEXT")
        )

    assert caught.value.code == "AUTHORING_UNAVAILABLE"
    assert message_fragment in str(caught.value)
    assert resolution_fragment in caught.value.resolution
    assert caught.value.retryable is retryable
    assert caught.value.__suppress_context__ is True
    assert caught.value.__cause__ is None
    rendered = f"{caught.value} {caught.value.resolution}"
    assert "PRIVATE_PROVIDER_DETAIL" not in rendered
    assert "PRIVATE_REQUEST_TEXT" not in rendered
    assert "sk-private" not in rendered
    assert "/private/path" not in rendered
    assert not (tmp_path / "coach" / "lessons").exists()


@pytest.mark.parametrize("hostile_category", ([], {"unhashable": "mapping"}))
def test_public_provider_failure_treats_non_string_category_as_generic_service(
    tmp_path,
    hostile_category,
) -> None:
    failure = AuthoringProviderUnavailable("PRIVATE_PROVIDER_DETAIL")
    failure.category = hostile_category

    with pytest.raises(Exception) as caught:
        service(tmp_path, FakeProvider(error=failure)).resolve(bounded_request())

    assert str(caught.value) == "The OpenAI lesson-authoring service is temporarily unavailable."


def test_public_provider_failure_treats_hostile_category_getter_as_generic_service(
    tmp_path,
) -> None:
    class HostileCategoryFailure(AuthoringProviderUnavailable):
        @property
        def category(self):
            raise AssertionError("category getter must not escape")

        @category.setter
        def category(self, value):
            del value

    failure = HostileCategoryFailure("PRIVATE_PROVIDER_DETAIL")

    with pytest.raises(Exception) as caught:
        service(tmp_path, FakeProvider(error=failure)).resolve(bounded_request())

    assert str(caught.value) == "The OpenAI lesson-authoring service is temporarily unavailable."


def test_openai_adapter_handles_refusal_malformed_and_incomplete_explicitly() -> None:
    refusal = OpenAILessonAuthoringProvider(
        client=FakeClient(
            {
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "refusal", "refusal": "declined"}],
                    }
                ],
            }
        ),
        model_version="injected",
    )
    malformed = OpenAILessonAuthoringProvider(
        client=FakeClient({"status": "completed", "output_text": "not-json"}),
        model_version="injected",
    )
    incomplete = OpenAILessonAuthoringProvider(
        client=FakeClient({"status": "incomplete", "output": []}),
        model_version="injected",
    )
    request = type(
        "Request",
        (),
        {
            "query": "x",
            "segment": "y",
            "difficulty": None,
            "hand": None,
            "max_events": 512,
            "max_duration_seconds": 120,
        },
    )()

    with pytest.raises(AuthoringProviderRefusal):
        refusal.propose(request, control=AuthoringControl())
    with pytest.raises(AuthoringProviderMalformed):
        malformed.propose(request, control=AuthoringControl())
    with pytest.raises(AuthoringProviderMalformed):
        incomplete.propose(request, control=AuthoringControl())


def test_openai_environment_adapter_uses_default_model_and_never_reads_dotenv(monkeypatch) -> None:
    monkeypatch.delenv("HALCYON_COACH_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(AuthoringProviderUnavailable):
        OpenAILessonAuthoringProvider.from_environment()

    client = FakeClient({"status": "completed", "output_text": json.dumps(valid_proposal())})
    defaulted = OpenAILessonAuthoringProvider.from_environment(client=client)
    assert DEFAULT_COACH_MODEL == "gpt-5.6-luna"
    assert defaulted.client is client
    assert defaulted.model_version == DEFAULT_COACH_MODEL

    injected = OpenAILessonAuthoringProvider.from_environment(
        client=client, model="explicit-test-model"
    )
    assert injected.client is client
    assert injected.model_version == "explicit-test-model"


def test_openai_environment_adapter_accepts_explicit_stored_key(monkeypatch) -> None:
    monkeypatch.setenv("HALCYON_COACH_MODEL", "coach-test-model")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    created: list[str] = []

    class FakeOpenAI:
        def __init__(self, *, api_key: str) -> None:
            created.append(api_key)

    monkeypatch.setattr("openai.OpenAI", FakeOpenAI)

    provider = OpenAILessonAuthoringProvider.from_environment(api_key="  stored-key  ")

    assert created == ["stored-key"]
    assert isinstance(provider.client, FakeOpenAI)
    assert provider.model_version == "coach-test-model"


def test_openai_provider_repr_never_delegates_to_client_repr() -> None:
    marker = "PRIVATE_CLIENT_REPR_CREDENTIAL"

    class HostileClient:
        def __repr__(self) -> str:
            return marker

    provider = OpenAILessonAuthoringProvider(
        client=HostileClient(),
        model_version="repr-test-model",
    )

    assert marker not in repr(provider)
    assert "client=" not in repr(provider)


def test_provider_transport_uses_remaining_deadline_and_disables_retries() -> None:
    client = FakeClient({"status": "completed", "output_text": json.dumps(valid_proposal())})
    provider = OpenAILessonAuthoringProvider(
        client=client,
        model_version="injected",
        transport_timeout_seconds=20,
    )
    control = AuthoringControl(deadline_monotonic=105.0, clock=lambda: 100.0)
    request = type(
        "Request",
        (),
        {
            "query": "x",
            "segment": "Main Melody",
            "difficulty": None,
            "hand": None,
            "max_events": 512,
            "max_duration_seconds": 120,
        },
    )()

    provider.propose(request, control=control)

    assert client.options == [{"timeout": 5.0, "max_retries": 0}]
    with pytest.raises(ValueError, match="within"):
        OpenAILessonAuthoringProvider(
            client=client,
            model_version="injected",
            transport_timeout_seconds=61,
        )


def test_elapsed_authoring_deadline_surfaces_session_cancelled_without_provider_call(
    tmp_path,
) -> None:
    provider = FakeProvider()
    control = AuthoringControl(deadline_monotonic=10.0, clock=lambda: 11.0)

    with pytest.raises(Exception) as caught:
        service(tmp_path, provider).resolve(bounded_request(), control=control)

    assert caught.value.code == "SESSION_CANCELLED"
    assert control.reason == "timeout"
    assert control.state == "finished"
    assert control.cancel_event.is_set()
    assert provider.calls == 0
    assert not (tmp_path / "coach" / "lessons").exists()


class BlockingProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def propose(self, request, *, control):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(2)
        return deepcopy(self.result)


class CooperativeBlockingProvider(FakeProvider):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()

    def propose(self, request, *, control):
        self.calls += 1
        self.entered.set()
        assert control.cancel_event.wait(2)
        raise AuthoringProviderUnavailable("transport observed cancellation")


def test_cooperative_provider_observes_stop_and_persists_nothing(tmp_path) -> None:
    provider = CooperativeBlockingProvider()
    lesson_service = service(tmp_path, provider)
    control = AuthoringControl()
    outcomes: list[object] = []

    def resolve() -> None:
        try:
            outcomes.append(lesson_service.resolve(bounded_request(), control=control))
        except Exception as exc:
            outcomes.append(exc)

    worker = threading.Thread(target=resolve)
    worker.start()
    assert provider.entered.wait(1)
    assert control.request_cancel("stop") is True
    worker.join(2)

    assert not worker.is_alive()
    assert len(outcomes) == 1
    assert getattr(outcomes[0], "code", None) == "SESSION_CANCELLED"
    assert control.state == "finished"
    assert control.reason == "stop"
    assert not (lesson_service.store.root / "lessons").exists()


def test_late_uncooperative_provider_stays_running_then_cannot_persist(tmp_path) -> None:
    provider = BlockingProvider()
    lesson_service = service(tmp_path, provider)
    control = AuthoringControl()
    outcomes: list[object] = []

    def resolve() -> None:
        try:
            outcomes.append(lesson_service.resolve(bounded_request(), control=control))
        except Exception as exc:
            outcomes.append(exc)

    worker = threading.Thread(target=resolve)
    worker.start()
    assert provider.entered.wait(1)

    assert control.request_cancel("stop") is True
    assert control.state == "cancel_requested"
    assert control.last_checkpoint == "authoring.before_provider"
    assert worker.is_alive()
    assert not (lesson_service.store.root / "lessons").exists()

    provider.release.set()
    worker.join(2)
    assert not worker.is_alive()
    assert len(outcomes) == 1
    assert getattr(outcomes[0], "code", None) == "SESSION_CANCELLED"
    assert control.state == "finished"
    assert control.reason == "stop"
    assert not (lesson_service.store.root / "lessons").exists()


def test_cancel_after_normalization_wins_before_persistence(tmp_path, monkeypatch) -> None:
    lesson_service = service(tmp_path, FakeProvider())
    control = AuthoringControl()
    normalized = threading.Event()
    release = threading.Event()
    outcomes: list[object] = []
    real_normalize = coach_authoring.normalize_authoring_proposal

    def blocking_normalize(*args, **kwargs):
        lesson = real_normalize(*args, **kwargs)
        normalized.set()
        assert release.wait(2)
        return lesson

    monkeypatch.setattr(coach_authoring, "normalize_authoring_proposal", blocking_normalize)

    def resolve() -> None:
        try:
            outcomes.append(lesson_service.resolve(bounded_request(), control=control))
        except Exception as exc:
            outcomes.append(exc)

    worker = threading.Thread(target=resolve)
    worker.start()
    assert normalized.wait(1)
    assert control.request_cancel("stop") is True
    release.set()
    worker.join(2)

    assert getattr(outcomes[0], "code", None) == "SESSION_CANCELLED"
    assert not (lesson_service.store.root / "lessons").exists()


def test_commit_fence_wins_over_late_cancel_and_finishes_canonical_reload(
    tmp_path, monkeypatch
) -> None:
    lesson_service = service(tmp_path, FakeProvider())
    control = AuthoringControl()
    entered_persist = threading.Event()
    release_persist = threading.Event()
    outcomes: list[object] = []
    real_persist = lesson_service.store.persist

    def blocking_persist(*args, **kwargs):
        entered_persist.set()
        assert release_persist.wait(2)
        return real_persist(*args, **kwargs)

    monkeypatch.setattr(lesson_service.store, "persist", blocking_persist)

    def resolve() -> None:
        try:
            outcomes.append(lesson_service.resolve(bounded_request(), control=control))
        except Exception as exc:
            outcomes.append(exc)

    worker = threading.Thread(target=resolve)
    worker.start()
    assert entered_persist.wait(1)

    assert control.state == "committing"
    assert control.request_cancel("stop") is False
    release_persist.set()
    worker.join(2)

    assert not worker.is_alive()
    assert len(outcomes) == 1
    assert getattr(outcomes[0], "authored", False) is True
    assert control.state == "finished"
    assert control.reason is None
    assert control.last_checkpoint == "authoring.finished"


def test_settled_cache_hit_and_failure_reject_late_cancellation(tmp_path) -> None:
    provider = FakeProvider()
    lesson_service = service(tmp_path, provider)
    lesson_service.resolve(bounded_request())

    cache_control = AuthoringControl()
    cached = lesson_service.resolve(
        bounded_request(author=AuthorMode.NEVER, difficulty=None, hand=None),
        control=cache_control,
    )
    assert cached.cache_hit is True
    assert cache_control.state == "finished"
    assert cache_control.request_cancel("stop") is False

    failure_control = AuthoringControl()
    missing_service = service(tmp_path / "missing", FakeProvider())
    with pytest.raises(Exception) as caught:
        missing_service.resolve(bounded_request(author=AuthorMode.NEVER), control=failure_control)
    assert caught.value.code == "SOURCE_REQUIRED"
    assert failure_control.state == "finished"
    assert failure_control.request_cancel("stop") is False


def test_cancel_after_last_cache_checkpoint_overrides_pending_success(
    tmp_path, monkeypatch
) -> None:
    lesson_service = service(tmp_path, FakeProvider())
    lesson_service.resolve(bounded_request())
    control = AuthoringControl()
    timeline_entered = threading.Event()
    timeline_release = threading.Event()
    outcomes: list[object] = []
    real_resolve_timeline = coach_authoring.resolve_timeline

    def blocking_timeline(*args, **kwargs):
        timeline = real_resolve_timeline(*args, **kwargs)
        timeline_entered.set()
        assert timeline_release.wait(2)
        return timeline

    monkeypatch.setattr(coach_authoring, "resolve_timeline", blocking_timeline)

    def resolve() -> None:
        try:
            outcomes.append(
                lesson_service.resolve(
                    bounded_request(author=AuthorMode.NEVER, difficulty=None, hand=None),
                    control=control,
                )
            )
        except Exception as exc:
            outcomes.append(exc)

    worker = threading.Thread(target=resolve)
    worker.start()
    assert timeline_entered.wait(1)
    assert control.last_checkpoint == "resolve.after_cache"
    assert control.request_cancel("stop") is True
    timeline_release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert len(outcomes) == 1
    assert getattr(outcomes[0], "code", None) == "SESSION_CANCELLED"
    assert control.state == "finished"
    assert control.reason == "stop"
