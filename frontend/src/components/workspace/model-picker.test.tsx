// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ProviderMap } from "@/types";
import { ModelPicker } from "./model-picker";

const providers: ProviderMap = {
  "strong-reasoner": {
    configured_model: "opus",
    ready: true,
    routes: [
      { role: "reasoner", position: 1 },
      { role: "verifier", position: 1 },
    ],
  },
  "fast-controller": {
    configured_model: "gpt-fast",
    ready: true,
    routes: [{ role: "controller", position: 1 }],
  },
};

afterEach(cleanup);

describe("ModelPicker", () => {
  it("shows the effective planning, acting, and checking models at send time", async () => {
    const user = userEvent.setup();
    const onOpenModels = vi.fn();
    render(
      <ModelPicker
        providers={providers}
        preferences={{}}
        locked={false}
        onOpenModels={onOpenModels}
      />,
    );

    const button = screen.getByRole("button", {
      name: /reasoning: opus.*acting: gpt-fast.*checking: opus/i,
    });
    expect(button.textContent).toContain("Opus + gpt-fast");
    await user.click(button);
    expect(onOpenModels).toHaveBeenCalledOnce();
  });

  it("marks a snapshotted run route as locked", () => {
    render(
      <ModelPicker
        providers={providers}
        preferences={{ controller: "strong-reasoner" }}
        activeRoute={{
          reasoner: ["strong-reasoner"],
          controller: ["fast-controller", "strong-reasoner"],
          verifier: ["strong-reasoner"],
        }}
        locked
        onOpenModels={() => undefined}
      />,
    );

    const button = screen.getByRole("button", {
      name: /acting: gpt-fast/i,
    });
    expect(button.getAttribute("title")).toContain("Locked for this run");
    expect(button.textContent).toContain("Opus + gpt-fast");
  });

  it("keeps a three-model route compact while preserving role detail", () => {
    render(
      <ModelPicker
        providers={{
          planner: {
            configured_model: "opus",
            ready: true,
            routes: [{ role: "reasoner", position: 1 }],
          },
          actor: {
            configured_model: "gpt-fast",
            ready: true,
            routes: [{ role: "controller", position: 1 }],
          },
          checker: {
            configured_model: "haiku",
            ready: true,
            routes: [{ role: "verifier", position: 1 }],
          },
        }}
        preferences={{}}
        locked={false}
        onOpenModels={() => undefined}
      />,
    );

    const button = screen.getByRole("button", {
      name: /reasoning: opus.*acting: gpt-fast.*checking: haiku/i,
    });
    expect(button.textContent).toContain("Opus + 2 more");
    expect(button.getAttribute("title")).toContain("Checking: haiku");
  });
});
