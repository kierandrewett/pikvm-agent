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

export const defaultRoleRoute = (
  providers: ProviderMap,
  role: ModelRole,
) =>
  Object.entries(providers)
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
