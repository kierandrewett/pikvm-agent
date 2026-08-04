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

const noop = () => undefined;

afterEach(cleanup);

describe("ModelPicker", () => {
  it("defaults to Auto and applies a picked model to every stage", async () => {
    const user = userEvent.setup();
    const onPreferenceChange = vi.fn();
    render(
      <ModelPicker
        providers={providers}
        preferences={{}}
        locked={false}
        onPreferenceChange={onPreferenceChange}
        onResetPreferences={noop}
        onOpenModels={noop}
      />,
    );

    const trigger = screen.getByRole("combobox", { name: /Model: Auto/ });
    await user.click(trigger);
    await user.click(screen.getByRole("option", { name: "Opus" }));

    // One pick fans out to every stage — the sheet reads back this exact state.
    expect(onPreferenceChange).toHaveBeenCalledTimes(4);
    expect(onPreferenceChange).toHaveBeenCalledWith(
      "controller",
      "strong-reasoner",
    );
  });

  it("routes Configure models… to the full sheet without changing the pick", async () => {
    const user = userEvent.setup();
    const onOpenModels = vi.fn();
    const onPreferenceChange = vi.fn();
    render(
      <ModelPicker
        providers={providers}
        preferences={{}}
        locked={false}
        onPreferenceChange={onPreferenceChange}
        onResetPreferences={noop}
        onOpenModels={onOpenModels}
      />,
    );

    await user.click(screen.getByRole("combobox", { name: /Model: Auto/ }));
    await user.click(
      screen.getByRole("option", { name: /Configure models…/ }),
    );
    expect(onOpenModels).toHaveBeenCalledOnce();
    expect(onPreferenceChange).not.toHaveBeenCalled();
  });

  it("hands the choice back to the harness when Auto is picked", async () => {
    const user = userEvent.setup();
    const onResetPreferences = vi.fn();
    render(
      <ModelPicker
        providers={providers}
        preferences={{
          assistant: "strong-reasoner",
          reasoner: "strong-reasoner",
          controller: "strong-reasoner",
          verifier: "strong-reasoner",
        }}
        locked={false}
        onPreferenceChange={noop}
        onResetPreferences={onResetPreferences}
        onOpenModels={noop}
      />,
    );

    // A unified pick shows as the model's short name, not provider plumbing.
    const trigger = screen.getByRole("combobox", { name: /Model: Opus/ });
    await user.click(trigger);
    // Auto names the model it resolves to, so the option reads "Auto <model>".
    await user.click(screen.getByRole("option", { name: /^Auto\b/ }));
    expect(onResetPreferences).toHaveBeenCalledOnce();
  });

  it("says which model Auto actually runs", async () => {
    const user = userEvent.setup();
    render(
      <ModelPicker
        providers={providers}
        preferences={{}}
        locked={false}
        onPreferenceChange={noop}
        onResetPreferences={noop}
        onOpenModels={noop}
      />,
    );

    // "Auto" alone answered none of the question the control exists for, so
    // both the trigger and its option carry the resolved model.
    const trigger = screen.getByRole("combobox", { name: /^Model: Auto — .+/ });
    expect(trigger.textContent).toMatch(/^Auto./);

    await user.click(trigger);
    const auto = screen.getByRole("option", { name: /^Auto\s+\S/ });
    expect(auto.textContent).not.toBe("Auto");
  });

  it("shows Split when stages differ and locks during an active run", () => {
    const { rerender } = render(
      <ModelPicker
        providers={providers}
        preferences={{ controller: "fast-controller" }}
        locked={false}
        onPreferenceChange={noop}
        onResetPreferences={noop}
        onOpenModels={noop}
      />,
    );
    expect(
      screen.getByRole("combobox", { name: /Model: Split/ }),
    ).not.toBeNull();

    rerender(
      <ModelPicker
        providers={providers}
        preferences={{}}
        activeRoute={{ controller: ["fast-controller"] }}
        activeProvider="fast-controller"
        locked
        onPreferenceChange={noop}
        onResetPreferences={noop}
        onOpenModels={noop}
      />,
    );
    const locked = screen.getByRole("combobox", {
      name: /Model: gpt-fast \(locked for this run\)/,
    });
    expect(locked.hasAttribute("disabled")).toBe(true);
  });
});
