import {
  BotIcon,
  CheckCircle2Icon,
  ChevronRightIcon,
  GaugeIcon,
  LockKeyholeIcon,
  PlusIcon,
  RotateCcwIcon,
  TriangleAlertIcon,
} from "lucide-react";
import { lazy, Suspense, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  defaultRoleRoute,
  effectiveRoleRoute,
  MODEL_ROLES,
  providerModelLabel,
} from "@/lib/model-routes";
import type {
  ModelPreferences,
  ModelRole,
  ProviderCatalogEntry,
  ProviderConnectionInput,
  ProviderConnectionResult,
  ProviderHealth,
  ProviderMap,
  RunModelRoute,
} from "@/types";

const ProviderConnectionDialog = lazy(() =>
  import("@/components/workspace/provider-connection-dialog").then(
    (module) => ({ default: module.ProviderConnectionDialog }),
  ),
);

type ProviderConnectionsSheetProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  providers: ProviderMap;
  catalog: ProviderCatalogEntry[];
  preferences: ModelPreferences;
  activeRoute?: RunModelRoute | null;
  activeProvider?: string | null;
  locked: boolean;
  onPreferenceChange: (role: ModelRole, provider: string) => void;
  onResetPreferences: () => void;
  connectingProvider?: boolean;
  onConnectProvider?: (
    input: ProviderConnectionInput,
  ) => Promise<ProviderConnectionResult>;
};

const titleCase = (value: string) =>
  value
    .replaceAll("_", " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());

const latencyLabel = (milliseconds: number | null | undefined) => {
  if (milliseconds == null) return "no latency yet";
  if (milliseconds < 1_000) return `${milliseconds} ms`;
  const seconds = milliseconds / 1_000;
  return `${seconds.toFixed(seconds < 100 ? 1 : 0)} s`;
};

const FAST_ACTION_PATH_MEDIAN_MS = 5_000;

/* Plain sentences for the two facts a user needs about sign-in: how this account
 * authenticates, and what to do when it is not ready. Secrets and paths never
 * appear here — readiness_error stays server-side by design. */
const signInLabel = (owner: string | undefined) => {
  if (owner === "provider_cli") return "Signed in with the provider's CLI";
  if (owner === "harness_environment") return "API key on the harness host";
  if (owner === "external_bridge") return "External bridge";
  return "Local harness setup";
};

const setupGuidance = (owner: string | undefined) => {
  if (owner === "provider_cli") {
    return "Sign in with the provider CLI on this harness host, then refresh.";
  }
  if (owner === "harness_environment") {
    return "Add the credential to the harness environment, then restart or refresh.";
  }
  if (owner === "external_bridge") {
    return "Configure the external bridge, then refresh.";
  }
  return "Complete this provider's local harness setup, then refresh.";
};

const authModeLabel = (mode: string) => {
  if (mode === "saved_cli_login") return "CLI sign-in";
  if (mode === "api_key_env") return "API key";
  if (mode === "bearer_env") return "Bearer credential";
  if (mode === "bearer_command") return "Provider CLI credential";
  if (mode === "external_or_none") return "Bridge-defined";
  return titleCase(mode);
};

const conformanceLabel = (status: string | undefined) => {
  if (status === "passed") return "Conformance passed";
  if (status === "degraded") return "Conformance degraded";
  if (status === "failed") return "Conformance failed";
  if (status === "invalid-report") return "Conformance report invalid";
  return "Conformance not run";
};

const conformanceEvidence = (health: ProviderHealth) =>
  [
    conformanceLabel(health.conformance_status),
    health.conformance_calls_attempted != null &&
    health.conformance_exact != null
      ? `${health.conformance_exact}/${health.conformance_calls_attempted} exact`
      : "",
    health.conformance_median_latency_ms != null
      ? `median ${latencyLabel(health.conformance_median_latency_ms)}`
      : "",
    health.conformance_p95_latency_ms != null
      ? `p95 ${latencyLabel(health.conformance_p95_latency_ms)}`
      : "",
  ]
    .filter(Boolean)
    .join(" · ");

/** Which single answer describes the current preferences: the harness picks
 *  ("auto"), one provider does everything (its name), or stages are split. */
const unifiedSelection = (preferences: ModelPreferences) => {
  const values = MODEL_ROLES.map((role) => preferences[role.key]);
  if (values.every((value) => !value)) return "auto";
  const [first] = values;
  if (first && values.every((value) => value === first)) return first;
  return "split";
};

/**
 * The one control most people need: pick a model, it runs every stage.
 * "Automatic" hands the choice back to the harness route. The stage-by-stage
 * split lives in the advanced section below and shows up here only as a
 * read-only "Split by stage" state.
 */
function ModelChoice({
  providers,
  preferences,
  activeRoute,
  activeProvider,
  locked,
  onPreferenceChange,
  onResetPreferences,
}: Pick<
  ProviderConnectionsSheetProps,
  | "providers"
  | "preferences"
  | "activeRoute"
  | "activeProvider"
  | "locked"
  | "onPreferenceChange"
  | "onResetPreferences"
>) {
  const readyProviders = Object.entries(providers).filter(
    ([, health]) => health.ready !== false,
  );
  const selection = locked
    ? activeProvider ||
      effectiveRoleRoute({
        providers,
        preferences,
        activeRoute,
        activeProvider,
        locked,
        role: "assistant",
      })[0] ||
      "auto"
    : unifiedSelection(preferences);

  // What "Automatic" actually resolves to right now, so the default option
  // answers the question instead of hiding it.
  const autoPrimary = defaultRoleRoute(providers, "assistant")[0];
  const autoLabel = autoPrimary
    ? `Automatic — harness picks (now ${providerModelLabel(autoPrimary, providers[autoPrimary])})`
    : "Automatic — harness picks";

  const items = [
    { value: "auto", label: autoLabel },
    ...readyProviders.map(([name, health]) => ({
      value: name,
      label: `${providerModelLabel(name, health)} · ${name}`,
    })),
    ...(selection === "split"
      ? [{ value: "split", label: "Split by stage (set below)" }]
      : []),
  ];

  const choose = (next: string | null) => {
    if (!next || next === "split") return;
    if (next === "auto") {
      onResetPreferences();
      return;
    }
    // One model for everything: point every stage at it. The advanced section
    // reads back exactly this state, so the two views can never disagree.
    for (const role of MODEL_ROLES) onPreferenceChange(role.key, next);
  };

  return (
    <section aria-labelledby="model-choice-title">
      <div className="flex flex-wrap items-center gap-2">
        <h3 id="model-choice-title" className="text-sm font-semibold">
          Model
        </h3>
        {locked ? (
          <Badge variant="outline">
            <LockKeyholeIcon data-icon="inline-start" aria-hidden="true" />
            Locked for this run
          </Badge>
        ) : null}
      </div>
      <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
        {locked
          ? "This task keeps the models it started with. Start a new task to change them."
          : "Runs the whole task. Applies when you send the next task."}
      </p>
      <div className="mt-3">
        <Select
          items={items}
          value={selection}
          onValueChange={choose}
          disabled={locked}
        >
          <SelectTrigger
            size="sm"
            aria-label="Model for this task"
            className="w-full"
          >
            <SelectValue placeholder="Choose a model" />
          </SelectTrigger>
          <SelectContent alignItemWithTrigger={false} align="end">
            <SelectGroup>
              {items.map((item) => (
                <SelectItem
                  key={item.value}
                  value={item.value}
                  disabled={item.value === "split"}
                >
                  {item.label}
                </SelectItem>
              ))}
            </SelectGroup>
          </SelectContent>
        </Select>
        {readyProviders.length === 0 ? (
          <p className="mt-2 text-xs leading-relaxed text-caution-foreground">
            No model is ready yet. Fix an account below or add one.
          </p>
        ) : null}
      </div>
    </section>
  );
}

/** The stage-by-stage split, folded away because most tasks never need it. */
function StageSplit({
  providers,
  preferences,
  activeRoute,
  activeProvider,
  locked,
  onPreferenceChange,
  onResetPreferences,
}: Pick<
  ProviderConnectionsSheetProps,
  | "providers"
  | "preferences"
  | "activeRoute"
  | "activeProvider"
  | "locked"
  | "onPreferenceChange"
  | "onResetPreferences"
>) {
  const split = unifiedSelection(preferences) === "split";
  const [expanded, setExpanded] = useState(split);
  const open = expanded || split;
  const readyProviders = Object.entries(providers).filter(
    ([, health]) => health.ready !== false,
  );
  const customized = Object.keys(preferences).length > 0;

  return (
    <Collapsible open={open} onOpenChange={setExpanded}>
      <div className="flex items-center gap-2">
        <CollapsibleTrigger
          className="group/split flex items-center gap-1.5 text-sm font-semibold outline-none focus-visible:ring-2 focus-visible:ring-ring/60"
          aria-label="Split by stage"
        >
          <ChevronRightIcon
            className="size-4 text-muted-foreground transition-transform group-data-[panel-open]/split:rotate-90"
            aria-hidden="true"
          />
          Split by stage
        </CollapsibleTrigger>
        <span className="text-xs text-muted-foreground">advanced</span>
        {!locked && customized ? (
          <Button
            type="button"
            size="xs"
            variant="ghost"
            className="ml-auto"
            onClick={onResetPreferences}
          >
            <RotateCcwIcon data-icon="inline-start" aria-hidden="true" />
            Back to automatic
          </Button>
        ) : null}
      </div>
      <CollapsibleContent>
        <p className="mt-2 max-w-lg text-xs leading-relaxed text-muted-foreground">
          A task runs in stages. Each can use a different model — a fast one
          for acting on the computer, a stronger one for planning.
        </p>
        <FieldGroup className="mt-3 gap-0 border-y border-border/70">
          {MODEL_ROLES.map((role) => {
            const routed = effectiveRoleRoute({
              providers,
              preferences,
              activeRoute,
              activeProvider,
              locked,
              role: role.key,
            });
            const selected = locked
              ? routed[0] || "auto"
              : preferences[role.key] || "auto";
            const items = [
              { value: "auto", label: "Automatic" },
              ...readyProviders.map(([name, health]) => ({
                value: name,
                label: `${providerModelLabel(name, health)} · ${name}`,
              })),
            ];
            const primary = routed[0];
            const fallbacks = routed.slice(1);
            return (
              <Field
                key={role.key}
                orientation="responsive"
                data-disabled={locked || undefined}
                className="border-b border-border/60 py-3 last:border-b-0"
              >
                <div className="w-full @md/field-group:w-28">
                  <FieldLabel
                    htmlFor={`model-route-${role.key}`}
                    className="text-xs"
                  >
                    {role.label}
                  </FieldLabel>
                  <FieldDescription className="mt-0.5 text-[11px]">
                    {role.description}
                  </FieldDescription>
                </div>
                <div className="flex min-w-0 flex-1 flex-col gap-1.5">
                  <Select
                    items={items}
                    value={selected}
                    onValueChange={(next) =>
                      onPreferenceChange(
                        role.key,
                        !next || next === "auto" ? "" : next,
                      )
                    }
                    disabled={locked}
                  >
                    <SelectTrigger
                      id={`model-route-${role.key}`}
                      size="sm"
                      aria-label={`${role.label} model`}
                      className="w-full"
                    >
                      <SelectValue placeholder="Choose model" />
                    </SelectTrigger>
                    <SelectContent alignItemWithTrigger={false} align="end">
                      <SelectGroup>
                        {items.map((item) => (
                          <SelectItem key={item.value} value={item.value}>
                            {item.label}
                          </SelectItem>
                        ))}
                      </SelectGroup>
                    </SelectContent>
                  </Select>
                  <p className="truncate text-[11px] text-muted-foreground">
                    {primary
                      ? `Runs on ${providerModelLabel(primary, providers[primary])}${
                          fallbacks.length
                            ? `, falls back to ${fallbacks
                                .map((name) =>
                                  providerModelLabel(name, providers[name]),
                                )
                                .join(", ")}`
                            : ""
                        }`
                      : "No ready model for this stage"}
                  </p>
                </div>
              </Field>
            );
          })}
        </FieldGroup>
      </CollapsibleContent>
    </Collapsible>
  );
}

/** The one warning worth interrupting with: the model that clicks and types is
 *  measured inaccurate or slow. Rendered outside the advanced fold so it is
 *  seen even by people who never open it. */
function ActingPathWarning({
  providers,
  preferences,
  activeRoute,
  activeProvider,
  locked,
}: Pick<
  ProviderConnectionsSheetProps,
  "providers" | "preferences" | "activeRoute" | "activeProvider" | "locked"
>) {
  const actingPrimary = effectiveRoleRoute({
    providers,
    preferences,
    activeRoute,
    activeProvider,
    locked,
    role: "controller",
  })[0];
  const actingHealth = actingPrimary ? providers[actingPrimary] : undefined;
  const actingMeasured =
    actingHealth?.conformance_calls_attempted != null &&
    actingHealth.conformance_calls_attempted >= 3 &&
    actingHealth.conformance_exact != null;
  const actingAccurate =
    actingMeasured &&
    actingHealth?.conformance_status === "passed" &&
    actingHealth.conformance_exact === actingHealth.conformance_calls_attempted;
  const inexact = actingMeasured && !actingAccurate;
  const slow =
    actingAccurate &&
    actingHealth?.conformance_median_latency_ms != null &&
    actingHealth.conformance_median_latency_ms > FAST_ACTION_PATH_MEDIAN_MS;

  if (!(inexact || slow) || !actingPrimary || !actingHealth) return null;
  return (
    <p className="flex items-start gap-2 text-[11px] leading-relaxed text-caution-foreground">
      <GaugeIcon className="mt-0.5 size-3.5 shrink-0" aria-hidden="true" />
      {inexact ? (
        <span>
          {providerModelLabel(actingPrimary, actingHealth)} missed accuracy
          checks ({actingHealth.conformance_exact}/
          {actingHealth.conformance_calls_attempted} exact), so its clicks can
          land in the wrong place. Pick a different model for Acting until it
          passes.
        </span>
      ) : (
        <span>
          {providerModelLabel(actingPrimary, actingHealth)} is accurate but
          slow at acting (
          {latencyLabel(actingHealth.conformance_median_latency_ms)} per step).
          A faster model under Acting will feel much snappier.
        </span>
      )}
    </p>
  );
}

function ProviderRow({
  name,
  health,
}: {
  name: string;
  health: ProviderHealth;
}) {
  const ready = health.ready !== false;
  const coolingDown = Boolean(health.cooldown_until);
  const StatusIcon = ready ? CheckCircle2Icon : TriangleAlertIcon;
  const activity =
    (health.calls ?? 0) > 0
      ? `${health.successes ?? 0}/${health.calls} calls ok · ${latencyLabel(health.last_latency_ms)} last`
      : "Not used yet";

  return (
    <article className="border-t border-border/70 py-4 first:border-t-0">
      <div className="flex items-start gap-3">
        <StatusIcon
          className={
            ready
              ? "mt-0.5 size-4 shrink-0 text-emerald-300"
              : "mt-0.5 size-4 shrink-0 text-amber-300"
          }
          aria-hidden="true"
        />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h4 className="truncate text-sm font-semibold">
              {health.configured_model || name}
            </h4>
            <Badge variant={ready ? "secondary" : "outline"}>
              {coolingDown ? "Cooling down" : ready ? "Ready" : "Setup needed"}
            </Badge>
          </div>
          <p className="mt-1 truncate text-xs text-muted-foreground">
            {name} · {signInLabel(health.credential_owner)}
          </p>
          {ready ? (
            <p className="mt-1 text-xs text-muted-foreground">{activity}</p>
          ) : (
            <p className="mt-2 border-l-2 border-amber-400/60 pl-3 text-xs leading-relaxed text-amber-100/80">
              {setupGuidance(health.credential_owner)}
            </p>
          )}
          <details className="group/evidence mt-2">
            <summary className="cursor-pointer list-none text-[11px] font-medium text-muted-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring/60">
              <ChevronRightIcon
                className="mr-1 inline size-3 transition-transform group-open/evidence:rotate-90"
                aria-hidden="true"
              />
              Details
            </summary>
            <dl className="mt-2 grid gap-x-4 gap-y-2 text-[11px] sm:grid-cols-2">
              <div>
                <dt className="text-muted-foreground">Kind</dt>
                <dd className="font-medium">
                  {titleCase(health.kind || "provider")}
                  {health.support_tier
                    ? ` · ${titleCase(health.support_tier)}`
                    : ""}
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">Accuracy evidence</dt>
                <dd className="font-medium">{conformanceEvidence(health)}</dd>
              </div>
            </dl>
          </details>
        </div>
      </div>
    </article>
  );
}

function ConnectableProviders({
  catalog,
  providers,
}: {
  catalog: ProviderCatalogEntry[];
  providers: ProviderMap;
}) {
  const configuredKinds = new Set(
    Object.values(providers)
      .map((health) => health.kind)
      .filter(Boolean),
  );

  return (
    <details className="group/adapters border-t border-border/70 pt-4">
      <summary className="flex cursor-pointer list-none items-center gap-2 text-sm font-semibold outline-none focus-visible:ring-2 focus-visible:ring-ring/60">
        <ChevronRightIcon
          className="size-4 text-muted-foreground transition-transform group-open/adapters:rotate-90"
          aria-hidden="true"
        />
        Providers you can connect
        <span className="ml-auto text-xs font-normal text-muted-foreground">
          {catalog.length} supported
        </span>
      </summary>
      <div className="mt-3 divide-y divide-border/60">
        {catalog.map((entry) => (
          <div key={entry.kind} className="py-3">
            <div className="flex flex-wrap items-center gap-2">
              <p className="text-xs font-medium">{titleCase(entry.kind)}</p>
              <Badge variant="outline">{titleCase(entry.support_tier)}</Badge>
              {configuredKinds.has(entry.kind) ? (
                <Badge variant="secondary">Connected</Badge>
              ) : null}
            </div>
            <p className="mt-1 text-[11px] text-muted-foreground">
              Sign in:{" "}
              {entry.auth.map((auth) => authModeLabel(auth.mode)).join(" or ")}
            </p>
          </div>
        ))}
      </div>
    </details>
  );
}

export function ProviderConnectionsSheet({
  open,
  onOpenChange,
  providers,
  catalog,
  preferences,
  activeRoute,
  activeProvider,
  locked,
  onPreferenceChange,
  onResetPreferences,
  connectingProvider = false,
  onConnectProvider,
}: ProviderConnectionsSheetProps) {
  const [connectionOpen, setConnectionOpen] = useState(false);
  const entries = Object.entries(providers);
  const readyCount = entries.filter(
    ([, health]) => health.ready !== false,
  ).length;

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" className="w-full sm:max-w-xl">
        <SheetHeader className="border-b border-border/70">
          <SheetTitle className="flex items-center gap-2">
            <BotIcon aria-hidden="true" />
            Models
          </SheetTitle>
          <SheetDescription>
            Pick what runs your task. Keys and sign-ins stay on the harness
            host — secrets never enter this page.
          </SheetDescription>
          <div className="flex flex-wrap gap-2 pt-2">
            <Badge variant="secondary">
              {readyCount}/{entries.length} ready
            </Badge>
            {onConnectProvider ? (
              <Button
                type="button"
                size="xs"
                variant="outline"
                onClick={() => setConnectionOpen(true)}
              >
                <PlusIcon data-icon="inline-start" aria-hidden="true" />
                Add model
              </Button>
            ) : null}
          </div>
        </SheetHeader>
        <ScrollArea className="min-h-0 flex-1 px-4 pb-4">
          <div className="flex flex-col gap-6">
            <ModelChoice
              providers={providers}
              preferences={preferences}
              activeRoute={activeRoute}
              activeProvider={activeProvider}
              locked={locked}
              onPreferenceChange={onPreferenceChange}
              onResetPreferences={onResetPreferences}
            />
            <ActingPathWarning
              providers={providers}
              preferences={preferences}
              activeRoute={activeRoute}
              activeProvider={activeProvider}
              locked={locked}
            />
            <StageSplit
              providers={providers}
              preferences={preferences}
              activeRoute={activeRoute}
              activeProvider={activeProvider}
              locked={locked}
              onPreferenceChange={onPreferenceChange}
              onResetPreferences={onResetPreferences}
            />
            <section aria-labelledby="configured-accounts-title">
              <h3
                id="configured-accounts-title"
                className="text-sm font-semibold"
              >
                Accounts
              </h3>
              <div className="mt-2">
                {entries.length ? (
                  entries.map(([name, health]) => (
                    <ProviderRow key={name} name={name} health={health} />
                  ))
                ) : (
                  <p className="border-t border-border/70 py-6 text-sm text-muted-foreground">
                    No model accounts yet. Add one to get started.
                  </p>
                )}
              </div>
            </section>
            <ConnectableProviders catalog={catalog} providers={providers} />
          </div>
        </ScrollArea>
        {onConnectProvider && connectionOpen ? (
          <Suspense fallback={null}>
            <ProviderConnectionDialog
              open
              onOpenChange={setConnectionOpen}
              catalog={catalog}
              connecting={connectingProvider}
              onConnect={onConnectProvider}
            />
          </Suspense>
        ) : null}
      </SheetContent>
    </Sheet>
  );
}
