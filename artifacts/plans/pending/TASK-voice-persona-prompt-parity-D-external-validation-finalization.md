# Task: Voice-Persona-Prompt Parity D — external capability discovery, clone/design/LoRA integration tests, and final closure

## Problem Statement

This is part D (final) of the implementation plan for `artifacts/designs/pending/DD-voice-persona-prompt-parity-browser-external-validation.md`. Parts A–C are prerequisites. This part delivers the external-validation harness and integration coverage that the DD requires while the earlier parts delivered the pipeline-native surfaces: (1) external-engine capability discovery that reports a clear matrix of supported/unavailable/failed capability states (clone, design, builtin LoRA, LoRA, media decode/encode, required model/runtime features), (2) clone/design/LoRA integration tests that exercise the seams only when the capability is actually available — an unavailable capability is NEVER a green pass, and real-engine smoke tests are opt-in and isolated from deterministic CI, and (3) final project-wide closure: update docs, confirm all gates (test/lint/type/coverage/build/security), truthful environmental reporting, archive the completed plans with evidence, and hand back a director-level report.

## Dependencies

- **Prerequisite:** `artifacts/plans/pending/TASK-voice-persona-prompt-parity-A-...`, `-B-...`, `-C-...` (all backend + frontend delivered).
- Design: `artifacts/designs/pending/DD-voice-persona-prompt-parity-browser-external-validation.md`.
- `app/pipeline/tts_integration.py`, engine factory (`get_tts_engine`), `app/pipeline/adapter.py` (InMemorySQLiteAdapter for fixtures), fake-engine pattern already used by `tests/pipeline/test_tts_integration.py` (2230 lines) and `test_voices.py`. Real-engine surfaces (Artifactory/local Ollama) are opt-in via explicit environment markers; the deterministic suite never requires them.
- Invariants: no unavailable capability produces a green test (unavailable → marked `unavailable`, not `passed`); real-engine smoke tests opt-in and isolated with fixture provenance + tool versions; deterministic Python + TypeScript suites, deterministic build + committed-dist gate, and explicit environment-only failure classification retained; `app/tts.py` byte-identical; legacy guard 12/12; protected combined-walks workbench unchanged.

## Phases

### Phase 1: External capability discovery harness (S1)
- [ ] Add an external-validation module (test/validation harness OUTSIDE production routers, e.g. `tests/external/` with `capability_matrix.py` or reuse a markers file): `discover_capabilities()` probes and returns per-capability `{capability, status: supported|unavailable|failed, detail, evidence}` for clone, design, builtin_lora, lora, media_decode, media_encode, and required model/runtime-features; each probe is isolated so a missing engine marks only that capability unavailable (or failed if the engine exists but errors) — never silently skipped into a green pass. Include fixture provenance (which media fixture/license) and tool versions in the report.
    **Notes:** The harness lives outside the production routers (DD layer-mapping: "Test/validation harness outside production routers"). Deterministic CI runs discovery and asserts that unimplemented capabilities report unavailable (not passed); real-engine availability is environment-gated via an opt-in env marker (e.g. `ALEXANDRIA_EXTERNAL=1`).
- [ ] Verify: a new test `tests/external/test_capability_discovery.py` green in deterministic CI asserting the matrix reports `unavailable` when engines are absent (never green), `failed` on a present-but-broken engine, and that tool-version/fixture-provenance fields are present in the report.
    **Notes:** This is the DD acceptance "browser/external checks distinguish pass, fail, unavailable; no unavailable engine produces a green test."

### Phase 2: Clone/design/LoRA integration tests (S2)
- [ ] Add integration tests under `tests/external/` (or `tests/pipeline/test_external_integration.py`) that exercise the clone/design/builtin_LoRA/LoRA seams through the existing engine factory and `tts_integration.py` public seams, using the fake engine implementing the existing generate contract for deterministic behavior and the fixed media fixtures. Cover: clone reference rendering path (preview through TTS seam), design voice rendering, builtin LoRA + LoRA adapter rendering, and narrator fallback. Each availability-aware test propagates the capability status — when the engine is the deterministic fake it always passes; when gated to a real engine it is opt-in and reports unavailable/failed distinctly.
    **Notes:** These are "integration tests" in the DD sense (clone/design/LoRA integrations against the existing TTS seam + engine factory), not new production paths. No arbitrary engine arguments; only existing public seams. `app/tts.py` unchanged.
- [ ] Verify: `pytest -q tests/external/` in deterministic CI green using fake engines; optional real-engine marker run is documented and reports unavailable where deps are missing (never false-green).

### Phase 3: Documentation, security, and final gates (S3)
- [ ] Update project docs to match code: confirm CONTRACTS.md has all three contracts + delivered routes/DTOs/tables documented (parts A/B appended; C delivery lines), and add/refresh any pipeline documentation index/codemap touched by the new clone/persona/prompt surfaces (use the `update-docs` skill convention if a codemap/tab mapping doc exists). No legacy prompt-module references.
- [ ] Run the full security/compatibility gate: confirm no secret/raw-path exposure in responses, path/size/duration constraints enforced, owner checks on every reference/revision, `app/tts.py` byte-identical (`git diff --exit-code`), no legacy endpoint/manifest/prompt-file module, no auto-cascade, no arbitrary engine args. Confirm `pytest -q` (full), legacy guard, `tsc --noEmit`, `vitest run`, `npm run build && git diff --exit-code app/static/dist/`, and coverage.
    **Notes:** Record truthfully which environment-dependent checks (real-engine smoke) were NOT run because engines are unavailable — environmental reporting must be explicit, never a silent pass. Coverage gate: frontend `--coverage`, backend `pytest --cov`.
- [ ] Verify all gates green and record exact counts/deltas (backend test count, frontend vitest count, coverage %, dist hash, legacy guard 12/12, tts SHA).

### Phase 4: Archive plans and director report (S4)
- [ ] Archive the four completed TASK-voice-persona-prompt-parity plans (`plan_archive`) with evidence from annotations (commit SHAs, test counts, gate outputs), moving them from `artifacts/plans/pending/` to `artifacts/plans/completed/`.
- [ ] Commit closure: scoped commit(s) for docs update + final gate evidence if any source changed (docs-only otherwise), and produce the director-level report: plan paths, commits, tests, blockers, changed files, environmental notes, and confirmation the third DD is untouched.

## Completion Criteria

- External capability discovery matrix reports `supported|unavailable|failed` distinctly for clone/design/builtin_lora/lora/media-decode/media-encode/model-runtime; unavailable is never a green pass; real-engine smoke opt-in and isolated with fixture provenance + tool versions.
- Clone/design/LoRA integration tests pass deterministically via fake engines through existing public seams; availability-aware and opt-in; `app/tts.py` byte-identical.
- Docs updated (CONTRACTS parity-owned deliveries; no legacy references); security/compatibility gates pass.
- All four plans archived with evidence; director report delivered with plan paths, commits, tests, blockers, changed files.
- Deterministic suites (Python + TS), committed-dist gate, legacy guard 12/12, and explicit environment-only failure classification all green/nodegrading.

## References

- `artifacts/designs/pending/DD-voice-persona-prompt-parity-browser-external-validation.md`
- `artifacts/plans/pending/TASK-voice-persona-prompt-parity-A-...` / `-B-...` / `-C-...`
- `artifacts/plans/completed/TASK-combined-walks-workbench-workbench-foundation.md`
- `tests/pipeline/test_tts_integration.py`, `tests/pipeline/test_voices.py` (fake-engine conventions)
