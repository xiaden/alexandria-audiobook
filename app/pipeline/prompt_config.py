"""Effective prompt/settings config domain for the pipeline (Plan B, S3).

Owns the ``PipelineWalkPromptConfigRevisionAPI.v1`` contract enforced at the
domain layer (see ``CONTRACTS.md``):

* **Nine fixed tasks** — every ``WALK_TASK_NAMES`` task name is addressable;
  ``script_alias_resolution`` remains the canonical alias-resolution task.
* **Precedence** — on-disk config -> ``llm.task_overrides`` -> DB
  ``walk_override`` (DB wins).  Values reuse ``resolve_task_config`` so the
  API always matches what the walk runner actually uses.
* **DB prompt only wins when non-empty** — an empty or non-string DB prompt
  falls through to the config tiers (never wins).
* **Exact allowed keys** — ``model_name``, ``reasoning_effort``,
  ``temperature``, ``prompt``.  Unknown tasks/keys and malformed data are
  rejected deterministically.
* **Side-effect-free validate** — ``validate`` performs no writes.
* **Append-only revisions** — a save cites its ``base_revision`` and is
  rejected with ``StaleRevisionError`` (HTTP 409) when it does not match the
  current head; the prior record is preserved and marked ``superseded_by``.
* **Safe raw JSON** — ``raw_json`` is validated (parsed + allow-list checked)
  rather than merged unsafely.

The domain talks to ``PipelineStorage`` exactly like ``Workbench`` does.  All
overrides are applied through the existing ``walk_override`` single-writer
methods (``upsert_walk_override`` / ``delete_walk_override``); transaction
contention raises ``ConcurrentTransactionError`` mapped to 503 upstream.
"""

from __future__ import annotations

import json
import time
from typing import Any
from uuid import uuid4

from app.pipeline.adapter import PipelineStorage
from app.pipeline.walks.order import WALK_TASK_NAMES
from app.pipeline.workbench import (
    BookNotFoundError,
    StaleRevisionError,
    ValidationError,
)
from app.utils import load_app_config, resolve_task_config


# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------

ALLOWED_OVERRIDE_KEYS = frozenset(
    ("model_name", "reasoning_effort", "temperature", "prompt")
)
_REASONING_EFFORTS = frozenset(("low", "medium", "high"))
_TEMPERATURE_MIN = 0.0
_TEMPERATURE_MAX = 2.0
_MAX_PROMPT_LEN = 20000
_MODEL_MAX_LEN = 200
_DEFAULT_AUTHOR = "local"

# Canonical task names (the values of WALK_TASK_NAMES).
TASK_NAMES: tuple[str, ...] = tuple(WALK_TASK_NAMES.values())


def _now_ms() -> int:
    return int(time.time() * 1000)


def _is_truthy_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


class PromptConfigDomain:
    """Storage/domain facade for the prompt/settings config contract.

    Parameters
    ----------
    storage:
        An active ``PipelineStorage`` implementation (``SQLiteAdapter`` or
        ``InMemorySQLiteAdapter``).
    """

    def __init__(self, storage: PipelineStorage) -> None:
        self._storage = storage

    # ------------------------------------------------------------------
    # Book scope / reads
    # ------------------------------------------------------------------

    def require_book(self, book_id: str) -> None:
        """Raise :class:`BookNotFoundError` (HTTP 404) for an unknown book."""
        if not self._storage.execute_query(
            "SELECT id FROM book WHERE id = ?", (book_id,)
        ):
            raise BookNotFoundError(f"unknown book: {book_id}")

    def _source_for(self, field: str, task: str, book_id: str) -> str:
        """Return the resolving tier for *field*: row/config/task/global/fallback."""
        row = None
        try:
            rows = self._storage.execute_query(
                "SELECT key, value_json FROM walk_override"
                " WHERE book_id = ? AND walk_name = ? AND key = ?",
                (book_id, task, field),
            )
            row = rows[0] if rows else None
        except Exception:
            row = None
        if row is not None:
            try:
                value = json.loads(row["value_json"])
            except (json.JSONDecodeError, TypeError):
                value = None
            if field == "temperature":
                if value is not None:
                    return "row"
            elif field == "prompt":
                if isinstance(value, str) and value:
                    return "row"
            elif value:
                return "row"

        config = load_app_config() or {}
        llm = config.get("llm", {})
        task_overrides = llm.get("task_overrides", {})
        task_override = (
            task_overrides.get(task, {}) if isinstance(task_overrides, dict) else {}
        )
        if field == "prompt":
            config_walk = config.get("walk_override", {})
            config_task = (
                config_walk.get(task, {}) if isinstance(config_walk, dict) else {}
            )
            if isinstance(config_task.get("prompt"), str) and config_task["prompt"]:
                return "config"
            if isinstance(task_override.get("prompt"), str) and task_override["prompt"]:
                return "task"
            if isinstance(llm.get("prompt"), str) and llm["prompt"]:
                return "global"
            return "fallback"
        if field == "temperature":
            if task_override.get("temperature") is not None:
                return "task"
            if llm.get("temperature") is not None:
                return "global"
            return "fallback"
        # model_name / reasoning_effort use truthiness.
        if task_override.get(field):
            return "task"
        if llm.get(field):
            return "global"
        return "fallback"

    def effective_config(self, book_id: str) -> dict:
        """Return effective values + sources for every task in *book_id*."""
        self.require_book(book_id)
        tasks: dict[str, dict] = {}
        for task in TASK_NAMES:
            values = resolve_task_config(task, self._storage, book_id)
            sources = {
                field: self._source_for(field, task, book_id)
                for field in ("model_name", "reasoning_effort", "temperature", "prompt")
            }
            tasks[task] = {"values": values, "sources": sources}
        return {"book_id": book_id, "tasks": tasks}

    # ------------------------------------------------------------------
    # Validation (side-effect free)
    # ------------------------------------------------------------------

    def _validate_settings(self, settings: Any) -> list[str]:
        errors: list[str] = []
        if settings is None:
            settings = {}
        if not isinstance(settings, dict):
            return ["settings must be an object"]
        unknown = set(settings) - ALLOWED_OVERRIDE_KEYS
        if unknown:
            errors.append(f"unknown override key(s): {sorted(unknown)}")
        for key, value in settings.items():
            if key == "model_name":
                if not _is_truthy_str(value) or len(value) > _MODEL_MAX_LEN:
                    errors.append("model_name must be a non-empty string")
            elif key == "reasoning_effort":
                if value not in _REASONING_EFFORTS:
                    errors.append(
                        f"reasoning_effort must be one of {sorted(_REASONING_EFFORTS)}"
                    )
            elif key == "temperature":
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not (_TEMPERATURE_MIN <= value <= _TEMPERATURE_MAX)
                ):
                    errors.append(
                        f"temperature must be a number in"
                        f" [{_TEMPERATURE_MIN}, {_TEMPERATURE_MAX}]"
                    )
            elif key == "prompt":
                if not _is_truthy_str(value) or len(value) > _MAX_PROMPT_LEN:
                    errors.append("prompt must be a non-empty string")
        return errors

    def _validate_raw_json(self, raw_json: Any) -> tuple[list[str], dict | None]:
        """Validate raw JSON safely.  Returns ``(errors, parsed)``."""
        errors: list[str] = []
        if raw_json is None:
            return errors, None
        if not isinstance(raw_json, str):
            return ["raw_json must be a JSON string"], None
        try:
            parsed = json.loads(raw_json)
        except (json.JSONDecodeError, TypeError) as exc:
            return [f"raw_json is not valid JSON: {exc}"], None
        if not isinstance(parsed, dict):
            return ["raw_json must decode to a JSON object"], None
        errors.extend(self._validate_settings(parsed))
        return errors, parsed

    def validate(self, write: dict) -> dict:
        """Side-effect-free validation returning ``{valid, errors, task}``."""
        errors: list[str] = []
        task = write.get("task")
        if task not in TASK_NAMES:
            errors.append(
                f"unknown task: {task}; must be one of {sorted(TASK_NAMES)}"
            )
        errors.extend(self._validate_settings(write.get("settings")))
        raw_errors, _ = self._validate_raw_json(write.get("raw_json"))
        errors.extend(raw_errors)
        prompt = write.get("prompt")
        if prompt is not None:
            if not _is_truthy_str(prompt) or len(prompt) > _MAX_PROMPT_LEN:
                errors.append("prompt must be a non-empty string")
        return {"valid": not errors, "errors": errors, "task": task}

    # ------------------------------------------------------------------
    # Revision persistence
    # ------------------------------------------------------------------

    def _current_head(self, book_id: str, task: str) -> dict | None:
        rows = self._storage.execute_query(
            "SELECT revision_id FROM prompt_config_revision"
            " WHERE book_id = ? AND task = ? AND superseded_by IS NULL"
            " ORDER BY created_ms DESC, revision_id DESC LIMIT 1",
            (book_id, task),
        )
        return rows[0] if rows else None

    def save(
        self,
        book_id: str,
        *,
        write: dict,
        base_revision: str | None,
        author_id: str = _DEFAULT_AUTHOR,
    ) -> dict:
        """Append a prompt-config revision, applying allowed overrides.

        * Validates the write deterministically (:class:`ValidationError`).
        * Rejects an unknown book (:class:`BookNotFoundError`).
        * Rejects a stale or cross-book ``base_revision``
          (:class:`StaleRevisionError`, HTTP 409).
        * Applies only allowed overrides through the ``walk_override``
          single-writer methods inside one transaction.
        """
        self.require_book(book_id)
        result = self.validate(write)
        if not result["valid"]:
            raise ValidationError("; ".join(result["errors"]))
        task = write["task"]

        raw_errors, raw_parsed = self._validate_raw_json(write.get("raw_json"))
        if raw_errors:
            raise ValidationError("; ".join(raw_errors))

        # Merge settings with validated raw_json (structured settings win).
        settings: dict[str, Any] = {}
        if raw_parsed:
            settings.update(
                {k: v for k, v in raw_parsed.items() if k in ALLOWED_OVERRIDE_KEYS}
            )
        provided_settings = write.get("settings") or {}
        if isinstance(provided_settings, dict):
            settings.update(
                {k: v for k, v in provided_settings.items() if k in ALLOWED_OVERRIDE_KEYS}
            )
        # prompt is a top-level field on the write.
        prompt = write.get("prompt")

        head = self._current_head(book_id, task)
        if head is None:
            if base_revision is not None:
                raise StaleRevisionError(
                    f"no existing revision for task '{task}' in book '{book_id}';"
                    f" base_revision must be None"
                )
        elif head["revision_id"] != base_revision:
            raise StaleRevisionError(
                f"stale base_revision {base_revision!r} for task '{task}' in"
                f" book '{book_id}'; current head {head['revision_id']!r}"
            )

        revision_id = "prompt-" + uuid4().hex
        now = _now_ms()

        with self._storage.transaction():
            # Apply overrides through the existing single writer.
            for key in ALLOWED_OVERRIDE_KEYS:
                if key == "prompt":
                    if prompt is None:
                        # Ensure no stale prompt override remains (falls through).
                        try:
                            self._storage.delete_walk_override(
                                book_id, task, "prompt"
                            )
                        except Exception:
                            pass
                    else:
                        self._storage.upsert_walk_override(
                            book_id, task, "prompt", json.dumps(prompt)
                        )
                    continue
                if key in settings:
                    self._storage.upsert_walk_override(
                        book_id, task, key, json.dumps(settings[key])
                    )

            # Recompute the effective prompt after applying overrides.
            effective_prompt = resolve_task_config(task, self._storage, book_id)["prompt"]
            source_layers = {
                field: self._source_for(field, task, book_id)
                for field in ("model_name", "reasoning_effort", "temperature", "prompt")
            }
            validation = {"valid": True, "errors": []}
            self._storage.insert_prompt_config_revision(
                {
                    "revision_id": revision_id,
                    "book_id": book_id,
                    "task": task,
                    "base_revision": base_revision,
                    "source_layers_json": json.dumps(source_layers),
                    "effective_prompt": effective_prompt,
                    "settings_json": json.dumps(settings),
                    "raw_json": write.get("raw_json"),
                    "validation_json": json.dumps(validation),
                    "author_id": author_id,
                    "created_ms": now,
                    "superseded_by": None,
                }
            )
            if head is not None:
                self._storage.supersede_prompt_config_revision(
                    head["revision_id"], revision_id
                )

        return self._decode(self._storage.get_prompt_config_revision(revision_id))

    def _decode(self, row: dict | None) -> dict:
        if row is None:
            return {}
        return {
            "revision_id": row["revision_id"],
            "book_id": row["book_id"],
            "task": row["task"],
            "base_revision": row["base_revision"],
            "source_layers": json.loads(row["source_layers_json"] or "{}"),
            "effective_prompt": row["effective_prompt"],
            "settings": json.loads(row["settings_json"] or "{}"),
            "raw_json": row["raw_json"],
            "validation": json.loads(row["validation_json"] or "{}"),
            "author_id": row["author_id"],
            "created_ms": row["created_ms"],
            "superseded_by": row["superseded_by"],
        }

    def list_revisions(self, book_id: str, task: str) -> list[dict]:
        """Return decoded revisions for ``(book_id, task)``, newest first."""
        self.require_book(book_id)
        rows = self._storage.list_prompt_config_revisions(book_id, task)
        return [self._decode(row) for row in rows]
