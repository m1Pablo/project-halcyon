"""Cache-first lesson resolution and bounded optional lesson authoring."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import os
import threading
import time
from typing import Any, Callable, Iterator, Literal, Mapping, Protocol, runtime_checkable

from halcyon.coach_models import (
    AUTHORING_LESSON_LIMITS,
    MAX_MAP_ENTRIES,
    Acquisition,
    AuthorMode,
    CoachError,
    Difficulty,
    Fidelity,
    Hand,
    LessonEvent,
    LessonProvenance,
    LessonRequest,
    LessonResolution,
    LessonRevision,
    LessonSegment,
    LessonValidationError,
    TempoChange,
    TimeSignature,
    create_lesson_revision,
    derive_lesson_id,
    normalize_lesson_alias,
    resolve_timeline,
)

_PROPOSAL_KEYS = frozenset(
    {
        "schema_version",
        "title",
        "composer",
        "segment_label",
        "difficulty",
        "hand",
        "ppq",
        "max_polyphony",
        "tempo_map",
        "time_signatures",
        "events",
    }
)
_TEMPO_KEYS = frozenset({"absolute_tick", "microseconds_per_quarter"})
_SIGNATURE_KEYS = frozenset({"absolute_tick", "numerator", "denominator"})
_EVENT_KEYS = frozenset(
    {"event_type", "note", "velocity", "channel", "absolute_tick", "delta_tick"}
)
_LEGACY_EVENT_KEYS = _EVENT_KEYS | {"status"}
_MAX_PROVIDER_TIMEOUT_SECONDS = 60.0
_MAX_CHECKPOINT_LENGTH = 96
DEFAULT_COACH_MODEL = "gpt-5.6-luna"
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 60.0

AuthoringFailureCategory = Literal[
    "authentication",
    "permission",
    "model",
    "quota",
    "rate_limit",
    "timeout",
    "connection",
    "request",
    "service",
]


AUTHORING_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "schema_version": {"type": "integer", "enum": [1]},
        "title": {"type": "string"},
        "composer": {"type": "string"},
        "segment_label": {"type": "string"},
        "difficulty": {
            "type": ["string", "null"],
            "enum": ["foundation", "standard", "challenge", None],
        },
        "hand": {"type": ["string", "null"], "enum": ["left", "right", "both", None]},
        "ppq": {"type": "integer", "minimum": 1, "maximum": 9_600},
        "max_polyphony": {"type": "integer", "minimum": 1, "maximum": 8},
        "tempo_map": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_MAP_ENTRIES,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "absolute_tick": {"type": "integer", "minimum": 0},
                    "microseconds_per_quarter": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 60_000_000,
                    },
                },
                "required": sorted(_TEMPO_KEYS),
            },
        },
        "time_signatures": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_MAP_ENTRIES,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "absolute_tick": {"type": "integer", "minimum": 0},
                    "numerator": {"type": "integer", "minimum": 1, "maximum": 32},
                    "denominator": {"type": "integer", "enum": [1, 2, 4, 8, 16, 32, 64]},
                },
                "required": sorted(_SIGNATURE_KEYS),
            },
        },
        "events": {
            "type": "array",
            "minItems": 2,
            "maxItems": AUTHORING_LESSON_LIMITS.max_events,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "event_type": {"type": "string", "enum": ["note_on", "note_off"]},
                    "note": {"type": "integer", "minimum": 0, "maximum": 127},
                    "velocity": {"type": "integer", "minimum": 0, "maximum": 127},
                    "channel": {"type": "integer", "minimum": 1, "maximum": 16},
                    "absolute_tick": {"type": "integer", "minimum": 0},
                    "delta_tick": {"type": "integer", "minimum": 0},
                },
                "required": sorted(_EVENT_KEYS),
            },
        },
    },
    "required": sorted(_PROPOSAL_KEYS),
}


class AuthoringProviderRefusal(RuntimeError):
    """The provider explicitly declined the bounded request."""


class AuthoringProviderMalformed(RuntimeError):
    """The provider response could not be interpreted as one proposal."""


class AuthoringProviderUnavailable(RuntimeError):
    """The configured authoring provider could not be reached or configured."""

    def __init__(
        self,
        message: str = "Lesson authoring provider unavailable",
        *,
        category: AuthoringFailureCategory = "service",
    ) -> None:
        self.category = category
        super().__init__(message)


class AuthoringControl:
    """Operation-owned cooperative cancellation and commit linearization."""

    def __init__(
        self,
        *,
        deadline_monotonic: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if deadline_monotonic is not None and (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
            or not math.isfinite(deadline_monotonic)
        ):
            raise ValueError("deadline_monotonic must be a finite monotonic timestamp")
        self.deadline_monotonic = (
            float(deadline_monotonic) if deadline_monotonic is not None else None
        )
        self._clock = clock
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._state = "active"
        self._reason: Literal["stop", "timeout"] | None = None
        self._last_checkpoint: str | None = None

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def reason(self) -> Literal["stop", "timeout"] | None:
        with self._lock:
            return self._reason

    @property
    def last_checkpoint(self) -> str | None:
        with self._lock:
            return self._last_checkpoint

    @property
    def cancel_event(self) -> threading.Event:
        """Return the cooperative read/wait event observed by provider workers."""

        return self._cancel_event

    def checkpoint(self, phase: str) -> None:
        if not isinstance(phase, str) or not phase.strip() or len(phase) > _MAX_CHECKPOINT_LENGTH:
            raise ValueError("phase must be bounded non-empty text")
        with self._lock:
            self._last_checkpoint = phase
            self._expire_deadline_locked()
            if self._state == "cancel_requested":
                raise _session_cancelled()
            if self._state == "finished":
                raise RuntimeError("authoring control is already finished")

    def remaining_seconds(self, cap: float) -> float:
        if (
            isinstance(cap, bool)
            or not isinstance(cap, (int, float))
            or not math.isfinite(cap)
            or cap <= 0
        ):
            raise ValueError("cap must be a positive finite duration")
        with self._lock:
            if self._state == "cancel_requested":
                raise _session_cancelled()
            if self._state == "finished":
                raise RuntimeError("authoring control is already finished")
            if self.deadline_monotonic is None:
                return float(cap)
            remaining = self.deadline_monotonic - self._clock()
            if remaining <= 0:
                self._request_timeout_locked()
                raise _session_cancelled()
            return min(float(cap), remaining)

    def request_cancel(self, reason: Literal["stop", "timeout"] = "stop") -> bool:
        if reason not in {"stop", "timeout"}:
            raise ValueError("cancellation reason must be stop or timeout")
        with self._lock:
            self._expire_deadline_locked()
            if self._state != "active":
                return False
            self._state = "cancel_requested"
            self._reason = reason
            self._cancel_event.set()
            return True

    def finish(self) -> None:
        """Atomically settle resolution, letting a pre-settlement cancel win."""

        with self._lock:
            if self._state == "finished":
                return
            if self._state == "committing":
                raise RuntimeError("cannot finish while the commit fence is active")
            self._expire_deadline_locked()
            cancelled = self._state == "cancel_requested"
            self._state = "finished"
            self._last_checkpoint = (
                "authoring.cancelled" if self._reason is not None else "resolve.finished"
            )
        if cancelled:
            raise _session_cancelled()

    @contextmanager
    def commit_fence(self) -> Iterator[None]:
        """Let cancellation win before commit, or make a started commit final."""

        with self._lock:
            self._last_checkpoint = "authoring.committing"
            self._expire_deadline_locked()
            if self._state == "cancel_requested":
                raise _session_cancelled()
            if self._state != "active":
                raise RuntimeError("authoring control cannot enter the commit fence twice")
            self._state = "committing"
        try:
            yield
        finally:
            with self._lock:
                self._state = "finished"
                self._last_checkpoint = "authoring.finished"

    def _expire_deadline_locked(self) -> None:
        if (
            self._state == "active"
            and self.deadline_monotonic is not None
            and self._clock() >= self.deadline_monotonic
        ):
            self._request_timeout_locked()

    def _request_timeout_locked(self) -> None:
        if self._state == "active":
            self._state = "cancel_requested"
            self._reason = "timeout"
            self._cancel_event.set()


def _session_cancelled() -> CoachError:
    return CoachError(
        "SESSION_CANCELLED",
        "Lesson authoring was cancelled.",
        resolution="Start a new coach request when ready.",
        retryable=True,
    )


@dataclass(frozen=True)
class AuthoringRequest:
    query: str
    segment: str
    difficulty: Difficulty | None
    hand: Hand | None
    max_events: int = AUTHORING_LESSON_LIMITS.max_events
    max_duration_seconds: int = AUTHORING_LESSON_LIMITS.max_duration_us // 1_000_000


@runtime_checkable
class LessonAuthoringProvider(Protocol):
    provider_name: str
    model_version: str

    def propose(
        self, request: AuthoringRequest, *, control: AuthoringControl
    ) -> Mapping[str, Any]: ...


@runtime_checkable
class LessonRepository(Protocol):
    def resolve(self, request_alias: str) -> LessonRevision | None: ...

    def persist(
        self, lesson: LessonRevision, *, aliases: tuple[str, ...] = ()
    ) -> LessonRevision: ...

    def load_revision(self, lesson_id: str, content_id: str) -> LessonRevision: ...


@dataclass
class LessonService:
    """Wave 3's only entry for cache resolution and optional authoring."""

    store: LessonRepository
    provider: LessonAuthoringProvider | None = None
    now: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))

    def resolve(
        self,
        request: LessonRequest,
        *,
        control: AuthoringControl | None = None,
    ) -> LessonResolution:
        active_control = control or AuthoringControl()
        try:
            return self._resolve_controlled(request, active_control)
        finally:
            active_control.finish()

    def _resolve_controlled(
        self, request: LessonRequest, active_control: AuthoringControl
    ) -> LessonResolution:
        active_control.checkpoint("resolve.before_cache")
        cached = self.store.resolve(request.normalized_alias)
        active_control.checkpoint("resolve.after_cache")
        if cached is not None:
            return LessonResolution(
                lesson=cached,
                timeline=resolve_timeline(cached, request),
                cache_hit=True,
                authored=False,
                matched_alias=request.normalized_alias,
            )
        if request.author is AuthorMode.NEVER:
            raise CoachError(
                "SOURCE_REQUIRED",
                "No saved lesson matches this cache-only request.",
                field="author",
                resolution=(
                    "Retry without --author never and name the part to learn, for example: "
                    '/coach teach "work title" --segment "main melody".'
                ),
                retryable=True,
            )
        if request.segment is None:
            raise CoachError(
                "SOURCE_REQUIRED",
                "Bounded authoring requires an explicit educational segment.",
                field="segment",
                resolution=(
                    'Name the segment, for example: /coach teach "work title" '
                    '--segment "main melody".'
                ),
                retryable=True,
            )
        provider = self.provider
        if provider is None:
            raise CoachError(
                "AUTHORING_UNAVAILABLE",
                "No lesson authoring provider is configured.",
                resolution="Add an OpenAI API key in Settings, then retry.",
                retryable=True,
            )
        authoring_request = AuthoringRequest(
            query=request.query,
            segment=request.segment,
            difficulty=request.difficulty,
            hand=request.hand,
        )
        active_control.checkpoint("authoring.before_provider")
        try:
            proposal = provider.propose(authoring_request, control=active_control)
        except AuthoringProviderRefusal:
            active_control.checkpoint("authoring.after_provider_error")
            raise CoachError(
                "AUTHORING_REFUSED",
                "The provider declined this bounded lesson request.",
                resolution="Try a different short segment or use a saved lesson.",
            ) from None
        except AuthoringProviderMalformed:
            active_control.checkpoint("authoring.after_provider_error")
            raise CoachError(
                "AUTHORING_INVALID",
                "The provider returned an incomplete or malformed lesson proposal.",
                resolution="Retry once; no lesson was saved.",
                retryable=True,
            ) from None
        except AuthoringProviderUnavailable as exc:
            active_control.checkpoint("authoring.after_provider_error")
            raise _public_authoring_unavailable(exc) from None
        except CoachError:
            active_control.checkpoint("authoring.after_provider_error")
            raise
        except Exception:
            active_control.checkpoint("authoring.after_provider_error")
            raise _public_authoring_unavailable(
                AuthoringProviderUnavailable(category="service")
            ) from None
        active_control.checkpoint("authoring.after_provider")
        try:
            lesson = normalize_authoring_proposal(
                proposal,
                request=request,
                provider_name=provider.provider_name,
                model_version=provider.model_version,
                created_at=_iso_timestamp(self.now()),
            )
        except LessonValidationError as exc:
            raise CoachError(
                "AUTHORING_INVALID",
                "The provider proposal failed deterministic lesson validation.",
                field=exc.field,
                resolution="Retry once; no lesson was saved.",
                retryable=True,
            ) from None
        except CoachError:
            raise
        except Exception:
            raise CoachError(
                "AUTHORING_INVALID",
                "The provider proposal is incompatible with the lesson contract.",
                resolution="Retry; no lesson was saved.",
            ) from None
        active_control.checkpoint("authoring.after_normalization")
        active_control.checkpoint("authoring.before_persist")
        with active_control.commit_fence():
            persisted = self.store.persist(lesson, aliases=(request.normalized_alias,))
            canonical = self.store.load_revision(persisted.lesson_id, persisted.content_id)
        return LessonResolution(
            lesson=canonical,
            timeline=resolve_timeline(canonical, request),
            cache_hit=False,
            authored=True,
            matched_alias=request.normalized_alias,
        )


def normalize_authoring_proposal(
    proposal: Mapping[str, Any],
    *,
    request: LessonRequest,
    provider_name: str,
    model_version: str,
    created_at: str,
) -> LessonRevision:
    """Normalize an untrusted closed proposal into the canonical lesson contract."""

    if not isinstance(proposal, Mapping) or set(proposal) != _PROPOSAL_KEYS:
        raise _invalid("proposal", "proposal has missing or unknown fields")
    if _integer(proposal["schema_version"], "schema_version") != 1:
        raise _invalid("schema_version", "proposal schema must be 1")
    if request.segment is None:
        raise _invalid("segment", "bounded authoring requires an explicit segment")
    title = _text(proposal["title"], "title", 160)
    composer = _text(proposal["composer"], "composer", 160)
    segment_label = _text(proposal["segment_label"], "segment_label", 80)
    if normalize_lesson_alias(segment_label) != normalize_lesson_alias(request.segment):
        raise _invalid("segment_label", "proposal segment does not match the requested segment")
    difficulty = _optional_enum(proposal["difficulty"], Difficulty, "difficulty")
    hand = _optional_enum(proposal["hand"], Hand, "hand")
    if request.difficulty is not None and difficulty is not request.difficulty:
        raise _invalid("difficulty", "proposal difficulty does not match the request")
    if request.hand is not None and hand is not request.hand:
        raise _invalid("hand", "proposal hand does not match the request")
    ppq = _integer(proposal["ppq"], "ppq")
    max_polyphony = _integer(proposal["max_polyphony"], "max_polyphony")
    tempo_map = tuple(
        _proposal_tempo(item, index)
        for index, item in enumerate(_array(proposal["tempo_map"], "tempo_map"))
    )
    signatures = tuple(
        _proposal_signature(item, index)
        for index, item in enumerate(_array(proposal["time_signatures"], "time_signatures"))
    )
    raw_events = _array(proposal["events"], "events")
    if len(raw_events) > AUTHORING_LESSON_LIMITS.max_events:
        raise _invalid(
            "events", f"proposal may contain at most {AUTHORING_LESSON_LIMITS.max_events} events"
        )
    lesson_id = derive_lesson_id(request.query)
    source_port = f"halcyon://coach/{lesson_id}"
    events = tuple(
        _proposal_event(item, index, source_port=source_port)
        for index, item in enumerate(raw_events)
    )
    if not events:
        raise _invalid("events", "proposal must contain MIDI note events")
    authorization = _iso_timestamp(datetime.fromisoformat(created_at.replace("Z", "+00:00")))
    provenance = LessonProvenance(
        acquisition=Acquisition.LOCALLY_AUTHORED_BOUNDED,
        citation=(
            f"Locally authored bounded educational approximation for the “{request.segment}” segment."
        ),
        fidelity=Fidelity.APPROXIMATE,
        authorization_timestamp=authorization,
        provider=_text(provider_name, "provider", 120),
        model_version=_text(model_version, "model_version", 120),
    )
    segment = LessonSegment(
        label=segment_label,
        start_tick=0,
        end_tick=events[-1].absolute_tick,
        hand=hand,
        difficulty=difficulty,
    )
    try:
        return create_lesson_revision(
            lesson_id=lesson_id,
            created_at=created_at,
            aliases=(request.normalized_alias,),
            title=title,
            composer=composer,
            difficulty=difficulty,
            hand=hand,
            ppq=ppq,
            tempo_map=tempo_map,
            time_signatures=signatures,
            provenance=provenance,
            events=events,
            segments=(segment,),
            max_polyphony=max_polyphony,
            limits=AUTHORING_LESSON_LIMITS,
        )
    except CoachError as exc:
        raise CoachError(
            "AUTHORING_INVALID",
            "The provider proposal failed deterministic lesson validation.",
            field=exc.field,
            resolution="Retry with a shorter segment; nothing was saved.",
        ) from None


@dataclass
class OpenAILessonAuthoringProvider:
    """Optional Responses API adapter with explicit-key and environment configuration."""

    client: Any = field(repr=False)
    model_version: str
    provider_name: str = "openai"
    transport_timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if (
            isinstance(self.transport_timeout_seconds, bool)
            or not isinstance(self.transport_timeout_seconds, (int, float))
            or not math.isfinite(self.transport_timeout_seconds)
            or not 0 < self.transport_timeout_seconds <= _MAX_PROVIDER_TIMEOUT_SECONDS
        ):
            raise ValueError(
                f"transport_timeout_seconds must be within (0, {_MAX_PROVIDER_TIMEOUT_SECONDS}]"
            )
        self.transport_timeout_seconds = float(self.transport_timeout_seconds)

    @classmethod
    def from_environment(
        cls,
        *,
        api_key: str | None = None,
        client: Any | None = None,
        model: str | None = None,
    ) -> OpenAILessonAuthoringProvider:
        selected_model = str(
            model or os.environ.get("HALCYON_COACH_MODEL") or DEFAULT_COACH_MODEL
        ).strip()
        if not selected_model:
            selected_model = DEFAULT_COACH_MODEL
        if client is None:
            selected_key = (
                api_key.strip()
                if api_key is not None
                else os.environ.get("OPENAI_API_KEY", "").strip()
            )
            if not selected_key:
                raise AuthoringProviderUnavailable(
                    "OPENAI_API_KEY is not configured",
                    category="authentication",
                )
            try:
                from openai import OpenAI

                client = OpenAI(api_key=selected_key)
            except Exception as exc:
                raise AuthoringProviderUnavailable(
                    "OpenAI client initialization failed",
                    category="service",
                ) from exc
        return cls(client=client, model_version=selected_model)

    def propose(self, request: AuthoringRequest, *, control: AuthoringControl) -> Mapping[str, Any]:
        control.checkpoint("provider.before_transport")
        timeout = control.remaining_seconds(self.transport_timeout_seconds)
        safe_input = json.dumps(
            {
                "request": request.query,
                "segment": request.segment,
                "difficulty": request.difficulty.value if request.difficulty else None,
                "hand": request.hand.value if request.hand else None,
                "limits": {
                    "events": request.max_events,
                    "seconds": request.max_duration_seconds,
                },
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        try:
            request_client = self.client.with_options(timeout=timeout, max_retries=0)
            request_options: dict[str, Any] = {
                "model": self.model_version,
                "instructions": (
                    "Create only a bounded educational MIDI approximation for the explicitly "
                    "requested segment. Do not retrieve files or URLs, claim exact transcription, "
                    "or expand to a full song. Return only the supplied strict schema."
                ),
                "input": safe_input,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "halcyon_lesson_proposal",
                        "strict": True,
                        "schema": AUTHORING_JSON_SCHEMA,
                    }
                },
                "max_output_tokens": 32_000,
                "store": False,
            }
            if self.model_version.lower().startswith("gpt-5"):
                request_options["reasoning"] = {"effort": "low"}
            response = request_client.responses.create(
                **request_options,
            )
        except Exception as exc:
            control.checkpoint("provider.after_transport")
            raise AuthoringProviderUnavailable(
                "Responses request failed",
                category=_classify_openai_failure(exc),
            ) from exc
        control.checkpoint("provider.after_transport")
        return _extract_openai_proposal(response)


def _extract_openai_proposal(response: Any) -> Mapping[str, Any]:
    status = _attribute(response, "status")
    if status not in {None, "completed"}:
        raise AuthoringProviderMalformed("response was incomplete")
    texts: list[str] = []
    for output in _attribute(response, "output") or ():
        if _attribute(output, "type") != "message":
            continue
        for part in _attribute(output, "content") or ():
            part_type = _attribute(part, "type")
            refusal = _attribute(part, "refusal")
            if part_type == "refusal" or refusal:
                raise AuthoringProviderRefusal("provider refusal")
            if part_type == "output_text" and isinstance(_attribute(part, "text"), str):
                texts.append(_attribute(part, "text"))
    if not texts and isinstance(_attribute(response, "output_text"), str):
        texts.append(_attribute(response, "output_text"))
    if not texts:
        raise AuthoringProviderMalformed("response contained no proposal")
    try:
        payload = json.loads("".join(texts))
    except json.JSONDecodeError as exc:
        raise AuthoringProviderMalformed("proposal was not JSON") from exc
    if not isinstance(payload, Mapping):
        raise AuthoringProviderMalformed("proposal was not an object")
    return payload


def _classify_openai_failure(exc: Exception) -> AuthoringFailureCategory:
    """Reduce an SDK failure to one fixed category without inspecting raw text."""

    try:
        raw_error_name = type(exc).__name__
    except Exception:
        raw_error_name = ""
    error_name = raw_error_name if type(raw_error_name) is str else ""
    raw_status = _safe_exception_attribute(exc, "status_code")
    status = raw_status if type(raw_status) is int else None
    raw_code = _safe_exception_attribute(exc, "code")
    code = raw_code[:128].lower() if type(raw_code) is str else ""

    if error_name == "AuthenticationError" or status == 401:
        return "authentication"
    if error_name == "PermissionDeniedError" or status == 403:
        return "permission"
    if error_name == "NotFoundError" or status == 404 or code == "model_not_found":
        return "model"
    if error_name == "RateLimitError" or status == 429:
        if code in {"billing_hard_limit_reached", "insufficient_quota", "usage_limit_reached"}:
            return "quota"
        return "rate_limit"
    if error_name in {"APITimeoutError", "TimeoutError"}:
        return "timeout"
    if error_name == "APIConnectionError":
        return "connection"
    if error_name in {"BadRequestError", "UnprocessableEntityError"} or status in {400, 422}:
        return "request"
    return "service"


def _safe_exception_attribute(exc: Exception, name: str) -> object:
    try:
        return getattr(exc, name, None)
    except Exception:
        return None


def _public_authoring_unavailable(exc: AuthoringProviderUnavailable) -> CoachError:
    details: dict[AuthoringFailureCategory, tuple[str, str, bool]] = {
        "authentication": (
            "OpenAI rejected the configured API key.",
            "Replace the OpenAI key in Settings, then retry.",
            False,
        ),
        "permission": (
            "This OpenAI project cannot use the configured lesson model.",
            "Remove HALCYON_COACH_MODEL or select a model available to this API project.",
            False,
        ),
        "model": (
            "The configured OpenAI lesson model is not available to this API project.",
            "Remove HALCYON_COACH_MODEL to use Halcyon's default lesson model, then retry.",
            False,
        ),
        "quota": (
            "OpenAI API quota is unavailable for lesson authoring.",
            "Check the API project's billing and usage limits, then retry.",
            False,
        ),
        "rate_limit": (
            "OpenAI rate-limited lesson authoring.",
            "Wait briefly, then retry; no lesson was saved.",
            True,
        ),
        "timeout": (
            "OpenAI lesson authoring took longer than Halcyon's time limit.",
            "Retry once; Halcyon's default lesson model is optimized for this short task.",
            True,
        ),
        "connection": (
            "Halcyon could not reach OpenAI for lesson authoring.",
            "Check the network connection, then retry.",
            True,
        ),
        "request": (
            "OpenAI rejected Halcyon's structured lesson request.",
            "Remove an incompatible HALCYON_COACH_MODEL override or update Halcyon, then retry.",
            False,
        ),
        "service": (
            "The OpenAI lesson-authoring service is temporarily unavailable.",
            "Retry shortly; no lesson was saved.",
            True,
        ),
    }
    raw_category = _safe_exception_attribute(exc, "category")
    category = raw_category if type(raw_category) is str and raw_category in details else "service"
    message, resolution, retryable = details[category]
    return CoachError(
        "AUTHORING_UNAVAILABLE",
        message,
        resolution=resolution,
        retryable=retryable,
    )


def _proposal_tempo(value: Any, index: int) -> TempoChange:
    payload = _closed_object(value, _TEMPO_KEYS, f"tempo_map[{index}]")
    return TempoChange(
        _integer(payload["absolute_tick"], f"tempo_map[{index}].absolute_tick"),
        _integer(
            payload["microseconds_per_quarter"],
            f"tempo_map[{index}].microseconds_per_quarter",
        ),
    )


def _proposal_signature(value: Any, index: int) -> TimeSignature:
    payload = _closed_object(value, _SIGNATURE_KEYS, f"time_signatures[{index}]")
    return TimeSignature(
        _integer(payload["absolute_tick"], f"time_signatures[{index}].absolute_tick"),
        _integer(payload["numerator"], f"time_signatures[{index}].numerator"),
        _integer(payload["denominator"], f"time_signatures[{index}].denominator"),
    )


def _proposal_event(value: Any, index: int, *, source_port: str) -> LessonEvent:
    field_name = f"events[{index}]"
    if not isinstance(value, Mapping) or set(value) not in {_EVENT_KEYS, _LEGACY_EVENT_KEYS}:
        raise _invalid(field_name, f"{field_name} has missing or unknown fields")
    payload = value
    event_type = _text(payload["event_type"], f"events[{index}].event_type", 16)
    if event_type not in {"note_on", "note_off"}:
        raise _invalid(f"events[{index}].event_type", "event type must be note_on or note_off")
    channel = _integer(payload["channel"], f"events[{index}].channel")
    if not 1 <= channel <= 16:
        raise _invalid(f"events[{index}].channel", "channel must be between 1 and 16")
    if "status" in payload:
        proposed_status = _integer(payload["status"], f"events[{index}].status")
        if not 0 <= proposed_status <= 255:
            raise _invalid(f"events[{index}].status", "status must be between 0 and 255")
    # Status duplicates event_type and channel. Provider output is untrusted, so
    # derive the canonical byte instead of requiring a model to synchronize its
    # command and channel nibbles perfectly.
    status_command = 0x90 if event_type == "note_on" else 0x80
    return LessonEvent(
        event_id="proposal",
        event_type=event_type,  # type: ignore[arg-type]
        note=_integer(payload["note"], f"events[{index}].note"),
        velocity=_integer(payload["velocity"], f"events[{index}].velocity"),
        channel=channel,
        status=status_command | (channel - 1),
        source_port=source_port,
        content_origin="halcyon",
        absolute_tick=_integer(payload["absolute_tick"], f"events[{index}].absolute_tick"),
        delta_tick=_integer(payload["delta_tick"], f"events[{index}].delta_tick"),
    )


def _closed_object(value: Any, keys: frozenset[str], field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise _invalid(field_name, f"{field_name} has missing or unknown fields")
    return value


def _array(value: Any, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise _invalid(field_name, f"{field_name} must be an array")
    return value


def _integer(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid(field_name, f"{field_name} must be an integer")
    return value


def _text(value: Any, field_name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise _invalid(
            field_name,
            f"{field_name} must be non-empty text no longer than {maximum} characters",
        )
    return value.strip()


def _optional_enum(value: Any, enum_type: Any, field_name: str) -> Any:
    if value is None:
        return None
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise _invalid(field_name, f"{field_name} is incompatible with the request") from exc


def _invalid(field_name: str, message: str) -> CoachError:
    return CoachError(
        "AUTHORING_INVALID",
        message,
        field=field_name,
        resolution="Retry with a shorter segment; nothing was saved.",
    )


def _attribute(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _iso_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
