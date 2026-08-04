import { useMemo, useState, type FormEvent } from "react";
import { PlugZapIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Field,
  FieldDescription,
  FieldError,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import type {
  CatalogModel,
  ConnectableProviderKind,
  ModelCatalog,
  ProviderCatalogEntry,
  ProviderConnectionAuthMode,
  ProviderConnectionInput,
  ProviderConnectionResult,
} from "@/types";

type AuthChoice = {
  value: ProviderConnectionAuthMode;
  label: string;
  credentialEnv?: string;
};

type SetupShape = {
  label: string;
  credentialEnv?: string;
  profileHomeEnv?: string;
  baseUrl: "none" | "optional" | "required";
  auth?: AuthChoice[];
};

const SETUP: Record<ConnectableProviderKind, SetupShape> = {
  codex_app_server: {
    label: "Codex app-server",
    baseUrl: "none",
  },
  codex_cli: {
    label: "Codex CLI",
    baseUrl: "none",
  },
  claude_cli: {
    label: "Claude CLI",
    baseUrl: "none",
  },
  gemini_cli: {
    label: "Gemini CLI",
    profileHomeEnv: "PIKVM_GEMINI_CLI_HOME",
    baseUrl: "none",
  },
  openai_responses: {
    label: "OpenAI Responses API",
    credentialEnv: "OPENAI_API_KEY",
    baseUrl: "optional",
  },
  anthropic_api: {
    label: "Anthropic Messages API",
    credentialEnv: "ANTHROPIC_API_KEY",
    baseUrl: "optional",
  },
  gemini_api: {
    label: "Gemini API",
    credentialEnv: "GEMINI_API_KEY",
    baseUrl: "optional",
  },
  openai_compatible: {
    label: "OpenAI-compatible API",
    credentialEnv: "MODEL_GATEWAY_KEY",
    baseUrl: "required",
  },
  azure_openai_responses: {
    label: "Azure OpenAI Responses API",
    baseUrl: "required",
    auth: [
      {
        value: "bearer_command",
        label: "Azure CLI OAuth",
      },
      {
        value: "api_key",
        label: "API key environment",
        credentialEnv: "AZURE_OPENAI_API_KEY",
      },
      {
        value: "bearer_env",
        label: "Bearer token environment",
        credentialEnv: "AZURE_OPENAI_ACCESS_TOKEN",
      },
    ],
  },
  vertex_gemini: {
    label: "Vertex AI Gemini",
    baseUrl: "required",
    auth: [
      {
        value: "bearer_command",
        label: "Google Cloud CLI OAuth",
      },
      {
        value: "bearer_env",
        label: "Bearer token environment",
        credentialEnv: "VERTEX_ACCESS_TOKEN",
      },
    ],
  },
};

/** "500K ctx · $5/M in · $25/M out" — the facts that separate similar models. */
const modelDetail = (model: CatalogModel) => {
  const parts: string[] = [];
  if (typeof model.context === "number" && model.context > 0) {
    parts.push(
      model.context >= 1000
        ? `${Math.round(model.context / 1000)}K ctx`
        : `${model.context} ctx`,
    );
  }
  if (typeof model.cost_input === "number") {
    parts.push(`$${model.cost_input}/M in`);
  }
  if (typeof model.cost_output === "number") {
    parts.push(`$${model.cost_output}/M out`);
  }
  if (!model.image_input) parts.push("no image input");
  return parts.join(" · ");
};

/** A provider name the harness will accept, suggested from the picked model. */
const suggestAlias = (modelId: string) =>
  modelId
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48);

const CUSTOM_MODEL = "\u0000custom";

/**
 * What a user actually chooses between. The adapter KIND is an implementation
 * detail — "OpenRouter" and "a self-hosted vLLM" are both openai_compatible,
 * but only one of them should make you go and find a base URL. A preset
 * carries the kind plus everything about that service we already know, and
 * narrows the model list to the models it can actually serve.
 */
type ProviderPreset = {
  id: string;
  label: string;
  hint?: string;
  kind: ConnectableProviderKind;
  baseUrl?: string;
  credentialEnv?: string;
  /** models.dev provider ids to show; omit to use the kind's default set. */
  catalogProviderIds?: string[];
};

const PRESETS: ProviderPreset[] = [
  {
    id: "openrouter",
    label: "OpenRouter",
    hint: "One key, most models",
    kind: "openai_compatible",
    baseUrl: "https://openrouter.ai/api/v1",
    credentialEnv: "OPENROUTER_API_KEY",
    catalogProviderIds: ["openrouter"],
  },
  {
    id: "codex_cli",
    label: "ChatGPT / Codex subscription",
    hint: "Uses your Codex CLI login",
    kind: "codex_cli",
  },
  {
    id: "claude_cli",
    label: "Claude subscription",
    hint: "Uses your Claude CLI login",
    kind: "claude_cli",
  },
  {
    id: "gemini_cli",
    label: "Gemini subscription",
    hint: "Uses your Gemini CLI login",
    kind: "gemini_cli",
  },
  {
    id: "openai_responses",
    label: "OpenAI API key",
    kind: "openai_responses",
    catalogProviderIds: ["openai"],
  },
  {
    id: "anthropic_api",
    label: "Anthropic API key",
    kind: "anthropic_api",
  },
  {
    id: "gemini_api",
    label: "Google AI API key",
    kind: "gemini_api",
  },
  {
    id: "openai_compatible",
    label: "Other OpenAI-compatible API",
    hint: "Self-hosted or another gateway",
    kind: "openai_compatible",
  },
  {
    id: "azure_openai_responses",
    label: "Azure OpenAI",
    kind: "azure_openai_responses",
  },
  {
    id: "vertex_gemini",
    label: "Vertex AI",
    kind: "vertex_gemini",
  },
  {
    id: "codex_app_server",
    label: "Codex app-server",
    hint: "Faster repeated calls on the same login",
    kind: "codex_app_server",
  },
];

const isConnectable = (
  kind: string,
): kind is ConnectableProviderKind => kind in SETUP;

type ProviderConnectionDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  catalog: ProviderCatalogEntry[];
  modelCatalog?: ModelCatalog;
  connecting?: boolean;
  onConnect: (
    input: ProviderConnectionInput,
  ) => Promise<ProviderConnectionResult>;
};

export function ProviderConnectionDialog({
  open,
  onOpenChange,
  catalog,
  modelCatalog,
  connecting = false,
  onConnect,
}: ProviderConnectionDialogProps) {
  const adapters = useMemo(
    () => catalog.filter((entry) => isConnectable(entry.kind)),
    [catalog],
  );
  // Only offer presets whose adapter this harness actually ships.
  const available = useMemo(() => {
    // Ordered by the harness's own adapter list, so the default pick still
    // follows what the harness reports first rather than our preset ordering.
    const rank = new Map(adapters.map((entry, index) => [entry.kind, index]));
    return PRESETS.filter((preset) => rank.has(preset.kind)).sort(
      (left, right) =>
        (rank.get(left.kind) ?? 0) - (rank.get(right.kind) ?? 0),
    );
  }, [adapters]);
  const initialPreset =
    available[0] ??
    PRESETS.find((preset) => preset.kind === adapters[0]?.kind) ??
    PRESETS[0];
  const initialKind = initialPreset.kind;
  const initialAuth = SETUP[initialKind].auth?.[0];
  const [presetId, setPresetId] = useState(initialPreset.id);
  const [kind, setKind] = useState<ConnectableProviderKind>(initialKind);
  const [authMode, setAuthMode] =
    useState<ProviderConnectionAuthMode | null>(
      initialAuth?.value ?? null,
    );
  const [alias, setAlias] = useState("");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState(initialPreset.baseUrl ?? "");
  const [credentialEnv, setCredentialEnv] = useState(
    initialPreset.credentialEnv ??
      initialAuth?.credentialEnv ??
      SETUP[initialKind].credentialEnv ??
      "",
  );
  const [profileHomeEnv, setProfileHomeEnv] = useState(
    SETUP[initialKind].profileHomeEnv ?? "",
  );
  const [error, setError] = useState("");
  const [customModel, setCustomModel] = useState(false);
  const [aliasEdited, setAliasEdited] = useState(false);
  const preset =
    available.find((candidate) => candidate.id === presetId) ?? initialPreset;
  const shape = SETUP[kind];
  // Real models this kind of account can run, straight from the models.dev
  // cache. Multiple source providers can feed one kind (openai_compatible
  // draws from OpenAI and OpenRouter), so labels carry the source when needed.
  const catalogModels = useMemo(() => {
    if (!modelCatalog?.available) return [];
    const providerIds =
      preset.catalogProviderIds ?? modelCatalog.kinds[kind] ?? [];
    const seen = new Set<string>();
    const options: Array<{
      model: CatalogModel;
      providerName: string;
      multiSource: boolean;
    }> = [];
    for (const providerId of providerIds) {
      const provider = modelCatalog.providers[providerId];
      if (!provider) continue;
      for (const model of provider.models) {
        if (seen.has(model.id)) continue;
        seen.add(model.id);
        options.push({
          model,
          providerName: provider.name,
          multiSource: providerIds.length > 1,
        });
      }
    }
    return options;
  }, [modelCatalog, kind, preset]);
  const pickedCatalogModel = catalogModels.find(
    (option) => option.model.id === model,
  );
  const authChoice = shape.auth?.find(
    (choice) => choice.value === authMode,
  );
  const credentialEnvRequired = Boolean(
    authChoice?.credentialEnv || shape.credentialEnv,
  );

  const selectPreset = (value: string | null) => {
    const next = available.find((candidate) => candidate.id === value);
    if (!next || !isConnectable(next.kind)) return;
    const nextShape = SETUP[next.kind];
    const nextAuth = nextShape.auth?.[0];
    setPresetId(next.id);
    setKind(next.kind);
    setAuthMode(nextAuth?.value ?? null);
    setCredentialEnv(
      next.credentialEnv ??
        nextAuth?.credentialEnv ??
        nextShape.credentialEnv ??
        "",
    );
    setProfileHomeEnv(nextShape.profileHomeEnv ?? "");
    setBaseUrl(next.baseUrl ?? "");
    setModel("");
    setCustomModel(false);
    setError("");
  };

  const selectAuthMode = (value: string | null) => {
    const choice = shape.auth?.find(
      (candidate) => candidate.value === value,
    );
    if (!choice) return;
    setAuthMode(choice.value);
    setCredentialEnv(choice.credentialEnv ?? "");
    setError("");
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError("");
    const input: ProviderConnectionInput = {
      alias: alias.trim(),
      kind,
      model: model.trim(),
    };
    if (shape.baseUrl !== "none" && baseUrl.trim()) {
      input.base_url = baseUrl.trim();
    }
    if (authChoice) {
      input.auth_mode = authChoice.value;
    }
    if (credentialEnvRequired) {
      input.credential_env = credentialEnv.trim();
    }
    if (shape.profileHomeEnv) {
      input.profile_home_env = profileHomeEnv.trim();
    }
    try {
      await onConnect(input);
      onOpenChange(false);
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "Could not connect the model.",
      );
    }
  };

  const valid =
    alias.trim() &&
    model.trim() &&
    (!shape.auth || authChoice) &&
    (!credentialEnvRequired || credentialEnv.trim()) &&
    (shape.baseUrl !== "required" || baseUrl.trim()) &&
    (!shape.profileHomeEnv || profileHomeEnv.trim());

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <div className="mb-1 flex size-9 items-center justify-center rounded-lg bg-muted">
            <PlugZapIcon aria-hidden="true" />
          </div>
          <DialogTitle>Add a model</DialogTitle>
          <DialogDescription>
            Connect a provider. Existing names cannot be replaced.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={(event) => void submit(event)}>
          <FieldGroup>
            <Field>
              <FieldLabel htmlFor="provider-adapter">Provider</FieldLabel>
              <Select
                value={preset.id}
                onValueChange={selectPreset}
                items={available.map((entry) => ({
                  value: entry.id,
                  label: entry.label,
                }))}
              >
                <SelectTrigger
                  id="provider-adapter"
                  className="w-full"
                  aria-label="Provider"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent alignItemWithTrigger={false}>
                  <SelectGroup>
                    {available.map((entry) => (
                      <SelectItem key={entry.id} value={entry.id}>
                        {entry.label}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
              {preset.hint ? (
                <FieldDescription>{preset.hint}</FieldDescription>
              ) : null}
            </Field>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field>
                <FieldLabel htmlFor="provider-alias">Provider name</FieldLabel>
                <Input
                  id="provider-alias"
                  value={alias}
                  onChange={(event) => {
                    setAlias(event.target.value);
                    setAliasEdited(event.target.value.trim().length > 0);
                  }}
                  placeholder="work-openai"
                  autoComplete="off"
                  spellCheck={false}
                />
                <FieldDescription>Unique in this harness.</FieldDescription>
              </Field>
              <Field>
                <FieldLabel htmlFor="provider-model">Model</FieldLabel>
                {catalogModels.length > 0 && !customModel ? (
                  <>
                    <Select
                      value={pickedCatalogModel ? model : null}
                      onValueChange={(value) => {
                        if (!value) return;
                        if (value === CUSTOM_MODEL) {
                          setCustomModel(true);
                          setModel("");
                          return;
                        }
                        setModel(value);
                        if (!aliasEdited) setAlias(suggestAlias(value));
                      }}
                      items={[
                        ...catalogModels.map((option) => ({
                          value: option.model.id,
                          label: option.multiSource
                            ? `${option.model.name} · ${option.providerName}`
                            : option.model.name,
                        })),
                        { value: CUSTOM_MODEL, label: "Custom model ID…" },
                      ]}
                    >
                      <SelectTrigger
                        id="provider-model"
                        className="w-full"
                        aria-label="Model"
                      >
                        <SelectValue placeholder="Choose a model" />
                      </SelectTrigger>
                      <SelectContent alignItemWithTrigger={false}>
                        <SelectGroup>
                          {catalogModels.map((option) => (
                            <SelectItem
                              key={option.model.id}
                              value={option.model.id}
                            >
                              {option.multiSource
                                ? `${option.model.name} · ${option.providerName}`
                                : option.model.name}
                            </SelectItem>
                          ))}
                          <SelectItem value={CUSTOM_MODEL}>
                            Custom model ID…
                          </SelectItem>
                        </SelectGroup>
                      </SelectContent>
                    </Select>
                    {pickedCatalogModel ? (
                      <FieldDescription>
                        {modelDetail(pickedCatalogModel.model) ||
                          pickedCatalogModel.model.id}
                      </FieldDescription>
                    ) : null}
                  </>
                ) : (
                  <>
                    <Input
                      id="provider-model"
                      value={model}
                      onChange={(event) => {
                        setModel(event.target.value);
                        if (!aliasEdited && event.target.value) {
                          setAlias(suggestAlias(event.target.value));
                        }
                      }}
                      placeholder="Provider model ID"
                      autoComplete="off"
                      spellCheck={false}
                    />
                    {catalogModels.length > 0 ? (
                      <FieldDescription>
                        <button
                          type="button"
                          className="underline underline-offset-2"
                          onClick={() => setCustomModel(false)}
                        >
                          Back to the model list
                        </button>
                      </FieldDescription>
                    ) : null}
                  </>
                )}
              </Field>
            </div>
            {shape.baseUrl !== "none" ? (
              <Field>
                <FieldLabel htmlFor="provider-base-url">
                  Base URL{shape.baseUrl === "optional" ? " (optional)" : ""}
                </FieldLabel>
                <Input
                  id="provider-base-url"
                  type="url"
                  value={baseUrl}
                  onChange={(event) => setBaseUrl(event.target.value)}
                  placeholder="https://api.example.com/v1"
                  autoComplete="off"
                  spellCheck={false}
                />
              </Field>
            ) : null}
            {shape.auth ? (
              <Field>
                <FieldLabel htmlFor="provider-auth-mode">
                  Authentication
                </FieldLabel>
                <Select
                  value={authMode}
                  onValueChange={selectAuthMode}
                  items={shape.auth.map((choice) => ({
                    value: choice.value,
                    label: choice.label,
                  }))}
                >
                  <SelectTrigger
                    id="provider-auth-mode"
                    className="w-full"
                    aria-label="Provider authentication"
                  >
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent alignItemWithTrigger={false}>
                    <SelectGroup>
                      {shape.auth.map((choice) => (
                        <SelectItem
                          key={choice.value}
                          value={choice.value}
                        >
                          {choice.label}
                        </SelectItem>
                      ))}
                    </SelectGroup>
                  </SelectContent>
                </Select>
                {authChoice?.value === "bearer_command" ? (
                  <FieldDescription>
                    Uses the provider CLI&apos;s existing login through a fixed
                    harness command.
                  </FieldDescription>
                ) : null}
              </Field>
            ) : null}
            {credentialEnvRequired ? (
              <Field>
                <FieldLabel htmlFor="provider-credential-env">
                  Credential environment variable
                </FieldLabel>
                <Input
                  id="provider-credential-env"
                  value={credentialEnv}
                  onChange={(event) => setCredentialEnv(event.target.value)}
                  autoComplete="off"
                  spellCheck={false}
                />
              </Field>
            ) : null}
            {shape.profileHomeEnv ? (
              <Field>
                <FieldLabel htmlFor="provider-profile-env">
                  Dedicated profile environment variable
                </FieldLabel>
                <Input
                  id="provider-profile-env"
                  value={profileHomeEnv}
                  onChange={(event) => setProfileHomeEnv(event.target.value)}
                  autoComplete="off"
                  spellCheck={false}
                />
              </Field>
            ) : null}
            <p className="rounded-lg bg-muted/50 p-3 text-xs leading-relaxed text-muted-foreground">
              Credentials stay outside this form. CLIs keep their login; APIs
              reference environment variables. Cloud login commands are fixed.
            </p>
            <FieldError>{error}</FieldError>
            <Button type="submit" disabled={connecting || !valid}>
              {connecting ? <Spinner data-icon="inline-start" /> : null}
              Add model
            </Button>
          </FieldGroup>
        </form>
      </DialogContent>
    </Dialog>
  );
}
