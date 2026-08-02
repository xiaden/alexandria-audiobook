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
 * Upload a file to the API
 * @param file - File to upload
 * @returns Parsed JSON response
 */
export async function upload<T = unknown>(file: File): Promise<T> {
  const formData = new FormData();
  formData.append('file', file);
  const res = await fetch('/api/upload', {
    method: 'POST',
    body: formData
  });
  await handleError(res);
  return res.json();
}
