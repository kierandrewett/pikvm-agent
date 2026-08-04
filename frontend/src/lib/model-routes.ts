import type {
  ModelPreferences,
  ModelRole,
  ProviderHealth,
  ProviderMap,
  RunModelRoute,
} from "@/types";

export const MODEL_ROLES: ReadonlyArray<{
  key: ModelRole;
  label: string;
  shortLabel: string;
  description: string;
}> = [
  {
    key: "assistant",
    label: "Chat",
    shortLabel: "Chat",
    description: "Answers and uses tools",
  },
  {
    key: "reasoner",
    label: "Reasoning",
    shortLabel: "Plan",
    description: "Plans the task",
  },
  {
    key: "controller",
    label: "Acting",
    shortLabel: "Act",
    description: "Chooses computer input",
  },
  {
    key: "verifier",
    label: "Checking",
    shortLabel: "Check",
    description: "Verifies the screen",
  },
];

export const providerModelLabel = (
  name: string,
  health: ProviderHealth | undefined,
) => health?.configured_model || health?.last_model || name;

/**
 * Short, familiar names for the model a provider runs. "account-default" is a
 * real configured value meaning "whatever this account defaults to", which
 * names nothing to a reader, so it resolves to the product name instead. Shared
 * by the composer's picker and the Models sheet: the same account appearing as
 * "Codex" in one and "account-default" in the other, one click apart, is the
 * kind of thing that makes the whole control feel untrustworthy.
 */
const FAMILIAR_MODEL_ALIASES: Record<string, string> = {
  opus: "Opus",
  sonnet: "Sonnet",
  haiku: "Haiku",
  "account-default": "Codex",
};

export const compactModelLabel = (model: string) =>
  FAMILIAR_MODEL_ALIASES[model.toLowerCase()] || model;

export const defaultRoleRoute = (
  providers: ProviderMap,
  role: ModelRole,
): string[] => {
  const configured = Object.entries(providers)
    .flatMap(([name, health]) =>
      (health.routes ?? [])
        .filter(
          (route) =>
            route.role === role &&
            health.ready !== false,
        )
        .map((route) => ({
          name,
          position: route.position ?? 99,
        })),
    )
    .sort((left, right) => left.position - right.position)
    .map(({ name }) => name);
  return role === "assistant" && configured.length === 0
    ? defaultRoleRoute(providers, "reasoner")
    : configured;
};

export const effectiveRoleRoute = ({
  providers,
  preferences,
  activeRoute,
  activeProvider,
  locked,
  role,
}: {
  providers: ProviderMap;
  preferences: ModelPreferences;
  activeRoute?: RunModelRoute | null;
  activeProvider?: string | null;
  locked: boolean;
  role: ModelRole;
}) => {
  if (locked && activeProvider) return [activeProvider];
  if (locked && activeRoute?.[role]?.length) {
    return activeRoute[role]!.filter((name) => providers[name]);
  }
  if (
    locked &&
    role === "assistant" &&
    activeRoute?.reasoner?.length
  ) {
    return activeRoute.reasoner.filter((name) => providers[name]);
  }

  const automatic = defaultRoleRoute(providers, role);
  const preferred = preferences[role];
  if (
    !preferred ||
    !providers[preferred] ||
    providers[preferred]?.ready === false
  ) {
    return automatic;
  }
  return [preferred, ...automatic.filter((name) => name !== preferred)];
};

export const effectiveRolePrimary = (
  options: Parameters<typeof effectiveRoleRoute>[0],
) => effectiveRoleRoute(options)[0];

/** Which single answer describes the preferences: the harness picks ("auto"),
 *  one provider runs every stage (its name), or the stages are split. Shared by
 *  the composer's inline picker and the Models sheet so they can never disagree. */
export const unifiedSelection = (preferences: ModelPreferences) => {
  const values = MODEL_ROLES.map((role) => preferences[role.key]);
  if (values.every((value) => !value)) return "auto";
  const [first] = values;
  if (first && values.every((value) => value === first)) return first;
  return "split";
};
