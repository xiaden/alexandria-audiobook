/**
 * API client for Alexandria backend
 * Ported from app/static/index.html lines 1214-1249
 */

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
