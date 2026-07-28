// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  isDifferentUiBuild,
  UiUpdateBadge,
} from "@/components/workspace/ui-update-badge";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("UiUpdateBadge", () => {
  it("recognizes only a different non-empty server build", () => {
    expect(isDifferentUiBuild({ build: "next" }, "current")).toBe(true);
    expect(isDifferentUiBuild({ build: "current" }, "current")).toBe(false);
    expect(isDifferentUiBuild({ build: "" }, "current")).toBe(false);
    expect(isDifferentUiBuild(null, "current")).toBe(false);
  });

  it("offers a user-controlled reload when the server bundle changes", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => ({ build: `${__PIKVM_UI_BUILD__}-new` }),
      })),
    );

    render(<UiUpdateBadge />);

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Reload interface update" })
          .textContent,
      ).toContain("Update ready");
    });
  });

  it("stays out of the header when the build still matches", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => ({ build: __PIKVM_UI_BUILD__ }),
      })),
    );

    render(<UiUpdateBadge />);

    await waitFor(() => expect(fetch).toHaveBeenCalledOnce());
    expect(
      screen.queryByRole("button", { name: "Reload interface update" }),
    ).toBeNull();
  });
});
