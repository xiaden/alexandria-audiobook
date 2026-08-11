"""Byte-stable config round-trip tests for GET/POST /api/config (Plan G Phase 1).

Contract (CONTRACTS.md rule #11 / decision #6 / DD cannot-restore #4 & #13):

- ``POST /api/config`` is a raw-JSON merge: unknown top-level keys
  (``generation``, ``prompts``, ...) survive byte-stable instead of being wiped
  by ``AppConfig.model_dump()`` (the L4 data-loss bug). Known keys are validated
  through ``AppConfig`` (``extra='ignore'``, validation output never serialized).
  ``schema_version`` is stamped and the merged config is written atomically
  (tmp+rename) so a failed save never leaves a partial file.
- ``GET /api/config`` must NOT drop unknown keys present in the on-disk file
  (the old ``model_validate -> model_dump`` path stripped them) while still
  materialising pydantic defaults for known keys (the H1 contract: the frontend
  depends on ``task_overrides`` / ``reasoning_effort`` / pause fields being
  present in the response even when absent from the on-disk config).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_DIR = _REPO_ROOT / "app"

# pytest already puts the repo root on sys.path (tests/ and tests/pipeline/ are
# packages), but be explicit so this file also runs standalone.
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _load_app_local_module(name: str) -> None:
    """Load an app/*.py module under its bare name (e.g. ``utils`` -> app/utils.py).

    app/app.py imports these at module level; see test_legacy_removed.py for why
    the app dir cannot simply be added to sys.path.
    """
    spec = importlib.util.spec_from_file_location(name, _APP_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None, f"cannot locate app/{name}.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)


for _name in ("utils", "hf_utils"):
    if _name not in sys.modules:
        _load_app_local_module(_name)

import app.app  # noqa: E402  (after harness setup)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Real app TestClient with CONFIG_PATH redirected to a tmp file.

    CONFIG_PATH is read at app.py import time, so the endpoint module global is
    patched per-test (both get_config and save_config reference it).
    """
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(app.app, "CONFIG_PATH", str(config_path))
    return TestClient(app.app.app)


# -- Shared payloads ----------------------------------------------------------

_KNOWN_OK = {
    "llm": {
        "base_url": "http://localhost:11434/v1",
        "api_key": "local",
        "model_name": "test-model",
        "reasoning_effort": "high",
        "temperature": 0.7,
        "task_overrides": {"voice_audition": {"model_name": "override-model", "temperature": 0.1}},
    },
    "tts": {"mode": "local", "url": "http://127.0.0.1:7860", "device": "auto"},
}

_UNKNOWN = {
    "generation": {
        "max_chapters": 3,
        "seed": 42,
        "nested": {"enabled": True, "rate": 1.5},
    },
    "prompts": {"script": "You are a narrator.", "review": ["a", "b"]},
}


# -- P1-S1: unknown keys round-trip byte-stable -------------------------------


def test_unknown_keys_round_trip_byte_stable(client, tmp_path):
    """Unknown top-level keys POSTed are returned by GET with identical values."""
    body = {**_KNOWN_OK, **_UNKNOWN}
    r = client.post("/api/config", json=body)
    assert r.status_code == 200, r.text

    r = client.get("/api/config")
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["generation"] == _UNKNOWN["generation"]
    assert data["prompts"] == _UNKNOWN["prompts"]

    # Known keys are still validated + persisted (and materialised in GET).
    assert data["llm"]["model_name"] == "test-model"
    assert data["llm"]["task_overrides"]["voice_audition"]["model_name"] == "override-model"

    # On-disk file carries the same unknown sections.
    on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert on_disk["generation"] == _UNKNOWN["generation"]
    assert on_disk["prompts"] == _UNKNOWN["prompts"]


def test_unknown_keys_survive_later_known_only_save(client):
    """A later save touching only known keys must not wipe on-disk unknowns."""
    body = {**_KNOWN_OK, **_UNKNOWN}
    assert client.post("/api/config", json=body).status_code == 200

    # Second save: only a known-key tweak (what the Setup tab would send).
    tweak = {"llm": {**_KNOWN_OK["llm"], "temperature": 0.9}, "tts": _KNOWN_OK["tts"]}
    r = client.post("/api/config", json=tweak)
    assert r.status_code == 200, r.text

    data = client.get("/api/config").json()
    assert data["generation"] == _UNKNOWN["generation"]
    assert data["prompts"] == _UNKNOWN["prompts"]
    assert data["llm"]["temperature"] == 0.9


def test_unknown_keys_already_on_disk_survive_omitting_save(client, tmp_path):
    """Unknown keys already on disk survive a save whose body omits them (merge onto disk)."""
    (tmp_path / "config.json").write_text(
        json.dumps({**_KNOWN_OK, "generation": {"max_chapters": 3}}), encoding="utf-8"
    )

    body = {"llm": {**_KNOWN_OK["llm"], "temperature": 0.5}, "tts": _KNOWN_OK["tts"]}
    r = client.post("/api/config", json=body)
    assert r.status_code == 200, r.text

    data = client.get("/api/config").json()
    assert data["generation"] == {"max_chapters": 3}
    assert data["llm"]["temperature"] == 0.5


def test_get_preserves_unknown_keys_and_materialises_known_defaults(client, tmp_path):
    """GET keeps unknown keys from disk while materialising pydantic defaults (H1 contract)."""
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "llm": {"base_url": "http://localhost:11434/v1", "api_key": "local", "model_name": "m"},
                "tts": {"mode": "local"},
                "generation": {"max_chapters": 3},
            }
        ),
        encoding="utf-8",
    )

    r = client.get("/api/config")
    assert r.status_code == 200, r.text
    data = r.json()

    # H1 contract: known-key defaults materialised even when absent on disk.
    assert "task_overrides" in data["llm"]
    assert data["llm"]["reasoning_effort"] is None
    assert data["llm"]["temperature"] == 0.6
    assert data["tts"]["pause_between_speakers_ms"] == 500
    assert data["tts"]["pause_same_speaker_ms"] == 250

    # Byte-stable contract: unknown key preserved in the response.
    assert data["generation"] == {"max_chapters": 3}


# -- P1-S1: known keys still validated through AppConfig -----------------------


def test_known_keys_still_validated(client):
    """An invalid known value is rejected with a validation error (422)."""
    bad = {**_KNOWN_OK, "tts": {**_KNOWN_OK["tts"], "parallel_workers": "not-an-int"}}
    r = client.post("/api/config", json=bad)
    assert r.status_code == 422, r.text

    # Missing required known section is also a validation error.
    missing = {**_UNKNOWN}
    r = client.post("/api/config", json=missing)
    assert r.status_code == 422, r.text


# -- P2-S1: pause defaults round-trip byte-stable + validation bounds -----------


def test_pause_defaults_round_trip_byte_stable(client, tmp_path):
    """Custom pause defaults POSTed round-trip via GET and land on disk byte-stable.

    The two global pause defaults are known TTSConfig fields (validated by
    ``validate_pause_ms``), so a save materialises them and a subsequent GET
    returns the persisted values while unknown keys stay untouched.
    """
    body = {
        **_KNOWN_OK,
        "tts": {
            **_KNOWN_OK["tts"],
            "pause_between_speakers_ms": 900,
            "pause_same_speaker_ms": 400,
        },
        **_UNKNOWN,
    }
    r = client.post("/api/config", json=body)
    assert r.status_code == 200, r.text

    data = client.get("/api/config").json()
    assert data["tts"]["pause_between_speakers_ms"] == 900
    assert data["tts"]["pause_same_speaker_ms"] == 400

    on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert on_disk["tts"]["pause_between_speakers_ms"] == 900
    assert on_disk["tts"]["pause_same_speaker_ms"] == 400
    # Unknown sections survive alongside the pause defaults.
    assert on_disk["generation"] == _UNKNOWN["generation"]


def test_pause_zero_config_round_trips():
    """An explicit 0 default (intentional no-gap) is preserved, not coerced."""
    from app.pipeline.tts_integration import validate_pause_ms

    # 0 is a valid pause (intentional no-gap) and survives the validator.
    assert validate_pause_ms(0) == 0


def test_pause_config_out_of_bounds_422(client):
    """A pause default above PAUSE_MAX_MS is rejected with 422 (stable)."""
    from app.pipeline.tts_integration import PAUSE_MAX_MS

    bad = {
        **_KNOWN_OK,
        "tts": {**_KNOWN_OK["tts"], "pause_between_speakers_ms": PAUSE_MAX_MS + 1},
    }
    r = client.post("/api/config", json=bad)
    assert r.status_code == 422, r.text


def test_pause_config_non_int_422(client):
    """Non-integer pause defaults are rejected with 422 (boolean, string)."""
    for bad_value in (True, "abc", 1.5, -1):
        bad = {
            **_KNOWN_OK,
            "tts": {**_KNOWN_OK["tts"], "pause_same_speaker_ms": bad_value},
        }
        r = client.post("/api/config", json=bad)
        assert r.status_code == 422, (bad_value, r.status_code)


def test_pause_config_zero_materialised_in_get(client, tmp_path):
    """An explicit 0 on disk is read back as 0 (not coerced to a default)."""
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "llm": {"base_url": "http://localhost:11434/v1", "api_key": "local", "model_name": "m"},
                "tts": {
                    "mode": "local",
                    "pause_between_speakers_ms": 0,
                    "pause_same_speaker_ms": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    data = client.get("/api/config").json()
    assert data["tts"]["pause_between_speakers_ms"] == 0
    assert data["tts"]["pause_same_speaker_ms"] == 0


# -- P1-S1: schema_version stamp -----------------------------------------------


def test_schema_version_stamped(client, tmp_path):
    """schema_version is stamped into the saved config and surfaces via GET."""
    body = {**_KNOWN_OK, **_UNKNOWN}
    r = client.post("/api/config", json=body)
    assert r.status_code == 200, r.text

    on_disk = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert on_disk["schema_version"] == 1

    data = client.get("/api/config").json()
    assert data["schema_version"] == 1


# -- P1-S1: atomic write (no partial file on failure) --------------------------


def test_failed_save_leaves_file_unchanged(client, tmp_path):
    """Neither a malformed body nor a validation failure may alter the on-disk config."""
    body = {**_KNOWN_OK, **_UNKNOWN}
    assert client.post("/api/config", json=body).status_code == 200

    path = tmp_path / "config.json"
    before = path.read_bytes()

    # Malformed JSON body -> request-level 422, file untouched.
    r = client.post(
        "/api/config", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert r.status_code >= 400
    assert path.read_bytes() == before

    # Valid JSON but failing AppConfig validation -> 422, file untouched.
    bad = {**_KNOWN_OK, "tts": {"mode": 123}}
    r = client.post("/api/config", json=bad)
    assert r.status_code == 422, r.text
    assert path.read_bytes() == before

    # The file is still readable and fully intact.
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["generation"] == _UNKNOWN["generation"]
