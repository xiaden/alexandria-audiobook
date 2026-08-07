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
 * responds HTTP 503 with a Retry-After header (the transaction()
 * owner-thread contention contract: ``ConcurrentTransactionError`` is
 * mapped to 503 + Retry-After in the API layer).
 *
 * The Retry-After delay (integer seconds; the backend sends "1") is
 * honored before the single retry. Any non-retryable response — or a
 * second 503 — surfaces via ``handleError`` exactly as the plain
 * ``post()`` wrapper would. Never loops: at most 2 total POST attempts.
 */
export async function postWithRetryOnce<T = unknown>(endpoint: string, body: unknown): Promise<T> {
  let res = await fetch(endpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });

  if (res.status === 503 && res.headers.get('Retry-After') != null) {
    // Honor the retry delay. The backend sends integer seconds ("1");
    // the HTTP-date form is not produced here, so any unparseable value
    // falls back to a fixed 1s delay.
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
