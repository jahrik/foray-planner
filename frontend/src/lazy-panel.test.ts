import { describe, expect, it, vi } from "vitest";

import { createLazyLoader } from "./lazy-panel";

describe("createLazyLoader", () => {
  it("runs the loader once on the first open", async () => {
    const load = vi.fn().mockResolvedValue(true);
    const loader = createLazyLoader(load);

    loader.open();
    loader.open();
    await Promise.resolve();
    loader.open();

    expect(load).toHaveBeenCalledTimes(1);
  });

  it("does not start a second load while the first is in flight", () => {
    let resolve!: (loaded: boolean) => void;
    const load = vi.fn().mockReturnValue(new Promise<boolean>((r) => (resolve = r)));
    const loader = createLazyLoader(load);

    loader.open();
    loader.open();
    resolve(true);

    expect(load).toHaveBeenCalledTimes(1);
  });

  it("re-arms after a failed load so the next open retries", async () => {
    const load = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    const loader = createLazyLoader(load);

    loader.open();
    await Promise.resolve();
    loader.open();
    await Promise.resolve();
    loader.open();

    expect(load).toHaveBeenCalledTimes(2);
  });
});
