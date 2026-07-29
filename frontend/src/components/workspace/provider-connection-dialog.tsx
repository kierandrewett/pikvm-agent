import { useMemo, useState, type FormEvent } from "react";
import { KeyRoundIcon, PlugZapIcon } from "lucide-react";
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
  ConnectableProviderKind,
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

const isConnectable = (
  kind: string,
): kind is ConnectableProviderKind => kind in SETUP;

type ProviderConnectionDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  catalog: ProviderCatalogEntry[];
  connecting?: boolean;
  onConnect: (
    input: ProviderConnectionInput,
  ) => Promise<ProviderConnectionResult>;
};

export function ProviderConnectionDialog({
  open,
  onOpenChange,
  catalog,
  connecting = false,
  onConnect,
}: ProviderConnectionDialogProps) {
  const adapters = useMemo(
    () => catalog.filter((entry) => isConnectable(entry.kind)),
    [catalog],
  );
  const initialKind = (adapters[0]?.kind ??
    "codex_cli") as ConnectableProviderKind;
  const initialAuth = SETUP[initialKind].auth?.[0];
  const [kind, setKind] = useState<ConnectableProviderKind>(initialKind);
  const [authMode, setAuthMode] =
    useState<ProviderConnectionAuthMode | null>(
      initialAuth?.value ?? null,
    );
  const [alias, setAlias] = useState("");
  const [model, setModel] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [credentialEnv, setCredentialEnv] = useState(
    initialAuth?.credentialEnv ??
      SETUP[initialKind].credentialEnv ??
      "",
  );
  const [profileHomeEnv, setProfileHomeEnv] = useState(
    SETUP[initialKind].profileHomeEnv ?? "",
  );
  const [error, setError] = useState("");
  const shape = SETUP[kind];
  const authChoice = shape.auth?.find(
    (choice) => choice.value === authMode,
  );
  const credentialEnvRequired = Boolean(
    authChoice?.credentialEnv || shape.credentialEnv,
  );

  const selectKind = (value: string | null) => {
    if (!value || !isConnectable(value)) return;
    const nextShape = SETUP[value];
    const nextAuth = nextShape.auth?.[0];
    setKind(value);
    setAuthMode(nextAuth?.value ?? null);
    setCredentialEnv(
      nextAuth?.credentialEnv ?? nextShape.credentialEnv ?? "",
    );
    setProfileHomeEnv(nextShape.profileHomeEnv ?? "");
    setBaseUrl("");
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
            Connect a provider to the harness. Existing provider aliases cannot
            be replaced here.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={(event) => void submit(event)}>
          <FieldGroup>
            <Field>
              <FieldLabel htmlFor="provider-adapter">
                Provider adapter
              </FieldLabel>
              <Select
                value={kind}
                onValueChange={selectKind}
                items={adapters.map((entry) => ({
                  value: entry.kind,
                  label: `${SETUP[entry.kind as ConnectableProviderKind].label} · ${entry.support_tier}`,
                }))}
              >
                <SelectTrigger
                  id="provider-adapter"
                  className="w-full"
                  aria-label="Provider adapter"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent alignItemWithTrigger={false}>
                  <SelectGroup>
                    {adapters.map((entry) => (
                      <SelectItem key={entry.kind} value={entry.kind}>
                        {SETUP[entry.kind as ConnectableProviderKind].label} ·{" "}
                        {entry.support_tier}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
            </Field>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field>
                <FieldLabel htmlFor="provider-alias">Provider name</FieldLabel>
                <Input
                  id="provider-alias"
                  value={alias}
                  onChange={(event) => setAlias(event.target.value)}
                  placeholder="work-openai"
                  autoComplete="off"
                  spellCheck={false}
                />
                <FieldDescription>Unique in this harness.</FieldDescription>
              </Field>
              <Field>
                <FieldLabel htmlFor="provider-model">Model ID</FieldLabel>
                <Input
                  id="provider-model"
                  value={model}
                  onChange={(event) => setModel(event.target.value)}
                  placeholder="Provider model ID"
                  autoComplete="off"
                  spellCheck={false}
                />
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
            <p className="flex items-start gap-2 rounded-lg bg-muted/50 p-3 text-xs leading-relaxed text-muted-foreground">
              <KeyRoundIcon
                className="mt-0.5 size-3.5 shrink-0"
                aria-hidden="true"
              />
              Credential values never enter this form. CLI providers keep their
              own login; API providers reference a server environment variable.
              Cloud CLI commands are fixed by the harness and cannot be edited
              here.
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
