"""External, content-addressed storage for immutable coach lessons."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping

from halcyon.coach_models import (
    LESSON_SCHEMA_VERSION,
    CoachError,
    LessonRevision,
    LessonValidationError,
    normalize_lesson_alias,
    validate_lesson_revision,
)

CATALOG_SCHEMA_VERSION = 1
CATALOG_FILENAME = "catalog-v1.json"
ALIAS_BINDING_SCHEMA_VERSION = 1
ALIAS_BINDING_DIRNAME = "aliases-v1"
ALIAS_SUPERSESSION_SCHEMA_VERSION = 1
ALIAS_SUPERSESSION_DIRNAME = "supersessions-v1"


def default_coach_path() -> Path:
    """Return the external coach data root specified by ADR-5."""

    if override := os.environ.get("HALCYON_COACH_PATH"):
        return Path(override).expanduser()
    if data_home := os.environ.get("XDG_DATA_HOME"):
        root = Path(data_home).expanduser()
    else:
        root = Path.home() / ".local" / "share"
    return root / "project-halcyon" / "coach"


@dataclass(frozen=True)
class LessonReference:
    lesson_id: str
    content_id: str

    def to_dict(self) -> dict[str, str]:
        return {"lesson_id": self.lesson_id, "content_id": self.content_id}


@dataclass(frozen=True)
class AliasBinding:
    schema_version: int
    alias: str
    lesson_id: str
    content_id: str

    def to_dict(self) -> dict[str, str | int]:
        return {
            "schema_version": self.schema_version,
            "alias": self.alias,
            "lesson_id": self.lesson_id,
            "content_id": self.content_id,
        }

    @property
    def reference(self) -> LessonReference:
        return LessonReference(self.lesson_id, self.content_id)


@dataclass(frozen=True)
class AliasSupersession:
    schema_version: int
    alias: str
    expected: LessonReference
    replacement: LessonReference

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "alias": self.alias,
            "expected": self.expected.to_dict(),
            "replacement": self.replacement.to_dict(),
        }


@dataclass(frozen=True)
class ExcludedLessonReference:
    code: str
    lesson_id: str
    content_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "lesson_id": self.lesson_id,
            "content_id": self.content_id,
        }


@dataclass(frozen=True)
class CatalogDiagnostic:
    code: str
    lesson_id: str
    content_id: str


@dataclass
class LessonStore:
    """Versioned catalog plus immutable revision manifests outside the repository."""

    root: Path = field(default_factory=default_coach_path)
    builtins: tuple[LessonRevision, ...] = ()
    _builtin_aliases: dict[str, LessonRevision] = field(init=False, default_factory=dict)
    _diagnostics: list[CatalogDiagnostic] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser()
        self.builtins = tuple(self.builtins)
        for lesson in self.builtins:
            validate_lesson_revision(lesson)
            for alias in lesson.aliases:
                self._builtin_aliases.setdefault(alias, lesson)

    @property
    def catalog_path(self) -> Path:
        return self.root / CATALOG_FILENAME

    def revision_path(self, lesson_id: str, content_id: str) -> Path:
        _validate_reference_parts(lesson_id, content_id)
        return self.root / "lessons" / lesson_id / f"{content_id}.json"

    def alias_binding_path(self, alias: str) -> Path:
        normalized = normalize_lesson_alias(alias)
        return self.root / ALIAS_BINDING_DIRNAME / f"{_alias_digest(normalized)}.json"

    def alias_supersession_path(self, alias: str) -> Path:
        normalized = normalize_lesson_alias(alias)
        return self.root / ALIAS_SUPERSESSION_DIRNAME / f"{_alias_digest(normalized)}.json"

    def diagnostics(self) -> tuple[CatalogDiagnostic, ...]:
        return tuple(self._diagnostics)

    def is_builtin_alias(self, alias: str) -> bool:
        """Return whether a normalized request is owned by a packaged lesson."""

        return normalize_lesson_alias(alias) in self._builtin_aliases

    def resolve(self, request_alias: str) -> LessonRevision | None:
        """Resolve a normalized alias, with packaged built-ins taking precedence."""

        alias = normalize_lesson_alias(request_alias)
        if builtin := self._builtin_aliases.get(alias):
            return builtin
        catalog = self._load_catalog()
        reference = self._resolve_local_reference(alias, catalog)
        supersession_path = self.alias_supersession_path(alias)
        if supersession_path.exists():
            supersession = _read_alias_supersession(
                supersession_path,
                expected_alias=alias,
            )
            if reference is None:
                raise CoachError(
                    "LESSON_CORRUPT",
                    "A lesson alias supersession has no matching base binding.",
                    resolution="Leave it untouched and restore the immutable base binding.",
                )
            if reference != supersession.expected:
                raise CoachError(
                    "LESSON_CORRUPT",
                    "A lesson alias supersession does not match its immutable base binding.",
                    resolution="Leave both records untouched and restore their expected identities.",
                )
            base = self.load_revision(reference.lesson_id, reference.content_id)
            replacement = self.load_revision(
                supersession.replacement.lesson_id,
                supersession.replacement.content_id,
            )
            if base.lesson_id != replacement.lesson_id:
                raise CoachError(
                    "LESSON_CORRUPT",
                    "A lesson alias supersession crosses immutable lesson identities.",
                    resolution="Restore a replacement revision within the original lesson identity.",
                )
            return replacement
        if reference is None:
            return None
        return self.load_revision(reference.lesson_id, reference.content_id)

    def _resolve_local_reference(
        self,
        alias: str,
        catalog: dict[str, Any],
    ) -> LessonReference | None:
        binding_path = self.alias_binding_path(alias)
        if binding_path.exists():
            binding = _read_alias_binding(binding_path, expected_alias=alias)
            if catalog["aliases"].get(alias) != binding.reference.to_dict():
                self.rebuild_catalog()
            return binding.reference
        for attempt in range(2):
            raw_reference = catalog["aliases"].get(alias)
            if raw_reference is not None:
                return _reference_from_dict(raw_reference)
            raw_excluded = catalog["excluded_aliases"].get(alias)
            if raw_excluded is not None:
                excluded = _excluded_reference_from_dict(raw_excluded)
                raise CoachError(
                    excluded.code,
                    "A matching saved lesson is corrupt or uses a newer schema.",
                    resolution=(
                        "Leave it untouched, restore compatible schema-1 content, then rebuild."
                    ),
                )
            if attempt == 0:
                if not (self.root / "lessons").exists():
                    return None
                catalog = self.rebuild_catalog()
        return None

    def supersede_alias(
        self,
        alias: str,
        *,
        expected: LessonReference,
        replacement: LessonReference,
    ) -> LessonRevision:
        """Atomically install one write-once local supersession for an immutable alias."""

        normalized = normalize_lesson_alias(alias)
        _validate_reference(expected)
        _validate_reference(replacement)
        if normalized in self._builtin_aliases:
            raise _supersession_conflict(
                "Packaged lesson aliases cannot be superseded.",
                "Use a distinct local alias for the imported lesson.",
            )
        if expected == replacement:
            raise _supersession_conflict(
                "A lesson alias cannot supersede itself.",
                "Provide a different persisted revision within the same lesson identity.",
            )
        if expected.lesson_id != replacement.lesson_id:
            raise _supersession_conflict(
                "A lesson alias supersession must preserve its lesson identity.",
                "Persist the replacement under the base lesson ID and retry.",
            )

        binding_path = self.alias_binding_path(normalized)
        if not binding_path.exists():
            raise _supersession_conflict(
                "The lesson alias has no immutable local binding to supersede.",
                "Persist the base lesson first or use a distinct imported alias.",
            )
        binding = _read_alias_binding(binding_path, expected_alias=normalized)
        if binding.reference != expected:
            raise _supersession_conflict(
                "The lesson alias no longer matches the expected base revision.",
                "Reload the current reference before retrying the local import.",
            )

        self.load_revision(expected.lesson_id, expected.content_id)
        replacement_path = self.revision_path(
            replacement.lesson_id,
            replacement.content_id,
        )
        if not replacement_path.exists():
            raise CoachError(
                "LESSON_CORRUPT",
                "The replacement lesson revision is missing.",
                resolution="Persist and validate the replacement revision before superseding.",
            )
        canonical = _read_revision(
            replacement_path,
            expected_lesson_id=replacement.lesson_id,
            expected_content_id=replacement.content_id,
        )
        supersession = AliasSupersession(
            schema_version=ALIAS_SUPERSESSION_SCHEMA_VERSION,
            alias=normalized,
            expected=expected,
            replacement=replacement,
        )
        supersession_path = self.alias_supersession_path(normalized)
        if supersession_path.exists():
            existing = _read_alias_supersession(
                supersession_path,
                expected_alias=normalized,
            )
            if existing == supersession:
                return canonical
            raise _supersession_conflict(
                "This lesson alias already has a different write-once supersession.",
                "Use the existing replacement or a distinct alias.",
            )

        if _atomic_create_json(supersession_path, supersession.to_dict()):
            return canonical
        existing = _read_alias_supersession(
            supersession_path,
            expected_alias=normalized,
        )
        if existing == supersession:
            return canonical
        raise _supersession_conflict(
            "A concurrent import installed a different alias supersession.",
            "Reload the current replacement before retrying.",
        )

    def load_revision(self, lesson_id: str, content_id: str) -> LessonRevision:
        for builtin in self.builtins:
            if builtin.lesson_id == lesson_id and builtin.content_id == content_id:
                return builtin
        path = self.revision_path(lesson_id, content_id)
        if not path.exists():
            raise CoachError(
                "LESSON_CORRUPT",
                "The saved lesson revision is missing.",
                resolution="Remove its catalog alias or restore a valid schema-1 revision.",
            )
        return _read_revision(path, expected_lesson_id=lesson_id, expected_content_id=content_id)

    def persist(self, lesson: LessonRevision, *, aliases: Iterable[str] = ()) -> LessonRevision:
        """Persist an immutable revision first and atomically commit its aliases last."""

        validate_lesson_revision(lesson)
        # Conflict detection is deliberately read-only. In particular, a corrupt
        # derived catalog must not be rebuilt before we know that every requested
        # immutable binding is admissible.
        catalog = self._catalog_for_persist_preflight()
        alias_values = tuple(lesson.aliases) + tuple(aliases)
        normalized_aliases = tuple(
            dict.fromkeys(normalize_lesson_alias(alias) for alias in alias_values)
        )
        catalog_aliases = dict(catalog["aliases"])
        reference = LessonReference(lesson.lesson_id, lesson.content_id)
        bindings_to_write: list[tuple[Path, AliasBinding]] = []
        for alias in normalized_aliases:
            if alias in self._builtin_aliases:
                continue
            binding_path = self.alias_binding_path(alias)
            binding_exists = binding_path.exists()
            if binding_exists:
                existing_reference = _read_alias_binding(
                    binding_path, expected_alias=alias
                ).reference
            else:
                raw_existing = catalog_aliases.get(alias)
                existing_reference = (
                    _reference_from_dict(raw_existing) if raw_existing is not None else None
                )
            if existing_reference is not None and existing_reference != reference:
                raise CoachError(
                    "ALIAS_CONFLICT",
                    "This lesson alias already identifies a different immutable revision.",
                    field="aliases",
                    resolution="Use a distinct alias; the existing cache identity was preserved.",
                )
            if not binding_exists:
                bindings_to_write.append(
                    (
                        binding_path,
                        AliasBinding(
                            schema_version=ALIAS_BINDING_SCHEMA_VERSION,
                            alias=alias,
                            lesson_id=reference.lesson_id,
                            content_id=reference.content_id,
                        ),
                    )
                )

        path = self.revision_path(lesson.lesson_id, lesson.content_id)
        if path.exists():
            persisted_lesson = _read_revision(
                path,
                expected_lesson_id=lesson.lesson_id,
                expected_content_id=lesson.content_id,
            )
        elif _atomic_create_json(path, lesson.to_dict()):
            persisted_lesson = lesson
        else:
            persisted_lesson = _read_revision(
                path,
                expected_lesson_id=lesson.lesson_id,
                expected_content_id=lesson.content_id,
            )

        for binding_path, binding in bindings_to_write:
            if _atomic_create_json(binding_path, binding.to_dict()):
                continue
            existing = _read_alias_binding(
                binding_path,
                expected_alias=binding.alias,
            )
            if existing != binding:
                raise CoachError(
                    "ALIAS_CONFLICT",
                    "This lesson alias was concurrently bound to a different immutable revision.",
                    field="aliases",
                    resolution=(
                        "Use a distinct alias; the concurrent cache identity was preserved."
                    ),
                )
        committed = self._scan_manifests()
        _atomic_write_json(self.catalog_path, committed)
        return self.load_revision(persisted_lesson.lesson_id, persisted_lesson.content_id)

    def _catalog_for_persist_preflight(self) -> dict[str, Any]:
        if not self.catalog_path.exists():
            return self._scan_manifests()
        try:
            payload = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._scan_manifests()
        try:
            return _validate_catalog(payload)
        except CoachError as exc:
            if exc.code == "SCHEMA_UNSUPPORTED":
                raise
            return self._scan_manifests()

    def rebuild_catalog(self) -> dict[str, Any]:
        """Rebuild aliases only from manifests that fully validate."""

        catalog = self._scan_manifests()
        _atomic_write_json(self.catalog_path, catalog)
        return catalog

    def _load_catalog(self) -> dict[str, Any]:
        if not self.catalog_path.exists():
            if (self.root / "lessons").exists():
                return self.rebuild_catalog()
            return self._scan_manifests()
        try:
            payload = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self.rebuild_catalog()
        try:
            catalog = _validate_catalog(payload)
        except CoachError as exc:
            if exc.code == "SCHEMA_UNSUPPORTED":
                raise
            return self.rebuild_catalog()
        return catalog

    def _scan_manifests(self) -> dict[str, Any]:
        self._diagnostics.clear()
        aliases: dict[str, dict[str, str]] = {}
        excluded_aliases: dict[str, dict[str, str]] = {}
        invalid_binding_hashes: dict[str, str] = {}
        valid_lessons: dict[tuple[str, str], LessonRevision] = {}
        invalid_references: dict[tuple[str, str], ExcludedLessonReference] = {}
        lessons_root = self.root / "lessons"
        if lessons_root.exists():
            for path in sorted(lessons_root.glob("*/*.json")):
                lesson_id = path.parent.name
                content_id = path.stem
                try:
                    _validate_reference_parts(lesson_id, content_id)
                    lesson = _read_revision(
                        path,
                        expected_lesson_id=lesson_id,
                        expected_content_id=content_id,
                    )
                except CoachError as exc:
                    self._diagnostics.append(
                        CatalogDiagnostic(exc.code, lesson_id[:96], content_id[:64])
                    )
                    try:
                        _validate_reference_parts(lesson_id, content_id)
                    except CoachError:
                        continue
                    excluded = ExcludedLessonReference(
                        code=exc.code,
                        lesson_id=lesson_id,
                        content_id=content_id,
                    )
                    invalid_references[(lesson_id, content_id)] = excluded
                    for alias in _manifest_aliases(path):
                        excluded_aliases.setdefault(alias, excluded.to_dict())
                    continue
                valid_lessons[(lesson.lesson_id, lesson.content_id)] = lesson

        bindings_root = self.root / ALIAS_BINDING_DIRNAME
        if bindings_root.exists():
            for path in sorted(bindings_root.glob("*.json")):
                try:
                    binding = _read_alias_binding(path)
                except CoachError as exc:
                    invalid_binding_hashes[path.stem] = exc.code
                    recovered = _recover_alias_binding(path)
                    if recovered is None:
                        continue
                    alias, lesson_id, content_id = recovered
                    self._diagnostics.append(
                        CatalogDiagnostic(exc.code, lesson_id[:96], content_id[:64])
                    )
                    excluded_aliases.setdefault(
                        alias,
                        ExcludedLessonReference(exc.code, lesson_id, content_id).to_dict(),
                    )
                    continue
                if binding.alias in self._builtin_aliases:
                    continue
                key = (binding.lesson_id, binding.content_id)
                if key in valid_lessons:
                    aliases[binding.alias] = binding.reference.to_dict()
                    excluded_aliases.pop(binding.alias, None)
                    continue
                excluded = invalid_references.get(
                    key,
                    ExcludedLessonReference(
                        "LESSON_CORRUPT", binding.lesson_id, binding.content_id
                    ),
                )
                excluded_aliases[binding.alias] = excluded.to_dict()

        for key in sorted(valid_lessons):
            lesson = valid_lessons[key]
            reference = LessonReference(lesson.lesson_id, lesson.content_id).to_dict()
            for alias in lesson.aliases:
                if code := invalid_binding_hashes.get(_alias_digest(alias)):
                    excluded_aliases.setdefault(
                        alias,
                        ExcludedLessonReference(
                            code, lesson.lesson_id, lesson.content_id
                        ).to_dict(),
                    )
                    continue
                if alias not in self._builtin_aliases and alias not in excluded_aliases:
                    aliases.setdefault(alias, reference)
        for alias in aliases:
            excluded_aliases.pop(alias, None)
        return {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "aliases": dict(sorted(aliases.items())),
            "excluded_aliases": dict(sorted(excluded_aliases.items())),
        }


def _read_revision(
    path: Path, *, expected_lesson_id: str, expected_content_id: str
) -> LessonRevision:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoachError(
            "LESSON_CORRUPT",
            "A saved lesson revision is not valid JSON.",
            resolution="Leave it untouched, restore a valid manifest, then rebuild the catalog.",
        ) from exc
    if isinstance(payload, Mapping):
        schema = payload.get("schema_version")
        if (
            isinstance(schema, int)
            and not isinstance(schema, bool)
            and schema > LESSON_SCHEMA_VERSION
        ):
            raise CoachError(
                "SCHEMA_UNSUPPORTED",
                f"Lesson schema {schema} is newer than supported schema 1.",
                resolution="Upgrade Halcyon before using this revision; the file was not modified.",
            )
    try:
        lesson = LessonRevision.from_dict(payload)
    except LessonValidationError as exc:
        code = "SCHEMA_UNSUPPORTED" if exc.code == "SCHEMA_UNSUPPORTED" else "LESSON_CORRUPT"
        raise CoachError(
            code,
            "A saved lesson revision failed deterministic validation.",
            field=exc.field,
            resolution="Leave it untouched, restore valid schema-1 content, then rebuild the catalog.",
        ) from exc
    if lesson.lesson_id != expected_lesson_id or lesson.content_id != expected_content_id:
        raise CoachError(
            "LESSON_CORRUPT",
            "A saved revision identity does not match its catalog reference.",
            resolution="Leave it untouched and rebuild the catalog from valid manifests.",
        )
    return lesson


def _alias_digest(alias: str) -> str:
    return hashlib.sha256(alias.encode("utf-8")).hexdigest()


def _read_alias_binding(path: Path, *, expected_alias: str | None = None) -> AliasBinding:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoachError(
            "LESSON_CORRUPT",
            "A lesson alias binding is not valid JSON.",
            resolution="Leave it untouched and restore a valid schema-1 alias binding.",
        ) from exc
    if not isinstance(payload, Mapping):
        raise CoachError("LESSON_CORRUPT", "A lesson alias binding must be an object.")
    schema = payload.get("schema_version")
    if (
        isinstance(schema, int)
        and not isinstance(schema, bool)
        and schema > ALIAS_BINDING_SCHEMA_VERSION
    ):
        raise CoachError(
            "SCHEMA_UNSUPPORTED",
            f"Alias binding schema {schema} is newer than supported schema 1.",
            resolution="Upgrade Halcyon; the binding was not modified.",
        )
    if (
        set(payload) != {"schema_version", "alias", "lesson_id", "content_id"}
        or not isinstance(schema, int)
        or isinstance(schema, bool)
        or schema != ALIAS_BINDING_SCHEMA_VERSION
    ):
        raise CoachError("LESSON_CORRUPT", "A lesson alias binding is invalid.")
    alias = payload["alias"]
    lesson_id = payload["lesson_id"]
    content_id = payload["content_id"]
    if not isinstance(alias, str):
        raise CoachError("LESSON_CORRUPT", "A lesson alias binding is not normalized.")
    try:
        normalized_alias = normalize_lesson_alias(alias)
    except LessonValidationError as exc:
        raise CoachError("LESSON_CORRUPT", "A lesson alias binding is not normalized.") from exc
    if normalized_alias != alias:
        raise CoachError("LESSON_CORRUPT", "A lesson alias binding is not normalized.")
    if expected_alias is not None and alias != expected_alias:
        raise CoachError("LESSON_CORRUPT", "A lesson alias binding has the wrong identity.")
    if path.stem != _alias_digest(alias):
        raise CoachError("LESSON_CORRUPT", "A lesson alias binding filename is invalid.")
    if not isinstance(lesson_id, str) or not isinstance(content_id, str):
        raise CoachError("LESSON_CORRUPT", "A lesson alias binding reference is invalid.")
    _validate_reference_parts(lesson_id, content_id)
    return AliasBinding(ALIAS_BINDING_SCHEMA_VERSION, alias, lesson_id, content_id)


def _read_alias_supersession(
    path: Path,
    *,
    expected_alias: str | None = None,
) -> AliasSupersession:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CoachError(
            "LESSON_CORRUPT",
            "A lesson alias supersession is not valid JSON.",
            resolution="Leave it untouched and restore a valid schema-1 supersession.",
        ) from exc
    if not isinstance(payload, Mapping):
        raise CoachError("LESSON_CORRUPT", "A lesson alias supersession must be an object.")
    schema = payload.get("schema_version")
    if (
        isinstance(schema, int)
        and not isinstance(schema, bool)
        and schema > ALIAS_SUPERSESSION_SCHEMA_VERSION
    ):
        raise CoachError(
            "SCHEMA_UNSUPPORTED",
            f"Alias supersession schema {schema} is newer than supported schema 1.",
            resolution="Upgrade Halcyon; the supersession was not modified.",
        )
    if (
        set(payload) != {"schema_version", "alias", "expected", "replacement"}
        or not isinstance(schema, int)
        or isinstance(schema, bool)
        or schema != ALIAS_SUPERSESSION_SCHEMA_VERSION
    ):
        raise CoachError("LESSON_CORRUPT", "A lesson alias supersession is invalid.")
    alias = payload["alias"]
    if not isinstance(alias, str):
        raise CoachError("LESSON_CORRUPT", "A lesson alias supersession is not normalized.")
    try:
        normalized = normalize_lesson_alias(alias)
    except LessonValidationError as exc:
        raise CoachError(
            "LESSON_CORRUPT",
            "A lesson alias supersession is not normalized.",
        ) from exc
    if normalized != alias:
        raise CoachError("LESSON_CORRUPT", "A lesson alias supersession is not normalized.")
    if expected_alias is not None and alias != expected_alias:
        raise CoachError("LESSON_CORRUPT", "A lesson alias supersession has the wrong identity.")
    if path.stem != _alias_digest(alias):
        raise CoachError("LESSON_CORRUPT", "A lesson alias supersession filename is invalid.")
    try:
        expected = _reference_from_dict(payload["expected"])
        replacement = _reference_from_dict(payload["replacement"])
    except CoachError as exc:
        raise CoachError(
            "LESSON_CORRUPT",
            "A lesson alias supersession reference is invalid.",
            resolution="Leave it untouched and restore valid base and replacement references.",
        ) from exc
    if expected == replacement:
        raise CoachError("LESSON_CORRUPT", "A lesson alias supersession cannot reference itself.")
    if expected.lesson_id != replacement.lesson_id:
        raise CoachError(
            "LESSON_CORRUPT",
            "A lesson alias supersession crosses immutable lesson identities.",
        )
    return AliasSupersession(
        ALIAS_SUPERSESSION_SCHEMA_VERSION,
        alias,
        expected,
        replacement,
    )


def _recover_alias_binding(path: Path) -> tuple[str, str, str] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    alias = payload.get("alias")
    lesson_id = payload.get("lesson_id")
    content_id = payload.get("content_id")
    if (
        not isinstance(alias, str)
        or not isinstance(lesson_id, str)
        or not isinstance(content_id, str)
    ):
        return None
    try:
        normalized = normalize_lesson_alias(alias)
        _validate_reference_parts(lesson_id, content_id)
    except CoachError:
        return None
    return normalized, lesson_id, content_id


def _manifest_aliases(path: Path) -> tuple[str, ...]:
    """Recover only safe aliases for surfacing an excluded manifest on rebuild."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    raw_aliases = payload.get("aliases") if isinstance(payload, Mapping) else None
    if not isinstance(raw_aliases, list):
        return ()
    aliases: list[str] = []
    for raw_alias in raw_aliases:
        if not isinstance(raw_alias, str):
            continue
        try:
            aliases.append(normalize_lesson_alias(raw_alias))
        except LessonValidationError:
            continue
    return tuple(dict.fromkeys(aliases))


def _validate_catalog(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise CoachError("LESSON_CORRUPT", "The lesson catalog must be an object.")
    schema = payload.get("schema_version")
    if isinstance(schema, int) and not isinstance(schema, bool) and schema > CATALOG_SCHEMA_VERSION:
        raise CoachError(
            "SCHEMA_UNSUPPORTED",
            f"Catalog schema {schema} is newer than supported schema 1.",
            resolution="Upgrade Halcyon; the catalog was not modified.",
        )
    if (
        set(payload) != {"schema_version", "aliases", "excluded_aliases"}
        or not isinstance(schema, int)
        or isinstance(schema, bool)
        or schema != CATALOG_SCHEMA_VERSION
    ):
        raise CoachError("LESSON_CORRUPT", "The lesson catalog is invalid.")
    raw_aliases = payload["aliases"]
    raw_excluded_aliases = payload["excluded_aliases"]
    if not isinstance(raw_aliases, Mapping):
        raise CoachError("LESSON_CORRUPT", "The catalog aliases field must be an object.")
    if not isinstance(raw_excluded_aliases, Mapping):
        raise CoachError("LESSON_CORRUPT", "The catalog excluded_aliases field must be an object.")
    aliases: dict[str, dict[str, str]] = {}
    for raw_alias, raw_reference in raw_aliases.items():
        if not isinstance(raw_alias, str) or normalize_lesson_alias(raw_alias) != raw_alias:
            raise CoachError("LESSON_CORRUPT", "The catalog contains a non-normalized alias.")
        aliases[raw_alias] = _reference_from_dict(raw_reference).to_dict()
    excluded_aliases: dict[str, dict[str, str]] = {}
    for raw_alias, raw_reference in raw_excluded_aliases.items():
        if not isinstance(raw_alias, str) or normalize_lesson_alias(raw_alias) != raw_alias:
            raise CoachError("LESSON_CORRUPT", "The catalog contains a non-normalized alias.")
        excluded_aliases[raw_alias] = _excluded_reference_from_dict(raw_reference).to_dict()
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "aliases": aliases,
        "excluded_aliases": excluded_aliases,
    }


def _reference_from_dict(value: Any) -> LessonReference:
    if not isinstance(value, Mapping) or set(value) != {"lesson_id", "content_id"}:
        raise CoachError("LESSON_CORRUPT", "A catalog lesson reference is invalid.")
    lesson_id = value["lesson_id"]
    content_id = value["content_id"]
    if not isinstance(lesson_id, str) or not isinstance(content_id, str):
        raise CoachError("LESSON_CORRUPT", "A catalog lesson reference is invalid.")
    _validate_reference_parts(lesson_id, content_id)
    return LessonReference(lesson_id, content_id)


def _excluded_reference_from_dict(value: Any) -> ExcludedLessonReference:
    if not isinstance(value, Mapping) or set(value) != {"code", "lesson_id", "content_id"}:
        raise CoachError("LESSON_CORRUPT", "An excluded lesson reference is invalid.")
    code = value["code"]
    lesson_id = value["lesson_id"]
    content_id = value["content_id"]
    if code not in {"LESSON_CORRUPT", "SCHEMA_UNSUPPORTED"}:
        raise CoachError("LESSON_CORRUPT", "An excluded lesson error code is invalid.")
    if not isinstance(lesson_id, str) or not isinstance(content_id, str):
        raise CoachError("LESSON_CORRUPT", "An excluded lesson reference is invalid.")
    _validate_reference_parts(lesson_id, content_id)
    return ExcludedLessonReference(code, lesson_id, content_id)


def _validate_reference_parts(lesson_id: str, content_id: str) -> None:
    if (
        not lesson_id
        or len(lesson_id) > 96
        or not all(
            character.islower() or character.isdigit() or character in "._-"
            for character in lesson_id
        )
        or not (lesson_id[0].islower() or lesson_id[0].isdigit())
    ):
        raise CoachError("LESSON_CORRUPT", "A lesson reference has an invalid lesson ID.")
    if len(content_id) != 64 or any(
        character not in "0123456789abcdef" for character in content_id
    ):
        raise CoachError("LESSON_CORRUPT", "A lesson reference has an invalid content ID.")


def _validate_reference(reference: LessonReference) -> None:
    if not isinstance(reference, LessonReference):
        raise CoachError("LESSON_CORRUPT", "A lesson supersession reference is invalid.")
    _validate_reference_parts(reference.lesson_id, reference.content_id)


def _supersession_conflict(message: str, resolution: str) -> CoachError:
    return CoachError(
        "ALIAS_CONFLICT",
        message,
        field="aliases",
        resolution=resolution,
    )


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_create_json(path: Path, payload: Any) -> bool:
    """Publish one fully-written JSON file only if its final name is still absent."""

    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return True
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True
    finally:
        temporary.unlink(missing_ok=True)
