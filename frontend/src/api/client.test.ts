import { afterEach, describe, expect, it, vi } from "vitest";

import { deleteJson, getJson, openRefreshStream, postJson } from "./client";

function jsonResponse(body: unknown, status = 200, statusText = ""): Response {
  return new Response(body === null ? null : JSON.stringify(body), {
    status,
    statusText,
    headers: { "content-type": "application/json" },
  });
}

/** openapi-fetch calls the injected fetch with a `Request`; pull its URL back out. */
function requestedUrl(fetchMock: ReturnType<typeof vi.fn>): string {
  const input = fetchMock.mock.calls[0]?.[0] as Request | string | URL;
  return input instanceof Request ? input.url : String(input);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("getJson", () => {
  it("resolves with the parsed body on 200", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ refreshing: false })));
    await expect(getJson("/api/config")).resolves.toMatchObject({ refreshing: false });
  });

  it("throws the server's { detail } on a non-2xx with a JSON body", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({ detail: "catalog not built" }, 409)));
    await expect(getJson("/api/config")).rejects.toEqual({ detail: "catalog not built" });
  });

  it("throws a synthetic ApiError when a non-2xx has no body (a bare proxy 502)", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse(null, 502, "Bad Gateway")));
    await expect(getJson("/api/config")).rejects.toEqual({ detail: "Bad Gateway" });
  });
});

describe("postJson / deleteJson", () => {
  it("threads a query param into the request URL", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ status: "started" }));
    vi.stubGlobal("fetch", fetchMock);
    await postJson("/api/refresh", { params: { query: { target: "camps" } } });
    expect(requestedUrl(fetchMock)).toContain("target=camps");
  });

  it("substitutes a path param into the request URL", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(null, 200));
    vi.stubGlobal("fetch", fetchMock);
    await deleteJson("/api/genera/{taxon_id}", { params: { path: { taxon_id: 47348 } } });
    expect(requestedUrl(fetchMock)).toContain("/api/genera/47348");
  });
});

describe("openRefreshStream", () => {
  it("parses each SSE message and routes non-JSON to onMalformed", () => {
    class FakeEventSource {
      handlers: Record<string, (event: MessageEvent<string>) => void> = {};
      addEventListener(type: string, handler: (event: MessageEvent<string>) => void): void {
        this.handlers[type] = handler;
      }
      close(): void {}
    }
    vi.stubGlobal("EventSource", FakeEventSource);

    const events: unknown[] = [];
    const malformed: string[] = [];
    const source = openRefreshStream(
      (event) => events.push(event),
      (raw) => malformed.push(raw),
    ) as unknown as FakeEventSource;

    source.handlers.message?.(new MessageEvent("message", { data: '{"step":"Starting","progress":0}' }));
    source.handlers.message?.(new MessageEvent("message", { data: "<html>not json</html>" }));

    expect(events).toEqual([{ step: "Starting", progress: 0 }]);
    expect(malformed).toEqual(["<html>not json</html>"]);
  });
});
