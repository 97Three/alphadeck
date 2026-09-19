export class ApiError extends Error {
  constructor(message: string, public status: number, public code?: string) {
    super(message);
    this.name = 'ApiError';
  }
}

export type ApiEnvelope<T> = { success?: boolean; data?: T; message?: string; error?: string };

export async function apiRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  headers.set('X-AlphaDeck-Request', 'same-origin');
  const response = await fetch(path, { ...init, headers, credentials: 'same-origin', cache: 'no-store' });
  const contentType = response.headers.get('content-type') ?? '';
  const payload: unknown = contentType.includes('json')
    ? await response.json().catch(() => null)
    : await response.text().catch(() => '');
  const record = payload && typeof payload === 'object' ? payload as Record<string, unknown> : {};
  if (!response.ok || record.success === false) {
    const message = typeof record.message === 'string' ? record.message
      : typeof record.error === 'string' ? record.error
      : typeof payload === 'string' && payload ? payload
      : `请求失败（HTTP ${response.status}）`;
    throw new ApiError(message, response.status, typeof record.error === 'string' ? record.error : undefined);
  }
  return payload as T;
}

export function unwrap<T>(payload: ApiEnvelope<T> | T): T {
  if (payload && typeof payload === 'object' && 'data' in payload) {
    return (payload as ApiEnvelope<T>).data as T;
  }
  return payload as T;
}

export function jsonBody(value: unknown): RequestInit {
  return { method: 'POST', body: JSON.stringify(value) };
}
