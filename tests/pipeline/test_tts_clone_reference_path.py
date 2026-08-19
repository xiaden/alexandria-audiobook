"""Tests for resolving pipeline clone-reference audio paths."""

from pathlib import Path

import numpy as np

from app import tts


def test_resolves_uploaded_reference_from_canonical_root(tmp_path, monkeypatch):
    reference_dir = tmp_path / "designed_voices" / "references"
    reference_dir.mkdir(parents=True)
    reference = reference_dir / "uploaded.wav"
    reference.touch()
    monkeypatch.setenv("CLONE_REFERENCE_ROOT", str(reference_dir))

    assert tts._resolve_clone_reference_path("uploaded.wav") == str(reference)


def test_falls_back_to_repository_relative_reference(tmp_path, monkeypatch):
    reference_dir = tmp_path / "references"
    reference_dir.mkdir()
    project_reference = tmp_path / "refs" / "legacy.wav"
    project_reference.parent.mkdir()
    project_reference.touch()
    monkeypatch.setattr(tts.os.path, "abspath", lambda _: str(tmp_path / "app" / "tts.py"))
    monkeypatch.setenv("CLONE_REFERENCE_ROOT", str(reference_dir))

    assert tts._resolve_clone_reference_path("refs/legacy.wav") == str(project_reference)


def test_preserves_absolute_reference_path(tmp_path):
    reference = Path(tmp_path) / "absolute.wav"

    assert tts._resolve_clone_reference_path(str(reference)) == str(reference)


def test_local_clone_prompt_uses_canonical_reference_path(tmp_path, monkeypatch):
    reference_dir = tmp_path / "references"
    reference_dir.mkdir()
    reference = reference_dir / "uploaded.wav"
    reference.touch()
    monkeypatch.setenv("CLONE_REFERENCE_ROOT", str(reference_dir))

    class Model:
        def create_voice_clone_prompt(self, **kwargs):
            return kwargs["ref_audio"][1]

    engine = tts.TTSEngine({"tts": {"mode": "local"}})
    monkeypatch.setattr(engine, "_init_local_clone", lambda: Model())
    monkeypatch.setattr(tts.sf, "read", lambda path: (np.zeros(1), 16000))

    assert engine._get_clone_prompt(
        "speaker", {"speaker": {"ref_audio": "uploaded.wav", "ref_text": "text"}}
    ) == 16000


def test_external_clone_uses_canonical_reference_path(tmp_path, monkeypatch):
    reference_dir = tmp_path / "references"
    reference_dir.mkdir()
    reference = reference_dir / "uploaded.wav"
    reference.touch()
    generated = tmp_path / "generated.wav"
    generated.write_bytes(b"audio")
    monkeypatch.setenv("CLONE_REFERENCE_ROOT", str(reference_dir))
    captured = []

    class Client:
        def predict(self, audio, *args, **kwargs):
            captured.append(audio)
            return [str(generated)]

    engine = tts.TTSEngine({"tts": {"mode": "external"}})
    monkeypatch.setattr(engine, "_init_external", lambda: Client())
    monkeypatch.setattr("gradio_client.handle_file", lambda path: path)
    output = tmp_path / "output.wav"
    assert engine._external_generate_clone(
        "text", "speaker", {"speaker": {"ref_audio": "uploaded.wav", "ref_text": "reference"}}, str(output)
    ) is True
    assert captured == [str(reference)]
