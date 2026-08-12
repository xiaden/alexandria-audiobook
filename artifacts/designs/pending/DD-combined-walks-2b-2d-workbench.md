# Combined Frontend Workbench for Walks 2b–2d — Design Document

**Status:** Pending  
**Author:** rnd-dd-author  
**Date:** 2026-08-12  
**Consistency gates:** all schema/API changes are registered in [CONTRACTS.md](../parts/epub-audiobook-pipeline-rewrite/CONTRACTS.md); active pipeline contract is [DD-epub-audiobook-pipeline-rewrite-v3.md](../completed/DD-epub-audiobook-pipeline-rewrite-v3.md).  

**Related Documents:**
- [Universal Upgrade](../pending/DD-universal-upgrade.md) — Current universal-upgrade capabilities and pipeline-only constraints.
- [Pipeline v3 contract](../completed/DD-epub-audiobook-pipeline-rewrite-v3.md) — Active nine-walk pipeline contract.
- [Authoritative contracts ledger](../parts/epub-audiobook-pipeline-rewrite/CONTRACTS.md) — Append-only schema/API registration ledger.

---

## Scope

Pipeline-native frontend workbench and supporting contracts for character discovery (2b), alias resolution (2c), and scene presence (2d).

---

## Problem Statement

The pipeline has character rows and junctions but no scene-facing API, no safe alias-management workflow, no durable manual-decision protection across reruns, and no unified frontend for inspecting generated character/scene evidence. Existing review items are unified but walk items render incompletely, per-book walk_override has no HTTP surface, and 2b reruns can duplicate junctions. Users need one accessible workbench that makes generated state, evidence, decisions, configuration, conflicts, and rerun consequences understandable without violating pipeline-only architecture.

---

## Architecture

Use a unified normalized workbench contract projected from existing pipeline rows: shared setup, stable source anchors, canonical characters and aliases, scene presence relations, generated suggestions, manual decisions, provenance, conflicts, and generation revision. Add thin pipeline API routes in existing api_* modules and a new workbench frontend tab following the {tab}-tab convention. Keep generated values separate from manual decisions; reruns create a new generation revision, reconcile by stable IDs/anchors, preserve protected decisions, and surface unresolved conflicts. Alias conversion is a preview-then-commit command with explicit consequences and reversible decision records. Reads use rows as truth; manifests remain derived. The first delivery must add explicit representations for alias identity, merge-review/undo, human absence, boundary overrides, and rerun provenance; today's destructive 2c merge, row absence, and index-only operations cannot provide those guarantees.

---

## Design Goals

Provide coherent 2b–2d journeys; make scene text and evidence navigable; protect manual decisions; make alias merges safe and explainable; expose effective configuration; support keyboard and accessible review; preserve universal-upgrade concurrency, review-band, run-history, cancellation, and committed-dist contracts.

---

## Constraints

Pipeline-only: routes remain under /api/pipeline in the existing seven api_* modules; app/tts.py and legacy guards remain untouched. Use v3 names walk_2b_character_discovery, walk_2c_alias_resolution (GLOBAL), walk_2d_scene_presence (LOCAL). Review thresholds remain <0.5 reject, 0.5–0.7 review, >=0.7 accept; no degraded-confidence auto-accept. SQLite writes use BEGIN IMMEDIATE/owner-thread guard and return 503 with Retry-After: 5 on concurrency. Existing scene rows have no title; scene identity is derived from stable scene_id and document hierarchy. Frontend dist is committed and must pass the existing build/diff gate.

---

## Open Questions

Actor identity remains timestamp/session only for this delivery; scene labels remain derived; shared setup/logging remains owned by this workbench. Reruns default to the requested dependency scope (book or explicit scenes), never automatic. Alias unmerge is supported while its merge record is active. These choices remove the former ambiguity; no competing alias or absence representation is permitted.

---

## Requirements

1. Provide user journeys and IA for a combined workbench with setup, navigator, review, and decision history.
2. Show scene navigator, source-text highlights, character ledger, aliases, presence, review states, provenance, and conflicts.
3. Provide alias conversion preview with affected characters, junctions, spans/scenes, protected decisions, and downstream consequences, with explicit confirmation.
4. Define precise contracts for overrides, stable anchors, merges, presence edits, invalidation, and manual-decision protection.
5. Add a reusable collapsible setup component with effective config precedence and validation.
6. Cover accessibility/keyboard use, undo/confirmation/errors, concurrency/conflicts, security, alternatives, non-goals, phased guidance, tests, and measurable acceptance criteria.
7. Preserve pipeline-only architecture and universal-upgrade features.

---

## Layer Mapping

Frontend: a new workbench tab, reusable CollapsibleWalkSetup, typed API client, selectors, focus/error/undo state. API: thin routes in api_walks.py, api_review.py, api_characters.py, and api_operations.py. Domain/persistence: reuse ReviewManager, CharacterLedger, PipelineStorage, resolve_task_config, transaction(), and 2c merge primitives; add durable decision/revision records where the existing schema cannot protect intent. Walk runner remains the only execution owner.

---

## Data Model

WorkbenchState contains book_id, generation_revision, scenes[{scene_id, chapter_id, position, anchor, spans, characters}], characters[{id,name,aliases,voice_assignment_id}], presence[{scene_id,character_id,relation_type,source,confidence,human_override,revision}], review items, overrides, effective_config, conflicts, and protected_decisions. Anchor = immutable source identity (book_id, scene_id, paragraph_id, span_id, and paragraph character offset where applicable); presentation position is display metadata only, never identity. Durable decision records are append-only and carry decision_id, target IDs, base_revision, payload, created_ms, and status. Boundary overrides require stable chapter/scene/paragraph anchors. Revisions are allocated only by the per-book workbench_generation row; that allocator has no book-row reference. Generated and manual presence projections are separate rows with independent target uniqueness; disagreement is a conflict, not a duplicate insert. Rerun reconciliation is keyed by stable IDs, upserts effective generated rows, preserves manual rows, records source_run_id, and cannot duplicate 2b rows or re-add a human absence.

The exact tables, columns, constraints, revision allocator, and migration contract are registered in CONTRACTS.md. The convergent alias model is a non-destructive `character_alias_merge` relation: member characters remain addressable, an active relation projects them under its canonical character, and the relation stores prior voice assignments and downstream impact for unmerge. Human absence is an active `character_scene_absence` tombstone, not a NULL or deleted junction. Generated 2b/2d rows are unique by stable target key only, never by source_run_id; source_run_id is provenance.

---

## API Surface

GET /api/pipeline/workbench/{book_id} returns the normalized read model, including scene hierarchy, spans/highlights, characters, aliases, presence, unified review items, conflicts, run summaries/result counters, overrides, and effective config.
GET /api/pipeline/workbench/{book_id}/config returns global config, DB walk_override rows, effective values, source tier, and validation errors.
GET /api/pipeline/workbench/{book_id}/boundary-overrides returns active chapter/scene/paragraph boundary overrides as BoundaryOverrideDTOs. PUT `/boundary-overrides` accepts BoundaryOverrideWriteDTO `{override_id?,anchor,payload,base_revision}`; POST `/boundary-overrides/{override_id}/apply` applies one atomically; DELETE `/boundary-overrides/{override_id}` accepts `{base_revision}` and deactivates the identified override while retaining its decision/provenance. DELETE is boundary-specific and never removes unrelated override keys. These routes and the boundary model are unconditional workbench scope. Any 2a rerun must consume active overrides or return 409 and may never erase or bypass them.
PUT /api/pipeline/workbench/{book_id}/overrides accepts {walk_name,key,value,base_revision}; only approved keys/types for 2b/2c/2d are accepted and returns the effective value/source. DELETE removes the DB tier.
POST /api/pipeline/workbench/{book_id}/alias-conversions/preview accepts {canonical_id,member_ids,base_revision}; returns a short-lived book-scoped preview token, normalized group, alias result, every affected junction/span/scene row, protected decisions, voice-assignment consequences, merge-review consequences, conflicts, and downstream 2d implications. POST .../commit accepts {preview_token,base_revision,confirm_consequences}; atomically applies only if preview and revision still match and preserves a reversible record before any member removal.
PUT /api/pipeline/workbench/{book_id}/presence accepts {scene_id,character_id,relation_type:'present'|'speaker'|'absent',decision_id,base_revision}; returns the relation and new revision. Removal must create the selected human absence/tombstone representation so 2d cannot re-add it.
POST /api/pipeline/workbench/{book_id}/reruns accepts {walk_name:'walk_2b_character_discovery'|'walk_2c_alias_resolution'|'walk_2d_scene_presence',scope:'book'|'scenes',scene_ids?,preserve_manual_decisions:true|false,base_revision}; starts an explicit runner execution and returns run_id. `scenes` requires a non-empty reachable scene_ids list; 2c rejects `scenes` with 422 because it is book-global. No review action auto-runs a walk. Preserve is default; replacement requires confirmation. Any 2a rerun is subject to the boundary-override protection above.
Existing POST /review/accept|reject|override remains the resolution authority. Bodies are `{item_id,base_revision}` for accept/reject and `{item_id,new_value,base_revision}` for override. Dispatch is exact: `decision:{uuid}` resolves an active workbench decision; `junction:{table}:{character_id}:{entity_id}` resolves that allow-listed live junction (accept sets confidence 1, reject creates the applicable human absence/removal decision, override validates and writes the typed value); `walkitem:{id}` resolves that book-scoped walk_review_item (accept marks resolved, reject restores prior_value, override validates/writes the supplied value, then marks resolved). Each action is one transaction, creates a decision record, transitions the target, and returns ActionResultDTO. Undo is `POST /workbench/{book_id}/decisions/{decision_id}/undo` with `{base_revision}`; it creates an inverse decision and returns 409 rather than overwriting a newer decision. 404 means unknown/cross-book item, 422 validation, and 503 includes Retry-After:5 for transaction contention.

All request/response DTOs, item-ID encoding, status enums, and route registration are authoritative in CONTRACTS.md. Workbench decision IDs are `decision:{uuid}`; existing `junction:{table}:{character_id}:{entity_id}` and `walkitem:{id}` IDs remain accepted by review actions. Workbench actions return `{item_id, decision_id, status, generation_revision, superseded_item_ids, conflict}` and preserve the existing accept/reject/override response fields. Undo posts the decision ID and returns 409 when a newer revision exists; supersede/conflict responses identify both the current item and competing decision.

---

## Workflows

Journey A: open Workbench → choose scene → inspect highlighted span evidence and character ledger → filter pending/accepted/rejected/protected/conflict → accept/reject/typed override with toast and undo where reversible. Journey B: select aliases → preview conversion → inspect consequences and protected decisions → confirm → refresh all affected scenes/review items. Journey C: edit presence → confirm destructive removal → save decision → rerun explicitly if desired; protected decisions win over generated output, while conflicts are surfaced rather than silently overwritten. Journey D: expand setup → edit per-walk prompt/model/temperature → see field-specific effective source and validation → save; collapse preserves state. Rerun completion supersedes stale walk_review_item targets, reconciles junctions idempotently, and never deletes characters or manual decisions.

Rerun scope is `book` or the explicit `scenes` set; 2b is scoped to source scenes, 2c always reconciles the book-level alias graph, and 2d is scoped to affected scenes. A 2b change invalidates 2c and 2d for affected characters/scenes; a 2c merge invalidates 2d for the union of member scenes; a 2d change invalidates no upstream walk. The runner marks dependent generated rows/review items stale before execution, atomically allocates the next `workbench_generation.revision` under BEGIN IMMEDIATE, writes only that generation, and reconciles generated rows by `(character_id, scene_id, relation_type)` (2d) or stable character/anchor keys (2b). `source_run_id` is stored separately and is never part of uniqueness. Generated rows are upserted; manual rows and active absence tombstones win. Failed/interrupted/cancelled runs retain their run row and committed prior generation, mark only writes from that run failed/partial, and never supersede the last successful revision. Normal lifecycle is pending → running → completed|failed|interrupted|cancelled, with heartbeat/cancel_requested and startup stale-run reconciliation as specified by the universal-upgrade contract.

---

## IA and Interaction Contracts


---

## Configuration and Validation

CollapsibleWalkSetup is reusable by 2b/2c/2d and exposes only typed fields allowed by the task contract. For model_name, reasoning_effort, and temperature the order is DB row → `llm.task_overrides[task]` → `llm` global → hardcoded fallback. For prompt the order is DB row → top-level `config.walk_override[task].prompt` → `llm.task_overrides[task].prompt` → `llm.prompt` → hardcoded fallback; empty/non-string values fall through. `resolve_task_config` returns the source per field and implements this split chain without changing existing walk prompt behavior. Validation rejects unknown walk names, unsupported per-task keys, invalid temperature/model values, oversized prompts, and malformed JSON before persistence; secrets are never returned.

---

## Safety, Concurrency, and Security

All writes validate book ownership/reachability and stable IDs, parameterize SQL, whitelist target columns/keys, and escape text in DOM rendering. Alias preview tokens are short-lived, book-scoped, and bind canonical/member IDs plus base_revision. Commit uses one transaction and optimistic revision checks; stale previews never apply. Manual decisions are protected by decision_id/base_revision and never overwritten by a walk; generated suggestions become conflicts. Active-run/rerun and snapshot protections remain those of universal-upgrade. Errors are typed, actionable, and do not expose prompts, paths, SQL, or secrets.

---

## Alternatives Considered

Per-walk screens were rejected because they duplicate setup and hide alias/presence consequences. A client-only composition of export/review endpoints was rejected because scenes and anchors are absent from current payloads and writes need atomic protection. Full event sourcing was rejected for first delivery as disproportionate; append-only decision provenance plus normalized current state preserves the required safety without replay infrastructure.

---

## Non-Goals

No changes to TTS, rendering, legacy endpoints, walk ordering, LLM prompt semantics beyond typed overrides, scene segmentation (2a), span attribution (2e), voice catalog redesign, collaborative identity/permissions, or automatic reruns. No array-index-based persistence and no silent hard-delete/unmerge behavior.

---

## Phased Guidance

Phase 1: establish stable anchors, decision/provenance records, merge-review/undo, explicit absence, boundary protection, and idempotent rerun reconciliation; then deliver the read model, navigator/highlights, unified review rendering, run counters, and reusable setup. Phase 2: typed decisions, presence editing, conflict/undo UX, and safe alias preview/commit. Phase 3: explicit scoped rerun/invalidation reconciliation and committed-dist/accessibility hardening. Concrete frontend integration is `frontend/index.html` (nav and pane), `frontend/src/main.ts` (`initWorkbench()` wiring), `frontend/src/api.ts` (typed client; reuse one-retry `postWithRetryOnce` semantics and extend equivalent PUT/DELETE handling for 503), `frontend/src/state.ts`, new `frontend/src/tabs/workbench.ts`, and new `frontend/tests/frontend/test_workbench.test.ts`. Walk-item rendering and structured overrides are new UI surface; alias UI extends `frontend/src/tabs/voices.ts` patterns and rerun/preserve UX follows `frontend/src/tabs/projects.ts`. Build output is committed under `app/static/dist/` including the hash-renamed asset and is covered by the existing build/diff gate. These are structural slices of one cohesive delivery, not rollout stages or an implementation plan.

---

## Tests and Acceptance Criteria

Backend tests cover exact schemas, scope, anchor reachability, precedence/source validation, preview expiry and stale commit, preview==apply row equality, merge consequences/undo/unmerge, protected decisions, explicit absence surviving 2d rerun, idempotent 2b/2d reconciliation, failed/partial runs, walk_run/cancellation/reconciliation, snapshot blocking, 503 Retry-After, and review-union compatibility including merge items. Frontend tests cover IA, keyboard/disclosure/focus, highlight synchronization, typed walk-item actions, confirmation/undo/errors, config source badges, conflicts, run polling/cancellation, and committed build, with minimal browser coverage for keyboard-equivalent presence/merge paths. Acceptance: every workbench read shows scene_id and stable anchors; every stale write returns 409; protected decisions never change after rerun; alias preview enumerates every affected row, voice consequence, and preview token; preview and commit affected-row sets match; 2b/2d reruns produce no duplicates and never re-add explicit absence; effective config matches and reports precedence; WCAG 2.2 AA keyboard core journeys work without color-only meaning; `npm run build && git diff --exit-code app/static/dist/` passes; legacy guard remains 12/12; all new routes and DTOs are pipeline-prefixed and registered in CONTRACTS.md.

---

## Evidence Trail

Grounded in completed research outputs particular-scarlet-tapir, wicked-violet-iguana, clean-gray-bison, colorful-rose-lobster, unsightly-olive-grouse, and complexity critique christian-white-swordfish; its blocking findings are reflected explicitly rather than assuming current destructive 2c or duplicate-prone 2b/2d behavior is safe.

---
