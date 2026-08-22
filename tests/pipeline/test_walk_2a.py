"""Tests for Walk 2a scene segmentation."""

import json
from unittest.mock import Mock

import pytest

from app.pipeline.adapter import InMemorySQLiteAdapter
from app.pipeline.populate import populate_initial_spine
from app.pipeline.walks.walk_2a_scene_segmentation import (
    _build_scene_segmentation_prompt,
    _parse_llm_response,
    _validate_scene_partition,
    execute,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def storage():
    """Create an in-memory SQLite adapter with schema initialized."""
    adapter = InMemorySQLiteAdapter()
    adapter.init_db()
    return adapter


@pytest.fixture
def sample_chapters():
    """Sample chapter data with paragraphs and spans."""
    return [
        {
            "id": "chapter-1",
            "paragraphs": [
                {
                    "id": "para-1",
                    "spans": [
                        {"id": "span-1a", "span_type": "sentence", "text": "The sun rose over the mountains."},
                        {"id": "span-1b", "span_type": "sentence", "text": "Birds began to sing."},
                    ],
                },
                {
                    "id": "para-2",
                    "spans": [
                        {"id": "span-2a", "span_type": "sentence", "text": "John woke up early."},
                    ],
                },
                {
                    "id": "para-3",
                    "spans": [
                        {"id": "span-3a", "span_type": "sentence", "text": "He prepared breakfast."},
                    ],
                },
            ],
        },
        {
            "id": "chapter-2",
            "paragraphs": [
                {
                    "id": "para-4",
                    "spans": [
                        {"id": "span-4a", "span_type": "sentence", "text": "Later that day, the scene shifted to the city."},
                    ],
                },
                {
                    "id": "para-5",
                    "spans": [
                        {"id": "span-5a", "span_type": "sentence", "text": "Mary was waiting at the cafe."},
                    ],
                },
            ],
        },
    ]


@pytest.fixture
def populated_storage(storage, sample_chapters):
    """Storage with populated spine."""
    populate_initial_spine("series-1", "book-1", sample_chapters, storage)
    return storage


@pytest.fixture
def mock_llm_client():
    """Mock OpenAI client."""
    client = Mock()
    return client


# ---------------------------------------------------------------------------
# Tests: execute()
# ---------------------------------------------------------------------------


class TestExecute:
    """Test the main execute() function."""

    def test_execute_returns_summary_dict(self, populated_storage, mock_llm_client, monkeypatch):
        """execute() returns a summary dict with expected keys."""
        # Mock LLM response: single scene with all paragraphs
        mock_response = Mock()
        mock_response.choices = [
            Mock(
                message=Mock(
                    content=json.dumps([
                        {"paragraph_ids": ["P1", "P2", "P3"], "confidence": 0.9}
                    ])
                )
            )
        ]
        mock_llm_client.chat.completions.create.return_value = mock_response

        # Mock create_llm_client to return our mock
        def mock_create_llm_client(config_path=None):
            return mock_llm_client, "test-model"

        monkeypatch.setattr(
            "app.utils.create_llm_client",
            mock_create_llm_client,
        )

        # Mock resolve_task_config
        def mock_resolve_task_config(task, storage, book_id):
            return {"model_name": "test-model", "reasoning_effort": None, "temperature": 0.1}

        monkeypatch.setattr(
            "app.utils.resolve_task_config",
            mock_resolve_task_config,
        )

        result = execute("book-1", populated_storage, {})

        assert "book_id" in result
        assert "chapters_processed" in result
        assert "scenes_created" in result
        assert "scenes_rejected" in result
        assert "scenes_for_review" in result
        assert "errors" in result

    def test_execute_processes_all_chapters(self, populated_storage, mock_llm_client, monkeypatch):
        """execute() processes all chapters in the book."""
        # Mock LLM response for each chapter
        mock_response = Mock()
        mock_response.choices = [
            Mock(
                message=Mock(
                    content=json.dumps([
                        {"paragraph_ids": ["P1", "P2", "P3"], "confidence": 0.9}
                    ])
                )
            )
        ]
        mock_llm_client.chat.completions.create.return_value = mock_response

        def mock_create_llm_client(config_path=None):
            return mock_llm_client, "test-model"

        monkeypatch.setattr(
            "app.utils.create_llm_client",
            mock_create_llm_client,
        )

        def mock_resolve_task_config(task, storage, book_id):
            return {"model_name": "test-model", "reasoning_effort": None, "temperature": 0.1}

        monkeypatch.setattr(
            "app.utils.resolve_task_config",
            mock_resolve_task_config,
        )

        result = execute("book-1", populated_storage, {})

        # Should process 2 chapters
        assert result["chapters_processed"] == 2

    def test_execute_creates_scenes_with_high_confidence(self, populated_storage, mock_llm_client, monkeypatch):
        """execute() creates scenes when confidence >= 0.7."""
        # Mock LLM response: two scenes, both high confidence
        mock_response = Mock()
        mock_response.choices = [
            Mock(
                message=Mock(
                    content=json.dumps([
                        {"paragraph_ids": ["P1", "P2"], "confidence": 0.9},
                        {"paragraph_ids": ["P3"], "confidence": 0.8},
                    ])
                )
            )
        ]
        mock_llm_client.chat.completions.create.return_value = mock_response

        def mock_create_llm_client(config_path=None):
            return mock_llm_client, "test-model"

        monkeypatch.setattr(
            "app.utils.create_llm_client",
            mock_create_llm_client,
        )

        def mock_resolve_task_config(task, storage, book_id):
            return {"model_name": "test-model", "reasoning_effort": None, "temperature": 0.1}

        monkeypatch.setattr(
            "app.utils.resolve_task_config",
            mock_resolve_task_config,
        )

        result = execute("book-1", populated_storage, {})

        # Chapter 1 should have 2 scenes created
        assert result["scenes_created"] >= 2

    def test_execute_rejects_scenes_with_low_confidence(self, populated_storage, mock_llm_client, monkeypatch):
        """execute() rejects scenes when confidence < 0.5."""
        # Mock LLM response: one scene with low confidence
        mock_response = Mock()
        mock_response.choices = [
            Mock(
                message=Mock(
                    content=json.dumps([
                        {"paragraph_ids": ["P1", "P2", "P3"], "confidence": 0.3}
                    ])
                )
            )
        ]
        mock_llm_client.chat.completions.create.return_value = mock_response

        def mock_create_llm_client(config_path=None):
            return mock_llm_client, "test-model"

        monkeypatch.setattr(
            "app.utils.create_llm_client",
            mock_create_llm_client,
        )

        def mock_resolve_task_config(task, storage, book_id):
            return {"model_name": "test-model", "reasoning_effort": None, "temperature": 0.1}

        monkeypatch.setattr(
            "app.utils.resolve_task_config",
            mock_resolve_task_config,
        )

        result = execute("book-1", populated_storage, {})

        # Scene should be rejected
        assert result["scenes_rejected"] >= 1

    def test_execute_flags_scenes_for_review_with_medium_confidence(self, populated_storage, mock_llm_client, monkeypatch):
        """execute() flags scenes for review when 0.5 <= confidence < 0.7."""
        # Mock LLM response: one scene with medium confidence
        mock_response = Mock()
        mock_response.choices = [
            Mock(
                message=Mock(
                    content=json.dumps([
                        {"paragraph_ids": ["P1", "P2", "P3"], "confidence": 0.6}
                    ])
                )
            )
        ]
        mock_llm_client.chat.completions.create.return_value = mock_response

        def mock_create_llm_client(config_path=None):
            return mock_llm_client, "test-model"

        monkeypatch.setattr(
            "app.utils.create_llm_client",
            mock_create_llm_client,
        )

        def mock_resolve_task_config(task, storage, book_id):
            return {"model_name": "test-model", "reasoning_effort": None, "temperature": 0.1}

        monkeypatch.setattr(
            "app.utils.resolve_task_config",
            mock_resolve_task_config,
        )

        result = execute("book-1", populated_storage, {})

        # Scene should be flagged for review
        assert result["scenes_for_review"] >= 1


# ---------------------------------------------------------------------------
# Tests: _build_scene_segmentation_prompt()
# ---------------------------------------------------------------------------


    def test_walk_override_drives_llm_config(self, populated_storage, monkeypatch, tmp_path):
        """A walk_override row for (book, task) overrides the walk's LLM config.

        Phase 3 (Plan G): the walk resolves its LLM config via
        ``resolve_task_config(task, storage, book_id)`` at unit start, so a
        walk_override row for this book + task must beat the on-disk
        fallbacks and flow into the LLM call (temperature + model).
        """
        # Point config resolution at a tmp path with no config file -> fallbacks.
        monkeypatch.setenv("ALEXANDRIA_CONFIG_PATH", str(tmp_path / "config.json"))

        # walk_override rows override temperature AND model for this book+task.
        populated_storage.execute_insert(
            "INSERT INTO walk_override (book_id, walk_name, key, value_json)"
            " VALUES (?, ?, ?, ?)",
            ("book-1", "scene_segmentation", "temperature", json.dumps(0.9)),
        )
        populated_storage.execute_insert(
            "INSERT INTO walk_override (book_id, walk_name, key, value_json)"
            " VALUES (?, ?, ?, ?)",
            ("book-1", "scene_segmentation", "model_name", json.dumps("gpt-4o-mini")),
        )

        monkeypatch.setattr(
            "app.utils.create_llm_client", lambda config_path=None: (object(), None)
        )

        captured = {}

        def mock_call_llm(
            client, model_name, temperature, reasoning_effort, system_prompt, user_prompt
        ):
            captured["temperature"] = temperature
            captured["model_name"] = model_name
            return "[]"

        monkeypatch.setattr(
            "app.pipeline.walks.walk_2a_scene_segmentation.chat_completion",
            mock_call_llm,
        )

        execute("book-1", populated_storage, {})

        # Override wins over the 0.6 fallback temperature and default model.
        assert captured["temperature"] == 0.9
        assert captured["model_name"] == "gpt-4o-mini"

    def test_prompt_override_drives_system_prompt(
        self, populated_storage, monkeypatch, tmp_path
    ):
        """A walk_override row key='prompt' flows into the LLM system_prompt.

        Phase 3 Amendment (Plan G): ``resolve_task_config`` returns an
        effective ``prompt``; the walk must pass it as ``system_prompt``.
        With no override at any tier the built-in system prompt is used;
        with a walk_override prompt row the row wins.
        """
        # No config file at the pinned path -> no prompt override at any tier.
        monkeypatch.setenv("ALEXANDRIA_CONFIG_PATH", str(tmp_path / "config.json"))

        monkeypatch.setattr(
            "app.utils.create_llm_client", lambda config_path=None: (object(), None)
        )

        captured = {}

        def mock_call_llm(
            client, model_name, temperature, reasoning_effort, system_prompt, user_prompt
        ):
            captured["system_prompt"] = system_prompt
            return "[]"

        monkeypatch.setattr(
            "app.pipeline.walks.walk_2a_scene_segmentation.chat_completion",
            mock_call_llm,
        )

        execute("book-1", populated_storage, {})

        # Unset at every tier -> the walk's built-in system prompt.
        assert captured["system_prompt"] == (
            "You are a literary analyst specializing in narrative structure."
        )

        # walk_override row key="prompt" wins over the built-in.
        populated_storage.execute_insert(
            "INSERT INTO walk_override (book_id, walk_name, key, value_json)"
            " VALUES (?, ?, ?, ?)",
            (
                "book-1",
                "scene_segmentation",
                "prompt",
                json.dumps("You are a TEST override prompt."),
            ),
        )

        execute("book-1", populated_storage, {})

        assert captured["system_prompt"] == "You are a TEST override prompt."

    def test_prompt_override_config_section_drives_system_prompt(
        self, populated_storage, monkeypatch, tmp_path
    ):
        """Top-level config walk_override[task].prompt flows into system_prompt."""
        (tmp_path / "config.json").write_text(
            json.dumps(
                {
                    "walk_override": {
                        "scene_segmentation": {
                            "prompt": "You are a TEST config prompt."
                        }
                    }
                }
            )
        )
        monkeypatch.setenv("ALEXANDRIA_CONFIG_PATH", str(tmp_path / "config.json"))

        monkeypatch.setattr(
            "app.utils.create_llm_client", lambda config_path=None: (object(), None)
        )

        captured = {}

        def mock_call_llm(
            client, model_name, temperature, reasoning_effort, system_prompt, user_prompt
        ):
            captured["system_prompt"] = system_prompt
            return "[]"

        monkeypatch.setattr(
            "app.pipeline.walks.walk_2a_scene_segmentation.chat_completion",
            mock_call_llm,
        )

        execute("book-1", populated_storage, {})

        assert captured["system_prompt"] == "You are a TEST config prompt."


class TestBuildPrompt:
    """Test prompt building."""

    def test_prompt_includes_paragraph_text(self):
        """Prompt includes paragraph text."""
        paragraphs = [
            {"paragraph_id": "para-1", "text": "First paragraph."},
            {"paragraph_id": "para-2", "text": "Second paragraph."},
        ]

        prompt = _build_scene_segmentation_prompt(paragraphs)

        assert "First paragraph." in prompt
        assert "Second paragraph." in prompt

    def test_prompt_includes_paragraph_indices(self):
        """Prompt includes paragraph indices (P1, P2, etc.)."""
        paragraphs = [
            {"paragraph_id": "para-1", "text": "First paragraph."},
            {"paragraph_id": "para-2", "text": "Second paragraph."},
        ]

        prompt = _build_scene_segmentation_prompt(paragraphs)

        assert "[P1]" in prompt
        assert "[P2]" in prompt


# ---------------------------------------------------------------------------
# Tests: _parse_llm_response()
# ---------------------------------------------------------------------------


class TestParseResponse:
    """Test LLM response parsing."""

    def test_parse_valid_json(self):
        """Parse valid JSON response."""
        paragraphs = [
            {"paragraph_id": "para-1", "text": "First."},
            {"paragraph_id": "para-2", "text": "Second."},
        ]

        response = json.dumps([
            {"paragraph_ids": ["P1", "P2"], "confidence": 0.9}
        ])

        scenes = _parse_llm_response(response, paragraphs)

        assert len(scenes) == 1
        assert scenes[0]["paragraph_ids"] == ["para-1", "para-2"]
        assert scenes[0]["confidence"] == 0.9

    def test_parse_json_with_extra_text(self):
        """Parse JSON response with extra text."""
        paragraphs = [
            {"paragraph_id": "para-1", "text": "First."},
        ]

        response = 'Here is the JSON:\n[{"paragraph_ids": ["P1"], "confidence": 0.8}]\nDone.'

        scenes = _parse_llm_response(response, paragraphs)

        assert len(scenes) == 1
        assert scenes[0]["paragraph_ids"] == ["para-1"]

    def test_parse_invalid_json_returns_empty(self):
        """Parse invalid JSON returns empty list."""
        paragraphs = [
            {"paragraph_id": "para-1", "text": "First."},
        ]

        response = "This is not JSON"

        scenes = _parse_llm_response(response, paragraphs)

        assert scenes == []

    def test_parse_maps_indices_to_ids(self):
        """Parse maps paragraph indices to actual IDs."""
        paragraphs = [
            {"paragraph_id": "uuid-1", "text": "First."},
            {"paragraph_id": "uuid-2", "text": "Second."},
            {"paragraph_id": "uuid-3", "text": "Third."},
        ]

        response = json.dumps([
            {"paragraph_ids": ["P1", "P3"], "confidence": 0.9}
        ])

        scenes = _parse_llm_response(response, paragraphs)

        assert scenes[0]["paragraph_ids"] == ["uuid-1", "uuid-3"]

    def test_parse_ignores_unknown_indices(self):
        """Parse ignores unknown paragraph indices."""
        paragraphs = [
            {"paragraph_id": "para-1", "text": "First."},
        ]

        response = json.dumps([
            {"paragraph_ids": ["P1", "P99"], "confidence": 0.9}
        ])

        scenes = _parse_llm_response(response, paragraphs)

        # Should only include P1, not P99
        assert scenes[0]["paragraph_ids"] == ["para-1"]


class TestValidateScenePartition:
    """Test whole-response scene partition validation."""

    @pytest.fixture
    def paragraphs(self):
        return [{"paragraph_id": f"para-{i}"} for i in range(1, 5)]

    @pytest.mark.parametrize(
        "scenes",
        [
            [
                {"paragraph_ids": ["para-1", "para-2"], "confidence": 0.9},
                {"paragraph_ids": ["para-2", "para-3"], "confidence": 0.9},
            ],
            [{"paragraph_ids": ["para-1", "para-1"], "confidence": 0.9}],
            [
                {"paragraph_ids": ["para-3"], "confidence": 0.9},
                {"paragraph_ids": ["para-1", "para-2"], "confidence": 0.9},
            ],
            [{"paragraph_ids": ["para-1", "para-3"], "confidence": 0.9}],
        ],
        ids=["overlap", "duplicate", "out_of_order", "noncontiguous"],
    )
    def test_rejects_malformed_partition(self, paragraphs, scenes):
        assert not _validate_scene_partition(scenes, paragraphs)

    def test_retains_omitted_low_confidence_paragraphs(self, paragraphs):
        scenes = [
            {"paragraph_ids": ["para-1"], "confidence": 0.9},
            {"paragraph_ids": ["para-2"], "confidence": 0.3},
            {"paragraph_ids": ["para-3", "para-4"], "confidence": 0.9},
        ]

        assert _validate_scene_partition(scenes, paragraphs)
