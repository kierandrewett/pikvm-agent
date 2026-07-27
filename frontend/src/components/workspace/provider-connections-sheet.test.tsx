// @vitest-environment jsdom

import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
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
      />,
    );

    const route = screen.getByLabelText("Automatic route");
    expect(within(route).getByText("Reasoning")).not.toBeNull();
    expect(within(route).getByText("Acting")).not.toBeNull();
    expect(within(route).getByText("Checking")).not.toBeNull();
    expect(route.textContent).toContain("opus");
    expect(route.textContent).toContain("gpt-fast");
    expect(route.textContent).toContain("primary");

    expect(screen.getByText("Provider-owned sign-in")).not.toBeNull();
    expect(screen.getAllByText("Harness environment").length).toBeGreaterThan(
      0,
    );
    expect(screen.getByText("Conformance passed")).not.toBeNull();
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
});
