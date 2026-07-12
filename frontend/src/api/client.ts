import type { ApiError } from "./types";

// Thin fetch helpers. On a non-2xx response we reject with the parsed FastAPI error
// body ({ detail }) so callers can surface `error.detail`.

export async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path);
  if (!response.ok) throw (await response.json()) as ApiError;
  return (await response.json()) as T;
}

export async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw (await response.json()) as ApiError;
  return (await response.json()) as T;
}
