import { BotIcon, LockKeyholeIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  effectiveRolePrimary,
  MODEL_ROLES,
  providerModelLabel,
} from "@/lib/model-routes";
import type {
  ModelPreferences,
  ProviderMap,
  RunModelRoute,
} from "@/types";

type ModelPickerProps = {
  providers: ProviderMap;
  preferences: ModelPreferences;
  activeRoute?: RunModelRoute | null;
  activeProvider?: string | null;
  locked: boolean;
  onOpenModels: () => void;
};

const compactModelLabel = (model: string) => {
  const familiarAliases: Record<string, string> = {
    opus: "Opus",
    sonnet: "Sonnet",
    haiku: "Haiku",
    "account-default": "Codex",
  };
  return familiarAliases[model.toLowerCase()] || model;
};

export function ModelPicker({
  providers,
  preferences,
  activeRoute,
  activeProvider,
  locked,
  onOpenModels,
}: ModelPickerProps) {
  const primaries = MODEL_ROLES.map((role) => {
    const provider = effectiveRolePrimary({
      providers,
      preferences,
      activeRoute,
      activeProvider,
      locked,
      role: role.key,
    });
    return {
      ...role,
      provider,
      model: provider
        ? providerModelLabel(provider, providers[provider])
        : "No ready model",
    };
  });
  const models = primaries.map(({ model }) => model);
  const visibleModels = [...new Set(models.map(compactModelLabel))];
  const summary =
    visibleModels.length <= 2
      ? visibleModels.join(" + ")
      : `${visibleModels[0]} + ${visibleModels.length - 1} more`;
  const routeDescription = primaries
    .map(({ label, model, provider }) =>
      `${label}: ${model}${provider && model !== provider ? ` (${provider})` : ""}`,
    )
    .join(". ");

  return (
    <Button
      type="button"
      size="sm"
      variant="ghost"
      className="max-w-[44vw] justify-start px-1.5 sm:max-w-[min(58vw,24rem)]"
      aria-label={`Configure model route. ${routeDescription}`}
      title={`${routeDescription}.${locked ? " Locked for this run." : " Click to configure this task."}`}
      onClick={onOpenModels}
    >
      {locked ? (
        <LockKeyholeIcon data-icon="inline-start" />
      ) : (
        <BotIcon data-icon="inline-start" />
      )}
      <span className="truncate">{summary}</span>
    </Button>
  );
}
