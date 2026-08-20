"""Regression tests for safe LoRA dataset archive extraction."""

from __future__ import annotations

import io
import stat
import zipfile
import importlib.util
import sys
from pathlib import Path

from fastapi import HTTPException
import pytest

# Load app-local bare-name modules before importing app.app, matching the
# top-level app test harness used by the rest of this test directory.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_DIR = _REPO_ROOT / "app"
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
for _name in ("utils", "hf_utils"):
    if _name not in sys.modules:
        _spec = importlib.util.spec_from_file_location(_name, _APP_DIR / f"{_name}.py")
        assert _spec is not None and _spec.loader is not None
        _module = importlib.util.module_from_spec(_spec)
        sys.modules[_name] = _module
        _spec.loader.exec_module(_module)

from app.app import _safe_extract_zip


@pytest.mark.parametrize(
    "member_name",
    ["../outside.txt", "ABSOLUTE_OUTSIDE", "sub/../../outside.txt"],
)
def test_rejects_path_traversal_without_writing_outside(tmp_path, member_name):
    destination = tmp_path / "dataset"
    outside = tmp_path / "outside.txt"
    if member_name == "ABSOLUTE_OUTSIDE":
        member_name = str(tmp_path / "absolute_outside.txt")
        outside = tmp_path / "absolute_outside.txt"
    archive_data = io.BytesIO()
    with zipfile.ZipFile(archive_data, "w") as archive:
        archive.writestr(member_name, "owned")
    archive_data.seek(0)

    with zipfile.ZipFile(archive_data) as archive, pytest.raises(HTTPException) as exc_info:
        _safe_extract_zip(archive, destination)

    assert exc_info.value.status_code == 400
    assert not outside.exists()
    assert not destination.exists()


def test_valid_archive_extracts_nested_members(tmp_path):
    destination = tmp_path / "dataset"
    archive_data = io.BytesIO()
    with zipfile.ZipFile(archive_data, "w") as archive:
        archive.writestr("nested/metadata.jsonl", '{"audio": "sample.wav"}\n')
        archive.writestr("nested/sample.wav", b"RIFF audio bytes")
    archive_data.seek(0)

    with zipfile.ZipFile(archive_data) as archive:
        _safe_extract_zip(archive, destination)

    assert (destination / "nested/metadata.jsonl").read_text() == (
        '{"audio": "sample.wav"}\n'
    )
    assert (destination / "nested/sample.wav").read_bytes() == b"RIFF audio bytes"


def test_rejects_unsafe_member_before_writing_safe_members(tmp_path):
    destination = tmp_path / "dataset"
    archive_data = io.BytesIO()
    with zipfile.ZipFile(archive_data, "w") as archive:
        archive.writestr("safe.txt", "must not be written")
        archive.writestr("../outside.txt", "owned")
    archive_data.seek(0)

    with zipfile.ZipFile(archive_data) as archive, pytest.raises(HTTPException) as exc_info:
        _safe_extract_zip(archive, destination)

    assert exc_info.value.status_code == 400
    assert not (destination / "safe.txt").exists()
    assert not destination.exists()


def test_rejects_member_name_collision_as_bad_archive(tmp_path):
    archive_data = io.BytesIO()
    with zipfile.ZipFile(archive_data, "w") as archive:
        archive.writestr("a", "file")
        archive.writestr("a/nested.txt", "conflict")
    archive_data.seek(0)

    with zipfile.ZipFile(archive_data) as archive:
        with pytest.raises(HTTPException) as exc_info:
            _safe_extract_zip(archive, tmp_path / "dataset")

    assert exc_info.value.status_code == 400


def test_rejects_symlink_members(tmp_path):
    archive_data = io.BytesIO()
    with zipfile.ZipFile(archive_data, "w") as archive:
        member = zipfile.ZipInfo("link")
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(member, "outside")
    archive_data.seek(0)

    with zipfile.ZipFile(archive_data) as archive:
        with pytest.raises(HTTPException) as exc_info:
            _safe_extract_zip(archive, tmp_path / "dataset")

    assert exc_info.value.status_code == 400
