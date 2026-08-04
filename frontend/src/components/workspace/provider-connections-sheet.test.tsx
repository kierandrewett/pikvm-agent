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

const azureCatalog: ProviderCatalogEntry = {
  kind: "azure_openai_responses",
  support_tier: "beta",
  implementation_contract: "first_party",
  interface: "Azure OpenAI Responses API",
  pixel_input: "Native image input",
  structured_output: "Strict JSON Schema",
  auth: [
    {
      mode: "api_key_env",
      credential_owner: "harness_environment",
    },
    {
      mode: "bearer_command",
      credential_owner: "provider_cli",
    },
  ],
};

afterEach(cleanup);

describe("ProviderConnectionsSheet", () => {
  it("shows the stage split and sign-in ownership without secrets", async () => {
    const user = userEvent.setup();
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

    // The stage split is folded until asked for — most tasks never need it.
    await user.click(screen.getByRole("button", { name: "Split by stage" }));
    expect(screen.getByText("Reasoning")).not.toBeNull();
    expect(screen.getByText("Acting")).not.toBeNull();
    expect(screen.getByText("Checking")).not.toBeNull();
    expect(screen.getAllByText(/Runs on opus/).length).toBeGreaterThan(0);
    expect(screen.getByText("No ready model for this stage")).not.toBeNull();

    expect(
      screen.getByText(/Signed in with the provider's CLI/),
    ).not.toBeNull();
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

  it("names an account-default the same way the composer picker does", async () => {
    const user = userEvent.setup();
    render(
      <ProviderConnectionsSheet
        open
        onOpenChange={() => undefined}
        providers={
          {
            "codex-account": {
              kind: "codex_cli",
              configured_model: "account-default",
              ready: true,
              credential_owner: "provider_cli",
              routes: [{ role: "assistant", position: 1 }],
              calls: 0,
              successes: 0,
              failures: 0,
            },
          } as unknown as ProviderMap
        }
        catalog={catalog}
        preferences={{}}
        locked={false}
        onPreferenceChange={() => undefined}
        onResetPreferences={() => undefined}
      />,
    );

    // "account-default" named nothing, and the picker one click away called the
    // same account "Codex".
    expect(
      screen.getByRole("heading", { level: 4, name: "Codex" }),
    ).not.toBeNull();

    // The literal value is still reachable behind Details, since this sheet is
    // where you come to find out what is actually configured. (A closed
    // <details> hides its content visually, but jsdom still queries it, so this
    // asserts placement rather than the disclosure itself.)
    await user.click(screen.getByText("Details"));
    const details = screen.getByText("Configured model").closest("details");
    expect(details).not.toBeNull();
    expect(within(details!).getByText("account-default")).not.toBeNull();
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
    expect(screen.getByText("Providers you can connect")).not.toBeNull();
    expect(screen.getByText("2 supported")).not.toBeNull();
    expect(screen.getByText(/Sign in: CLI sign-in/)).not.toBeNull();
    expect(screen.getByText(/Sign in: API key/)).not.toBeNull();
  });

  it("applies one model to every stage from the single picker", async () => {
    const user = userEvent.setup();
    const onPreferenceChange = vi.fn();
    const onResetPreferences = vi.fn();
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
        preferences={{}}
        locked={false}
        onPreferenceChange={onPreferenceChange}
        onResetPreferences={onResetPreferences}
      />,
    );

    await user.click(
      screen.getByRole("combobox", { name: "Model for this task" }),
    );
    await user.click(
      screen.getByRole("option", { name: "gpt-fast · fast-controller" }),
    );

    // One pick fans out to every stage, so the simple and advanced views agree.
    expect(onPreferenceChange).toHaveBeenCalledTimes(4);
    for (const role of ["assistant", "reasoner", "controller", "verifier"]) {
      expect(onPreferenceChange).toHaveBeenCalledWith(
        role,
        "fast-controller",
      );
    }

  });

  it("keeps chat-only models out of computer roles", async () => {
    const user = userEvent.setup();
    const onPreferenceChange = vi.fn();
    const providersWithChatOnly = {
      ...providers,
      "spark-chat": {
        kind: "codex_app_server",
        configured_model: "gpt-5.3-codex-spark",
        ready: true,
        computer_screen_input: false,
        routes: [{ role: "assistant", position: 1 }],
      },
    } as ProviderMap;

    render(
      <ProviderConnectionsSheet
        open
        onOpenChange={() => undefined}
        providers={providersWithChatOnly}
        catalog={catalog}
        preferences={{}}
        locked={false}
        onPreferenceChange={onPreferenceChange}
        onResetPreferences={() => undefined}
      />,
    );

    expect(screen.getByText("Chat only")).not.toBeNull();

    await user.click(
      screen.getByRole("combobox", { name: "Model for this task" }),
    );
    expect(
      screen.queryByRole("option", {
        name: "gpt-5.3-codex-spark · spark-chat",
      }),
    ).toBeNull();
    await user.keyboard("{Escape}");

    await user.click(screen.getByRole("button", { name: "Split by stage" }));
    await user.click(screen.getByRole("combobox", { name: "Chat model" }));
    expect(
      screen.getByRole("option", {
        name: "gpt-5.3-codex-spark · spark-chat",
      }),
    ).not.toBeNull();
    await user.keyboard("{Escape}");

    await user.click(screen.getByRole("combobox", { name: "Acting model" }));
    expect(
      screen.queryByRole("option", {
        name: "gpt-5.3-codex-spark · spark-chat",
      }),
    ).toBeNull();
  });

  it("hands the choice back to the harness when Automatic is picked", async () => {
    const user = userEvent.setup();
    const onResetPreferences = vi.fn();
    render(
      <ProviderConnectionsSheet
        open
        onOpenChange={() => undefined}
        providers={providers}
        catalog={catalog}
        preferences={{ assistant: "claude-account" }}
        locked={false}
        onPreferenceChange={() => undefined}
        onResetPreferences={onResetPreferences}
      />,
    );

    await user.click(
      screen.getByRole("combobox", { name: "Model for this task" }),
    );
    await user.click(
      screen.getByRole("option", { name: /Automatic — routes each stage/ }),
    );
    expect(onResetPreferences).toHaveBeenCalled();
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

    // A single-stage preference means the split is live, so it opens itself.
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
        "opus is accurate but slow at acting (15.7 s per step). A faster model under Acting will feel much snappier.",
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
        "gpt-fast missed accuracy checks (4/5 exact), so its clicks can land in the wrong place. Pick a different model for Acting until it passes.",
      ),
    ).not.toBeNull();
  });

  it("shows and disables the durable route while a run is active", async () => {
    const user = userEvent.setup();
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
        "This task keeps the models it started with. Start a new task to change them.",
      ),
    ).not.toBeNull();
    expect(
      screen
        .getByRole("combobox", { name: "Model for this task" })
        .hasAttribute("disabled"),
    ).toBe(true);
    await user.click(screen.getByRole("button", { name: "Split by stage" }));
    expect(
      screen
        .getByRole("combobox", { name: "Acting model" })
        .hasAttribute("disabled"),
    ).toBe(true);
    expect(screen.getByText(/falls back to opus/)).not.toBeNull();
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
        /Credentials stay outside this form\./,
      ),
    ).not.toBeNull();
    expect(screen.queryByLabelText(/API key value/i)).toBeNull();

    expect(
      screen.getByRole("combobox", { name: "Provider" }).textContent,
    ).toContain("OpenAI API key");
    await user.type(screen.getByLabelText("Provider name"), "openai-work");
    const addDialog = within(screen.getByRole("dialog", { name: "Add a model" }));
    await user.type(addDialog.getByLabelText("Model"), "gpt-5-mini");
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

  it("connects Azure CLI OAuth without accepting an arbitrary command", async () => {
    const user = userEvent.setup();
    const onConnectProvider = vi.fn().mockResolvedValue({
      provider: "azure-work",
      configured_model: "controller-deployment",
      kind: "azure_openai_responses",
      ready: true,
      credential_owner: "provider_cli",
      configured_not_routed: true,
      secret_received: false,
    });

    render(
      <ProviderConnectionsSheet
        open
        onOpenChange={() => undefined}
        providers={providers}
        catalog={[azureCatalog]}
        preferences={{}}
        locked={false}
        onPreferenceChange={() => undefined}
        onResetPreferences={() => undefined}
        onConnectProvider={onConnectProvider}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Add model" }));
    expect(
      screen.getByRole("combobox", {
        name: "Provider authentication",
      }).textContent,
    ).toContain("Azure CLI OAuth");
    expect(
      screen.getByText(/fixed harness command/),
    ).not.toBeNull();
    expect(screen.queryByLabelText(/command/i)).toBeNull();
    expect(
      screen.queryByLabelText("Credential environment variable"),
    ).toBeNull();

    await user.type(screen.getByLabelText("Provider name"), "azure-work");
    await user.type(
      within(screen.getByRole("dialog", { name: "Add a model" })).getByLabelText(
        "Model",
      ),
      "controller-deployment",
    );
    await user.type(
      screen.getByLabelText("Base URL"),
      "https://resource.openai.azure.com/openai/v1",
    );
    await user.click(screen.getByRole("button", { name: "Add model" }));

    expect(onConnectProvider).toHaveBeenCalledWith({
      alias: "azure-work",
      kind: "azure_openai_responses",
      model: "controller-deployment",
      base_url: "https://resource.openai.azure.com/openai/v1",
      auth_mode: "bearer_command",
    });
  });
});

describe("adding a model from a stage", () => {
  it("offers Add a model… on each stage and routes the new account to it", async () => {
    const user = userEvent.setup();
    const onPreferenceChange = vi.fn();
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
        catalog={catalog}
        preferences={{}}
        locked={false}
        onPreferenceChange={onPreferenceChange}
        onResetPreferences={() => undefined}
        onConnectProvider={onConnectProvider}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Split by stage" }));
    await user.click(screen.getByRole("combobox", { name: "Acting model" }));
    // The option only renders when the sheet actually passes the hook down —
    // it was declared and consumed but never passed, so it never appeared.
    await user.click(screen.getByRole("option", { name: "Add a model…" }));

    const dialog = await screen.findByRole("dialog", { name: "Add a model" });
    expect(dialog).not.toBeNull();

    await user.type(
      within(dialog).getByLabelText("Provider name"),
      "openai-work",
    );
    await user.type(within(dialog).getByLabelText("Model"), "gpt-5-mini");
    await user.click(
      within(dialog).getByRole("button", { name: "Add model" }),
    );

    // Connecting from a stage row must leave that stage using the new account,
    // not drop the user back to pick it themselves.
    expect(onConnectProvider).toHaveBeenCalled();
    expect(onPreferenceChange).toHaveBeenCalledWith("controller", "openai-work");
  });
});
