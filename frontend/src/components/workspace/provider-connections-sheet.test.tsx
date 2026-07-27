// @vitest-environment jsdom

import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ProviderCatalogEntry, ProviderMap } from "@/types";
import { ProviderConnectionsSheet } from "./provider-connections-sheet";

const providers = {
  "claude-account": {
    kind: "claude_cli",
    configured_model: "opus",
    ready: true,
    support_tier: "stable",
    credential_owner: "provider_cli",
    routes: [
      { role: "reasoner", position: 1 },
      { role: "verifier", position: 1 },
    ],
    calls: 10,
    successes: 9,
    failures: 1,
    last_latency_ms: 12_400,
    conformance_status: "passed",
    conformance_calls_attempted: 3,
    conformance_exact: 3,
    conformance_median_latency_ms: 15_693,
    conformance_p95_latency_ms: 16_461,
    credential_source: "/home/operator/.claude",
    last_error: "raw provider failure with secret=do-not-render",
  },
  "fast-controller": {
    kind: "openai_responses",
    configured_model: "gpt-fast",
    ready: false,
    support_tier: "stable",
    credential_owner: "harness_environment",
    routes: [{ role: "controller", position: 1 }],
    calls: 0,
    successes: 0,
    failures: 0,
    readiness_error: "OPENAI_API_KEY is missing at /srv/harness/.env",
    conformance_status: "not-run",
  },
} as unknown as ProviderMap;

const catalog: ProviderCatalogEntry[] = [
  {
    kind: "claude_cli",
    support_tier: "stable",
    implementation_contract: "first_party",
    interface: "Claude print mode",
    pixel_input: "Isolated Read artifact",
    structured_output: "Strict JSON Schema",
    auth: [
      {
        mode: "saved_cli_login",
        credential_owner: "provider_cli",
      },
    ],
  },
  {
    kind: "openai_responses",
    support_tier: "stable",
    implementation_contract: "first_party",
    interface: "OpenAI Responses API",
    pixel_input: "Native image input",
    structured_output: "Strict JSON Schema",
    auth: [
      {
        mode: "api_key_env",
        credential_owner: "harness_environment",
      },
    ],
  },
];

afterEach(cleanup);

describe("ProviderConnectionsSheet", () => {
  it("shows the multi-model route and credential ownership without secrets", () => {
    render(
      <ProviderConnectionsSheet
        open
        onOpenChange={() => undefined}
        providers={providers}
        catalog={catalog}
        preferences={{}}
        locked={false}
        onPreferenceChange={() => undefined}
        onResetPreferences={() => undefined}
      />,
    );

    const route = screen.getByLabelText("Task route");
    expect(within(route).getByText("Reasoning")).not.toBeNull();
    expect(within(route).getByText("Acting")).not.toBeNull();
    expect(within(route).getByText("Checking")).not.toBeNull();
    expect(route.textContent).toContain("opus");
    expect(route.textContent).toContain("primary");
    expect(route.textContent).toContain("No ready provider configured");

    expect(screen.getByText("Provider-owned sign-in")).not.toBeNull();
    expect(screen.getAllByText("Harness environment").length).toBeGreaterThan(
      0,
    );
    expect(
      screen.getByText(
        "Conformance passed · 3/3 exact · median 15.7 s · p95 16.5 s",
      ),
    ).not.toBeNull();
    expect(screen.getByText("Conformance not run")).not.toBeNull();

    const body = document.body.textContent || "";
    expect(body).not.toContain("OPENAI_API_KEY");
    expect(body).not.toContain("/srv/harness/.env");
    expect(body).not.toContain("/home/operator/.claude");
    expect(body).not.toContain("do-not-render");
  });

  it("gives safe setup guidance and exposes the adapter catalog progressively", () => {
    render(
      <ProviderConnectionsSheet
        open
        onOpenChange={() => undefined}
        providers={providers}
        catalog={catalog}
        preferences={{}}
        locked={false}
        onPreferenceChange={() => undefined}
        onResetPreferences={() => undefined}
      />,
    );

    expect(screen.getByText("1/2 ready")).not.toBeNull();
    expect(screen.getAllByText("Setup needed").length).toBeGreaterThan(0);
    expect(
      screen.getByText(
        "Add the credential to the harness environment, then restart or refresh.",
      ),
    ).not.toBeNull();
    expect(screen.getByText("Available adapters")).not.toBeNull();
    expect(screen.getByText("2 supported")).not.toBeNull();
    expect(screen.getByText(/Claude print mode/)).not.toBeNull();
    expect(
      screen.getByText(/CLI sign-in · Provider-owned sign-in/),
    ).not.toBeNull();
    expect(screen.getByText(/API key · Harness environment/)).not.toBeNull();
  });

  it("edits each computer-use role independently", async () => {
    const user = userEvent.setup();
    const onPreferenceChange = vi.fn();
    const readyProviders = {
      ...providers,
      "fast-controller": {
        ...providers["fast-controller"],
        ready: true,
      },
    } as ProviderMap;

    render(
      <ProviderConnectionsSheet
        open
        onOpenChange={() => undefined}
        providers={readyProviders}
        catalog={catalog}
        preferences={{ reasoner: "claude-account" }}
        locked={false}
        onPreferenceChange={onPreferenceChange}
        onResetPreferences={() => undefined}
      />,
    );

    expect(screen.getByText("Custom primaries")).not.toBeNull();
    await user.click(screen.getByRole("combobox", { name: "Acting model" }));
    await user.click(
      screen.getByRole("option", {
        name: "gpt-fast · fast-controller",
      }),
    );

    expect(onPreferenceChange).toHaveBeenCalledWith(
      "controller",
      "fast-controller",
    );
  });

  it("warns when the accurate acting primary is not a fast-path model", () => {
    render(
      <ProviderConnectionsSheet
        open
        onOpenChange={() => undefined}
        providers={{
          ...providers,
          "claude-account": {
            ...providers["claude-account"],
            routes: [
              ...(providers["claude-account"].routes ?? []),
              { role: "controller", position: 1 },
            ],
          },
        }}
        catalog={catalog}
        preferences={{}}
        locked={false}
        onPreferenceChange={() => undefined}
        onResetPreferences={() => undefined}
      />,
    );

    expect(
      screen.getByText(
        "Acting path is accurate but slow: opus measured 15.7 s median. Use a sub-5 s API model as the primary controller.",
      ),
    ).not.toBeNull();
  });

  it("warns when a fast acting primary misses exact conformance", () => {
    render(
      <ProviderConnectionsSheet
        open
        onOpenChange={() => undefined}
        providers={{
          ...providers,
          "fast-controller": {
            ...providers["fast-controller"],
            ready: true,
            conformance_status: "degraded",
            conformance_calls_attempted: 5,
            conformance_exact: 4,
            conformance_median_latency_ms: 690,
          },
        }}
        catalog={catalog}
        preferences={{}}
        locked={false}
        onPreferenceChange={() => undefined}
        onResetPreferences={() => undefined}
      />,
    );

    expect(
      screen.getByText(
        "Acting path is not exact: gpt-fast scored 4/5 in blind-screen conformance. Keep it out of the primary route until it passes.",
      ),
    ).not.toBeNull();
  });

  it("shows and disables the durable route while a run is active", () => {
    render(
      <ProviderConnectionsSheet
        open
        onOpenChange={() => undefined}
        providers={{
          ...providers,
          "fast-controller": {
            ...providers["fast-controller"],
            ready: true,
          },
        }}
        catalog={catalog}
        preferences={{}}
        activeRoute={{
          reasoner: ["claude-account"],
          controller: ["fast-controller", "claude-account"],
          verifier: ["claude-account"],
        }}
        locked
        onPreferenceChange={() => undefined}
        onResetPreferences={() => undefined}
      />,
    );

    expect(screen.getByText("Locked for this run")).not.toBeNull();
    expect(
      screen.getByText(
        "This route was snapshotted when the task was sent. Start a new task to change it.",
      ),
    ).not.toBeNull();
    expect(
      screen
        .getByRole("combobox", { name: "Acting model" })
        .hasAttribute("disabled"),
    ).toBe(true);
    expect(screen.getByText(/fallback 1: opus/)).not.toBeNull();
  });

  it("adds a provider through a secret-reference-only form", async () => {
    const user = userEvent.setup();
    const onConnectProvider = vi.fn().mockResolvedValue({
      provider: "openai-work",
      configured_model: "gpt-5-mini",
      kind: "openai_responses",
      ready: true,
      credential_owner: "harness_environment",
      configured_not_routed: true,
      secret_received: false,
    });

    render(
      <ProviderConnectionsSheet
        open
        onOpenChange={() => undefined}
        providers={providers}
        catalog={[catalog[1]!, catalog[0]!]}
        preferences={{}}
        locked={false}
        onPreferenceChange={() => undefined}
        onResetPreferences={() => undefined}
        onConnectProvider={onConnectProvider}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Add model" }));
    expect(
      await screen.findByText(
        /Credential values never enter this form\./,
      ),
    ).not.toBeNull();
    expect(screen.queryByLabelText(/API key value/i)).toBeNull();

    expect(
      screen.getByRole("combobox", { name: "Provider adapter" }).textContent,
    ).toContain("OpenAI Responses API");
    await user.type(screen.getByLabelText("Provider name"), "openai-work");
    await user.type(screen.getByLabelText("Model ID"), "gpt-5-mini");
    expect(
      (screen.getByLabelText(
        "Credential environment variable",
      ) as HTMLInputElement).value,
    ).toBe("OPENAI_API_KEY");

    await user.click(
      screen.getByRole("button", { name: "Add model" }),
    );

    expect(onConnectProvider).toHaveBeenCalledWith({
      alias: "openai-work",
      kind: "openai_responses",
      model: "gpt-5-mini",
      credential_env: "OPENAI_API_KEY",
    });
  });
});
