import {
  BotIcon,
  CheckCircle2Icon,
  CircleDashedIcon,
  NetworkIcon,
  ShieldCheckIcon,
  TriangleAlertIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import type {
  ProviderCatalogEntry,
  ProviderHealth,
  ProviderMap,
} from "@/types";

type ProviderConnectionsSheetProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  providers: ProviderMap;
  catalog: ProviderCatalogEntry[];
};

const ROUTE_ROLES = [
  { key: "reasoner", label: "Reasoning", description: "Plans the task" },
  { key: "controller", label: "Acting", description: "Chooses computer input" },
  { key: "verifier", label: "Checking", description: "Verifies the screen" },
] as const;

const titleCase = (value: string) =>
  value
    .replaceAll("_", " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());

const latencyLabel = (milliseconds: number | null | undefined) => {
  if (milliseconds == null) return "No latency yet";
  if (milliseconds < 1_000) return `${milliseconds} ms`;
  return `${(milliseconds / 1_000).toFixed(milliseconds < 10_000 ? 1 : 0)} s`;
};

const authOwnerLabel = (owner: string | undefined) => {
  if (owner === "provider_cli") return "Provider-owned sign-in";
  if (owner === "harness_environment") return "Harness environment";
  if (owner === "external_bridge") return "External bridge";
  return "Authentication owner unclassified";
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

const providerRouteLabel = (
  name: string,
  health: ProviderHealth,
  position: number,
) => {
  const model = health.configured_model || health.last_model;
  return {
    title: model || name,
    detail: `${name} · ${position === 1 ? "primary" : `fallback ${position - 1}`}`,
  };
};

function AutomaticRoute({ providers }: { providers: ProviderMap }) {
  return (
    <section aria-labelledby="automatic-route-title">
      <div className="mb-2 flex items-center gap-2">
        <NetworkIcon
          className="size-4 text-muted-foreground"
          aria-hidden="true"
        />
        <h3 id="automatic-route-title" className="text-sm font-semibold">
          Automatic route
        </h3>
      </div>
      <p className="mb-3 text-xs leading-relaxed text-muted-foreground">
        The harness can use different configured accounts for planning, acting,
        and checking.
      </p>
      <div className="border-y border-border/70">
        {ROUTE_ROLES.map((role) => {
          const routed = Object.entries(providers)
            .flatMap(([name, health]) =>
              (health.routes ?? [])
                .filter((route) => route.role === role.key)
                .map((route) => ({
                  name,
                  health,
                  position: route.position ?? 99,
                })),
            )
            .sort((left, right) => left.position - right.position);
          return (
            <div
              key={role.key}
              className="grid grid-cols-[5.5rem_minmax(0,1fr)] gap-3 border-b border-border/60 py-3 last:border-b-0"
            >
              <div>
                <p className="text-xs font-medium">{role.label}</p>
                <p className="mt-0.5 text-[11px] text-muted-foreground">
                  {role.description}
                </p>
              </div>
              <div className="flex min-w-0 flex-col gap-2">
                {routed.length ? (
                  routed.map(({ name, health, position }) => {
                    const label = providerRouteLabel(name, health, position);
                    return (
                      <div
                        key={`${role.key}:${name}:${position}`}
                        className="flex min-w-0 items-center gap-2"
                      >
                        <span
                          className={
                            health.ready === false
                              ? "size-1.5 shrink-0 rounded-full bg-amber-400"
                              : "size-1.5 shrink-0 rounded-full bg-emerald-400"
                          }
                        >
                          <span className="sr-only">
                            {health.ready === false ? "Setup needed" : "Ready"}
                          </span>
                        </span>
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-xs font-medium">
                            {label.title}
                          </span>
                          <span className="block truncate text-[11px] text-muted-foreground">
                            {label.detail}
                          </span>
                        </span>
                      </div>
                    );
                  })
                ) : (
                  <p className="self-center text-xs text-muted-foreground">
                    No provider configured
                  </p>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </section>
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
  const routeLabels = (health.routes ?? [])
    .slice()
    .sort((left, right) => (left.position ?? 99) - (right.position ?? 99))
    .map((route) => {
      const role = ROUTE_ROLES.find(
        (candidate) => candidate.key === route.role,
      );
      return `${role?.label || titleCase(route.role || "route")} ${
        route.position === 1
          ? "primary"
          : `fallback ${Math.max(1, (route.position ?? 2) - 1)}`
      }`;
    });

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
            {health.support_tier ? (
              <Badge variant="outline">{titleCase(health.support_tier)}</Badge>
            ) : null}
          </div>
          <p className="mt-1 truncate text-xs text-muted-foreground">
            {name} · {titleCase(health.kind || "provider")}
          </p>

          <dl className="mt-3 grid gap-x-4 gap-y-3 sm:grid-cols-2">
            <div>
              <dt className="text-[10px] font-semibold tracking-[0.08em] text-muted-foreground uppercase">
                Authentication
              </dt>
              <dd className="mt-1 text-xs font-medium">
                {authOwnerLabel(health.credential_owner)}
              </dd>
            </div>
            <div>
              <dt className="text-[10px] font-semibold tracking-[0.08em] text-muted-foreground uppercase">
                Route
              </dt>
              <dd className="mt-1 text-xs font-medium">
                {routeLabels.join(" · ") || "Configured, not in auto route"}
              </dd>
            </div>
            <div>
              <dt className="text-[10px] font-semibold tracking-[0.08em] text-muted-foreground uppercase">
                Activity
              </dt>
              <dd className="mt-1 text-xs font-medium">
                {health.successes ?? 0}/{health.calls ?? 0} successful ·{" "}
                {latencyLabel(health.last_latency_ms)}
              </dd>
            </div>
            <div>
              <dt className="text-[10px] font-semibold tracking-[0.08em] text-muted-foreground uppercase">
                Evidence
              </dt>
              <dd className="mt-1 text-xs font-medium">
                {conformanceLabel(health.conformance_status)}
              </dd>
            </div>
          </dl>

          {!ready ? (
            <p className="mt-3 border-l-2 border-amber-400/60 pl-3 text-xs leading-relaxed text-amber-100/80">
              {setupGuidance(health.credential_owner)}
            </p>
          ) : null}
        </div>
      </div>
    </article>
  );
}

function AvailableAdapters({
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
        <CircleDashedIcon
          className="size-4 text-muted-foreground"
          aria-hidden="true"
        />
        Available adapters
        <span className="ml-auto text-xs font-normal text-muted-foreground">
          {catalog.length} supported
        </span>
      </summary>
      <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
        Support tier describes the maintained adapter, not live account
        readiness.
      </p>
      <div className="mt-3 divide-y divide-border/60">
        {catalog.map((entry) => (
          <div key={entry.kind} className="py-3">
            <div className="flex flex-wrap items-center gap-2">
              <p className="text-xs font-medium">{titleCase(entry.kind)}</p>
              <Badge variant="outline">{titleCase(entry.support_tier)}</Badge>
              {configuredKinds.has(entry.kind) ? (
                <Badge variant="secondary">Configured</Badge>
              ) : null}
            </div>
            <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">
              {entry.interface} · {entry.pixel_input} ·{" "}
              {entry.structured_output}
            </p>
            <p className="mt-1 text-[11px] text-muted-foreground">
              {entry.auth
                .map(
                  (auth) =>
                    `${authModeLabel(auth.mode)} · ${authOwnerLabel(auth.credential_owner)}`,
                )
                .join(" / ")}
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
}: ProviderConnectionsSheetProps) {
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
            Configured accounts, automatic routing, and readiness. Provider
            secret values never enter this UI.
          </SheetDescription>
          <div className="flex flex-wrap gap-2 pt-2">
            <Badge variant="secondary">
              {readyCount}/{entries.length} ready
            </Badge>
            <Badge variant="outline">
              <ShieldCheckIcon data-icon="inline-start" aria-hidden="true" />
              Harness-owned policy
            </Badge>
          </div>
        </SheetHeader>
        <ScrollArea className="min-h-0 flex-1 px-4 pb-4">
          <div className="flex flex-col gap-6">
            <AutomaticRoute providers={providers} />
            <section aria-labelledby="configured-providers-title">
              <h3
                id="configured-providers-title"
                className="text-sm font-semibold"
              >
                Configured accounts
              </h3>
              <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                Readiness is a local prerequisite check. Conformance evidence is
                reported separately.
              </p>
              <div className="mt-2">
                {entries.length ? (
                  entries.map(([name, health]) => (
                    <ProviderRow key={name} name={name} health={health} />
                  ))
                ) : (
                  <p className="border-t border-border/70 py-6 text-sm text-muted-foreground">
                    No model providers are configured.
                  </p>
                )}
              </div>
            </section>
            <AvailableAdapters catalog={catalog} providers={providers} />
            <p className="border-t border-border/70 pt-4 text-xs leading-relaxed text-muted-foreground">
              Connections are configured on the local harness host. This view
              reports ownership and readiness without reading, copying, or
              storing provider secrets.
            </p>
          </div>
        </ScrollArea>
      </SheetContent>
    </Sheet>
  );
}
