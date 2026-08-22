"""Negative-space guard: legacy backend/frontend content must stay removed.

Plan Q Phase 9. Asserts that the legacy cutover is complete and stays complete:

1. Legacy files are absent: ``app/project.py``, the prompt modules/files
   (``app/{default,review,persona}_prompts.py`` + repo-root ``*.txt``), and the
   legacy frontend tabs (``editor-legacy.ts``, ``audio.ts``).
2. ``import app.app`` succeeds (without ``soundfile``) and the live route table
   exposes NONE of the 29 audit-verified legacy routes (P1-S2). Matching is by
   EXACT path against the unique legacy set plus family prefix check — substring
   matching would false-positive on the preserved ``/api/pipeline/voices`` and
   ``/api/pipeline/merge`` routes (exec-worker log L68).
3. ``frontend/src/state.ts`` no longer defines ``pipelineEnabled``.
4. No ``.py`` file under ``app/`` defines the identifier ``project_manager``
   (docstring/comment mentions of the legacy decoupling — e.g. ``app/engine.py``
   documenting the former ``project_manager.engine`` attribute — are not
   identifiers and are excluded; the plan's intent is identifier absence).

Test-harness import setup (legitimate, not a workaround):
- ``app/app.py`` does ``from utils import atomic_json_write`` and
  ``from hf_utils import ...`` at module level. Those modules live in ``app/``
  and resolve in production only because the app dir is on ``sys.path``
  (cwd=app/ or PYTHONPATH=app/). We cannot put ``app/`` on ``sys.path`` here:
  ``app/app.py`` is itself a top-level module named ``app``, so a plain
  ``app/`` path entry would make ``import app.app`` resolve ``app`` to
  ``app/app.py`` and fail with "'app' is not a package". Instead the two
  app-local modules are loaded explicitly into ``sys.modules``.
- ``soundfile`` is NOT required: since Plan Q Phase 4, ``app/app.py`` no longer
  imports ``app.tts``/``app.project`` at module level (engine access is lazy via
  ``app/engine.py``), so the import chain is clean.
"""

from __future__ import annotations

import importlib.util
import sys
import tokenize
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_DIR = _REPO_ROOT / "app"

# -- Test-harness import path setup ------------------------------------------
# pytest already puts the repo root on sys.path (tests/ and tests/pipeline/ are
# packages), but be explicit so this file also runs standalone.
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _load_app_local_module(name: str) -> None:
    """Load an app/*.py module under its bare name (e.g. ``utils`` -> app/utils.py).

    app/app.py imports these at module level; see module docstring for why the
    app dir cannot simply be added to sys.path.
    """
    spec = importlib.util.spec_from_file_location(name, _APP_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None, f"cannot locate app/{name}.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)


for _name in ("utils", "hf_utils"):
    if _name not in sys.modules:
        _load_app_local_module(_name)

import app.app

# -- 1. Legacy files are absent ----------------------------------------------


_LEGACY_FILES = [
    _APP_DIR / "project.py",
    _APP_DIR / "default_prompts.py",
    _APP_DIR / "review_prompts.py",
    _APP_DIR / "persona_prompts.py",
    _REPO_ROOT / "default_prompts.txt",
    _REPO_ROOT / "review_prompts.txt",
    _REPO_ROOT / "persona_prompts.txt",
    _REPO_ROOT / "frontend" / "src" / "tabs" / "editor-legacy.ts",
    _REPO_ROOT / "frontend" / "src" / "tabs" / "audio.ts",
]


@pytest.mark.parametrize(
    "legacy_path", _LEGACY_FILES, ids=lambda p: str(p.relative_to(_REPO_ROOT))
)
def test_legacy_file_absent(legacy_path: Path) -> None:
    assert not legacy_path.exists(), f"legacy file still present: {legacy_path}"


# -- 2. app.app imports and exposes none of the legacy routes -----------------


# Audit-verified legacy route paths (P1-S2): 29 decorators, 26 unique paths.
_LEGACY_ROUTE_PATHS = {
    "/api/default_prompts",
    "/api/upload",
    "/api/annotated_script",
    "/api/status/{task_name}",
    "/api/voices",
    "/api/cancel_persona",
    "/api/save_voice_config",
    "/api/audiobook",
    "/api/chunks",
    "/api/chunks/restore",
    "/api/chunks/{index}",
    "/api/chunks/{index}/insert",
    "/api/chunks/{index}/generate",
    "/api/merge",
    "/api/unload",
    "/api/export_audacity",
    "/api/merge_m4b",
    "/api/audiobook_m4b",
    "/api/m4b_cover",
    "/api/generate_batch",
    "/api/generate_batch_fast",
    "/api/cancel_audio",
    "/api/scripts",
    "/api/scripts/save",
    "/api/scripts/load",
    "/api/scripts/{name}",
}

# Legacy route families (plan P9-S1 token list, normalized to path prefixes).
# Catches any route re-added under a legacy family, not just the audited set.
_LEGACY_ROUTE_FAMILIES = (
    "/api/upload",
    "/api/annotated_script",
    "/api/chunks",
    "/api/scripts",
    "/api/status/",
    "/api/cancel_audio",
    "/api/audiobook",
    "/api/unload",
    "/api/voices",
    "/api/save_voice_config",
    "/api/cancel_persona",
    "/api/merge",
    "/api/audiobook_m4b",
    "/api/m4b_cover",
    "/api/export_audacity",
    "/api/generate_batch",
    "/api/default_prompts",
)


def test_app_imports_and_exposes_no_legacy_routes() -> None:
    routes = list(app.app.app.routes)
    route_paths = {getattr(route, "path", None) for route in routes}

    # Sanity: the route scan is live — the pipeline router must be mounted.
    assert "/api/pipeline/onboard" in route_paths, (
        "pipeline router not mounted — route scan is not live"
    )

    # Exact match against the audit-verified legacy set (no substring matching:
    # preserved /api/pipeline/voices and /api/pipeline/merge would false-positive).
    present_exact = sorted(route_paths & _LEGACY_ROUTE_PATHS)
    assert not present_exact, f"legacy routes still registered: {present_exact}"

    # Family prefix check for any route re-added under a legacy family.
    present_family = sorted(
        path
        for path in route_paths
        if any(path.startswith(family) for family in _LEGACY_ROUTE_FAMILIES)
    )
    assert not present_family, f"routes under legacy families still registered: {present_family}"


# -- 3. frontend/src/state.ts has no pipelineEnabled --------------------------


def test_state_ts_has_no_pipeline_enabled() -> None:
    state_ts = _REPO_ROOT / "frontend" / "src" / "state.ts"
    assert state_ts.exists(), "frontend/src/state.ts missing"
    assert "pipelineEnabled" not in state_ts.read_text(encoding="utf-8")


# -- 4. No project_manager identifier in app/*.py -----------------------------


def _is_project_manager_identifier(py_file: Path) -> bool:
    """True if ``project_manager`` appears as a NAME token (a real identifier).

    Docstring/comment mentions of the legacy decoupling (e.g. app/engine.py
    documenting the former ``project_manager.engine`` attribute) are
    STRING/COMMENT tokens and are intentionally excluded — the plan's intent is
    identifier absence, not prose absence.
    """
    with tokenize.open(py_file) as fh:
        for tok in tokenize.generate_tokens(fh.readline):
            if tok.type == tokenize.NAME and tok.string == "project_manager":
                return True
    return False


def test_no_project_manager_identifier_in_app() -> None:
    offenders = []
    for py_file in sorted(_APP_DIR.rglob("*.py")):
        if _is_project_manager_identifier(py_file):
            offenders.append(str(py_file.relative_to(_REPO_ROOT)))
    assert not offenders, f"'project_manager' identifier present in: {offenders}"
