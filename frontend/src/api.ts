/**
 * API client for Alexandria backend
 * Ported from app/static/index.html lines 1214-1249
 */

import type {
  CloneReferenceListResponse,
  CloneReferenceUploadResponse,
  EffectiveWalkConfig,
  Persona,
  PersonaRerunRequest,
  PersonaRerunResult,
  PersonaRevision,
  PersonaValidationResponse,
  PersonaWriteRequest,
  PromptConfigRevision,
  PromptConfigValidationResponse,
  PromptConfigWriteRequest,
  ScopedWalkRerunRequest,
  ScopedWalkRerunResult,
} from './state';

/**
 * Handle HTTP error responses from the API
 */
export async function handleError(res: Response): Promise<void> {
  if (res.ok) return;
  
  try {
    const body = await res.json();
    throw new Error(body.detail || res.statusText);
  } catch (e) {
    if (e instanceof Error && e.message) throw e;
    throw new Error(res.statusText);
  }
}

/**
 * Perform a GET request to the API
 * @param endpoint - API endpoint (e.g., '/api/config')
 * @returns Parsed JSON response
 */
export async function get<T = unknown>(endpoint: string): Promise<T> {
  const res = await fetch(endpoint);
  await handleError(res);
  return res.json();
}

/**
 * Perform a POST request to the API
 * @param endpoint - API endpoint (e.g., '/api/config')
 * @param body - Request body (will be JSON.stringify'd)
 * @returns Parsed JSON response
 */
export async function post<T = unknown>(endpoint: string, body: unknown): Promise<T> {
  const res = await fetch(endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  await handleError(res);
  return res.json();
}

/**
 * POST a JSON body with exactly ONE automatic retry when the server
 * responds with a retryable status + Retry-After header.
 *
 * The live retryable contract is:
 *  - 409 (Plan I): snapshot restore is blocked while a walk/render is
 *    active (rule #10) — POST /api/pipeline/projects/load replies
 *    409 + Retry-After: 5 and the frontend retries once. Pass
 *    ``retryStatus=409`` for that contract.
 *
 * ``retryStatus`` defaults to 503 for backward compatibility with the
 * legacy two-argument callers (cancel_render/cancel_walks). That 503
 * contract is now live (Plan K): a concurrent pipeline storage write
 * raises ConcurrentTransactionError, which the API layer maps to 503 +
 * Retry-After: 5 app-wide — so cancel_render/cancel_walks retry once
 * when the write lock is held by a walk/render.
 *
 * The Retry-After delay (integer seconds; the load endpoint sends "5") is
 * honored before the single retry. Any non-retryable response — or a
 * second retryable response — surfaces via ``handleError`` exactly as the
 * plain ``post()`` wrapper would. Never loops: at most 2 total POST
 * attempts.
 */
export async function postWithRetryOnce<T = unknown>(
  endpoint: string,
  body: unknown,
  retryStatus: number = 503,
): Promise<T> {
  let res = await fetch(endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });

  if (res.status === retryStatus && res.headers.get('Retry-After') != null) {
    // Honor the retry delay. The backend sends integer seconds (currently
    // always "5" from the load 409); the HTTP-date form is not produced
    // here, so any unparseable value falls back to a fixed 1s delay.
    const seconds = parseInt(res.headers.get('Retry-After') as string, 10);
    const delayMs = Number.isFinite(seconds) && seconds >= 0 ? seconds * 1000 : 1000;
    await new Promise<void>((resolve) => setTimeout(resolve, delayMs));

    res = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  }

  await handleError(res);
  return res.json();
}

/**
 * Shared single-retry body used by the one-retry PUT/DELETE helpers.
 * Mirrors `postWithRetryOnce`: exactly ONE automatic retry when the server
 * responds with a retryable status + Retry-After header; the integer-seconds
 * delay is honored (fallback 1s), and any non-retryable response or a second
 * retryable response surfaces via `handleError` exactly as the plain helper
 * would. Never loops: at most 2 total attempts.
 */
async function withRetryOnce<T>(
  endpoint: string,
  init: RequestInit,
  retryStatus: number,
): Promise<T> {
  let res = await fetch(endpoint, init);

  if (res.status === retryStatus && res.headers.get('Retry-After') != null) {
    const seconds = parseInt(res.headers.get('Retry-After') as string, 10);
    const delayMs = Number.isFinite(seconds) && seconds >= 0 ? seconds * 1000 : 1000;
    await new Promise<void>((resolve) => setTimeout(resolve, delayMs));
    res = await fetch(endpoint, init);
  }

  await handleError(res);
  return res.json();
}

/**
 * Perform a PATCH request to the API
 * @param endpoint - API endpoint (e.g., '/api/pipeline/projects/{name}')
 * @param body - Request body (will be JSON.stringify'd)
 * @returns Parsed JSON response
 */
export async function patch<T = unknown>(endpoint: string, body: unknown): Promise<T> {
  const res = await fetch(endpoint, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  await handleError(res);
  return res.json();
}

/**
 * Perform a DELETE request to the API
 * @param endpoint - API endpoint (e.g., '/api/pipeline/projects/{name}')
 * @returns Parsed JSON response
 */
export async function del<T = unknown>(endpoint: string): Promise<T> {
  const res = await fetch(endpoint, {
    method: 'DELETE'
  });
  await handleError(res);
  return res.json();
}

/**
 * Perform a PUT request to the API
 * @param endpoint - API endpoint (e.g., '/api/pipeline/characters/{id}/voice')
 * @param body - Request body (will be JSON.stringify'd)
 * @returns Parsed JSON response
 */
export async function put<T = unknown>(endpoint: string, body: unknown): Promise<T> {
  const res = await fetch(endpoint, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  await handleError(res);
  return res.json();
}

/**
 * PUT with exactly ONE automatic retry when the server replies with
 * `retryStatus` (default 503, the pipeline concurrent-write contract) plus a
 * Retry-After header. Honors the integer-second delay. Used by the workbench
 * for writes that may contend with a concurrent pipeline storage write
 * (presence, overrides, boundary-overrides). Never loops: at most 2 attempts.
 */
export async function putWithRetryOnce<T = unknown>(
  endpoint: string,
  body: unknown,
  retryStatus: number = 503,
): Promise<T> {
  return withRetryOnce<T>(endpoint, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }, retryStatus);
}

/**
 * DELETE with exactly ONE automatic retry when the server replies with
 * `retryStatus` (default 503, the pipeline concurrent-write contract) plus a
 * Retry-After header. Honors the integer-second delay. Used by the workbench
 * for deleting overrides / boundary overrides that may contend with a
 * concurrent pipeline write. Never loops: at most 2 attempts.
 */
export async function delWithRetryOnce<T = unknown>(
  endpoint: string,
  body: unknown,
  retryStatus: number = 503,
): Promise<T> {
  return withRetryOnce<T>(endpoint, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }, retryStatus);
}

/**
 * POST a multipart ``FormData`` body with exactly ONE automatic retry on a
 * retryable status (default 503, the pipeline concurrent-write contract) plus
 * Retry-After. Honors the integer-second delay. Used by the clone-reference
 * upload (multipart ``audio`` part + optional ``ref_text``). Never loops.
 */
export async function postFormWithRetryOnce<T = unknown>(
  endpoint: string,
  form: FormData,
  retryStatus: number = 503,
): Promise<T> {
  // Do NOT set Content-Type — the browser must set the multipart boundary.
  const init: RequestInit = { method: 'POST', body: form };
  let res = await fetch(endpoint, init);
  if (res.status === retryStatus && res.headers.get('Retry-After') != null) {
    const seconds = parseInt(res.headers.get('Retry-After') as string, 10);
    const delayMs = Number.isFinite(seconds) && seconds >= 0 ? seconds * 1000 : 1000;
    await new Promise<void>((resolve) => setTimeout(resolve, delayMs));
    res = await fetch(endpoint, init);
  }
  await handleError(res);
  return res.json();
}

/**
 * DELETE with one automatic retry on a retryable status (default 503) plus
 * Retry-After, tolerating a 204 No Content response (no JSON body). Used by
 * the clone-reference delete endpoint (DELETE returns 204 without a body).
 * Never loops.
 */
export async function delNoContentWithRetryOnce(
  endpoint: string,
  retryStatus: number = 503,
): Promise<void> {
  let res = await fetch(endpoint, { method: 'DELETE' });
  if (res.status === retryStatus && res.headers.get('Retry-After') != null) {
    const seconds = parseInt(res.headers.get('Retry-After') as string, 10);
    const delayMs = Number.isFinite(seconds) && seconds >= 0 ? seconds * 1000 : 1000;
    await new Promise<void>((resolve) => setTimeout(resolve, delayMs));
    res = await fetch(endpoint, { method: 'DELETE' });
  }
  await handleError(res);
}

// ---------------------------------------------------------------------------
// Clone references (PipelineVoiceCloneReferenceAPI.v1)
// ---------------------------------------------------------------------------

/**
 * Upload a validated clone-voice reference audio sample.
 *
 * POST /api/pipeline/voices/{voice_id}/references — multipart ``audio`` file
 * plus optional ``ref_text``. Returns the new CloneReference and the updated
 * VoiceConfig (its ref_audio/ref_text now point at this reference).
 */
export async function uploadCloneReference(
  voiceId: string,
  file: File | Blob,
  filename: string,
  refText?: string,
): Promise<CloneReferenceUploadResponse> {
  const form = new FormData();
  form.append('audio', file, filename);
  if (refText !== undefined && refText !== '') {
    form.append('ref_text', refText);
  }
  return postFormWithRetryOnce<CloneReferenceUploadResponse>(
    `/api/pipeline/voices/${encodeURIComponent(voiceId)}/references`,
    form,
  );
}

/**
 * List the owner's clone references for a voice.
 * GET /api/pipeline/voices/{voice_id}/references
 */
export async function listCloneReferences(
  voiceId: string,
): Promise<CloneReferenceListResponse> {
  return get<CloneReferenceListResponse>(
    `/api/pipeline/voices/${encodeURIComponent(voiceId)}/references`,
  );
}

/**
 * Inline preview URL for a clone reference (GET .../preview streams inline
 * audio with Range support). Used directly as an <audio> src.
 */
export function cloneReferencePreviewUrl(voiceId: string, referenceId: string): string {
  return (
    `/api/pipeline/voices/${encodeURIComponent(voiceId)}` +
    `/references/${encodeURIComponent(referenceId)}/preview`
  );
}

/**
 * Attachment download URL for a clone reference (GET .../download is
 * attachment-only).
 */
export function cloneReferenceDownloadUrl(voiceId: string, referenceId: string): string {
  return (
    `/api/pipeline/voices/${encodeURIComponent(voiceId)}` +
    `/references/${encodeURIComponent(referenceId)}/download`
  );
}

/**
 * Tombstone and delete an owned clone reference.
 * DELETE /api/pipeline/voices/{voice_id}/references/{reference_id} → 204.
 */
export async function deleteCloneReference(
  voiceId: string,
  referenceId: string,
): Promise<void> {
  await delNoContentWithRetryOnce(
    `/api/pipeline/voices/${encodeURIComponent(voiceId)}` +
    `/references/${encodeURIComponent(referenceId)}`,
  );
}

// ---------------------------------------------------------------------------
// Persona (PipelineCharacterPersonaAPI.v1)
// ---------------------------------------------------------------------------

/**
 * Get the current head persona for a character.
 * GET /api/pipeline/characters/{character_id}/persona (404 if none).
 */
export async function getPersona(characterId: string): Promise<Persona> {
  return get<Persona>(
    `/api/pipeline/characters/${encodeURIComponent(characterId)}/persona`,
  );
}

/**
 * Save a persona revision (PUT /persona) with one 503 retry.
 */
export async function savePersona(
  characterId: string,
  write: PersonaWriteRequest,
): Promise<Persona> {
  return putWithRetryOnce<Persona>(
    `/api/pipeline/characters/${encodeURIComponent(characterId)}/persona`,
    write,
  );
}

/**
 * List persona revisions for a character (newest first).
 * GET /api/pipeline/characters/{character_id}/persona/revisions
 */
export async function listPersonaRevisions(
  characterId: string,
): Promise<PersonaRevision[]> {
  const res = await get<{ character_id: string; revisions: PersonaRevision[] }>(
    `/api/pipeline/characters/${encodeURIComponent(characterId)}/persona/revisions`,
  );
  return res.revisions;
}

/**
 * Side-effect-free persona validation.
 * POST /api/pipeline/characters/{character_id}/persona/validate
 */
export async function validatePersona(
  characterId: string,
  write: PersonaWriteRequest,
): Promise<PersonaValidationResponse> {
  return post<PersonaValidationResponse>(
    `/api/pipeline/characters/${encodeURIComponent(characterId)}/persona/validate`,
    write,
  );
}

/**
 * Explicit scoped persona rerun (confirm required).
 * POST /api/pipeline/characters/{character_id}/persona/rerun
 */
export async function rerunPersona(
  characterId: string,
  rerun: PersonaRerunRequest,
): Promise<PersonaRerunResult> {
  return post<PersonaRerunResult>(
    `/api/pipeline/characters/${encodeURIComponent(characterId)}/persona/rerun`,
    rerun,
  );
}

// ---------------------------------------------------------------------------
// Effective walk prompt config (PipelineWalkPromptConfigRevisionAPI.v1)
// ---------------------------------------------------------------------------

/**
 * Get the effective prompt/settings config for a book's nine fixed walks.
 * GET /api/pipeline/walks/{book_id}/config
 */
export async function getEffectiveWalkConfig(
  bookId: string,
): Promise<EffectiveWalkConfig> {
  return get<EffectiveWalkConfig>(
    `/api/pipeline/walks/${encodeURIComponent(bookId)}/config`,
  );
}

/**
 * Side-effect-free prompt-config validation.
 * POST /api/pipeline/walks/{book_id}/config/validate
 */
export async function validatePromptConfig(
  bookId: string,
  write: PromptConfigWriteRequest,
): Promise<PromptConfigValidationResponse> {
  return post<PromptConfigValidationResponse>(
    `/api/pipeline/walks/${encodeURIComponent(bookId)}/config/validate`,
    write,
  );
}

/**
 * Save a prompt-config revision (201) with one 503 retry.
 * POST /api/pipeline/walks/{book_id}/config/revisions
 */
export async function savePromptConfigRevision(
  bookId: string,
  write: PromptConfigWriteRequest,
): Promise<PromptConfigRevision> {
  return postWithRetryOnce<PromptConfigRevision>(
    `/api/pipeline/walks/${encodeURIComponent(bookId)}/config/revisions`,
    write,
  );
}

/** List prompt-config revisions for a book and task, newest first. */
export async function listPromptConfigRevisions(
  bookId: string,
  task: string,
): Promise<PromptConfigRevision[]> {
  const response = await get<{
    book_id: string;
    task: string;
    revisions: PromptConfigRevision[];
  }>(
    `/api/pipeline/walks/${encodeURIComponent(bookId)}/config/revisions?task=${encodeURIComponent(task)}`,
  );
  return response.revisions;
}

/**
 * Explicit scoped walk rerun (confirm required).
 * POST /api/pipeline/walks/{book_id}/reruns
 */
export async function rerunScopedWalk(
  bookId: string,
  rerun: ScopedWalkRerunRequest,
): Promise<ScopedWalkRerunResult> {
  return post<ScopedWalkRerunResult>(
    `/api/pipeline/walks/${encodeURIComponent(bookId)}/reruns`,
    rerun,
  );
}
