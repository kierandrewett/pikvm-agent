import { LockKeyholeIcon, Settings2Icon } from "lucide-react";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  effectiveRolePrimary,
  providerModelLabel,
  unifiedSelection,
} from "@/lib/model-routes";
import type {
  ModelPreferences,
  ModelRole,
  ProviderMap,
  RunModelRoute,
} from "@/types";
import { MODEL_ROLES } from "@/lib/model-routes";

type ModelPickerProps = {
  providers: ProviderMap;
  preferences: ModelPreferences;
  activeRoute?: RunModelRoute | null;
  activeProvider?: string | null;
  locked: boolean;
  onPreferenceChange: (role: ModelRole, provider: string) => void;
  onResetPreferences: () => void;
  onOpenModels: () => void;
};

/** Short, familiar names for the trigger — the composer is tight on space. */
const compactModelLabel = (model: string) => {
  const familiarAliases: Record<string, string> = {
    opus: "Opus",
    sonnet: "Sonnet",
    haiku: "Haiku",
    "account-default": "Codex",
  };
  return familiarAliases[model.toLowerCase()] || model;
};

/**
 * The model control, Copilot-style: a small dropdown that lives IN the
 * composer. Pick a model and it runs the whole task; "Auto" hands the choice
 * to the harness; "Configure models…" opens the full sheet for accounts,
 * stage-splitting and diagnostics. The selection is the same unified state the
 * sheet edits, so the two can never disagree.
 */
export function ModelPicker({
  providers,
  preferences,
  activeRoute,
  activeProvider,
  locked,
  onPreferenceChange,
  onResetPreferences,
  onOpenModels,
}: ModelPickerProps) {
  const readyProviders = Object.entries(providers).filter(
    ([, health]) => health.ready !== false,
  );
  const selection = locked
    ? activeProvider ||
      effectiveRolePrimary({
        providers,
        preferences,
        activeRoute,
        activeProvider,
        locked,
        role: "assistant",
      }) ||
      "auto"
    : unifiedSelection(preferences);

  // "Auto" on its own never said which model would actually run, which is the
  // one thing someone opens this control to find out. This is the same
  // resolution the harness performs, so what is shown is what will run.
  const autoPrimary = effectiveRolePrimary({
    providers,
    preferences,
    activeRoute,
    activeProvider,
    locked,
    role: "assistant",
  });
  const autoLabel = autoPrimary
    ? compactModelLabel(providerModelLabel(autoPrimary, providers[autoPrimary]))
    : "";

  const triggerLabel =
    selection === "auto"
      ? "Auto"
      : selection === "split"
        ? "Split"
        : compactModelLabel(
            providerModelLabel(selection, providers[selection]),
          );
  const triggerHint = selection === "auto" ? autoLabel : "";

  const items = [
    { value: "auto", label: "Auto", hint: autoLabel },
    ...readyProviders.map(([name, health]) => ({
      value: name,
      label: compactModelLabel(providerModelLabel(name, health)),
    })),
    ...(selection === "split"
      ? [{ value: "split", label: "Split by stage" }]
      : []),
    { value: "manage", label: "Configure models…" },
  ];

  const choose = (next: string | null) => {
    if (!next || next === "split") return;
    if (next === "manage") {
      onOpenModels();
      return;
    }
    if (next === "auto") {
      onResetPreferences();
      return;
    }
    // One model for the whole task: every stage points at it (the sheet's
    // advanced view reads back exactly this state).
    for (const role of MODEL_ROLES) onPreferenceChange(role.key, next);
  };

  return (
    <Select items={items} value={selection} onValueChange={choose} disabled={locked}>
      <SelectTrigger
        size="sm"
        aria-label={`Model: ${triggerLabel}${triggerHint ? ` — ${triggerHint}` : ""}${locked ? " (locked for this run)" : ""}`}
        title={
          locked
            ? "This task keeps the model it started with."
            : "Model that runs your next task"
        }
        className="max-w-[38vw] border-none bg-transparent px-1.5 font-medium shadow-none sm:max-w-56 dark:bg-transparent"
      >
        {locked ? (
          <LockKeyholeIcon
            className="size-3.5 text-muted-foreground"
            aria-hidden="true"
          />
        ) : null}
        <SelectValue>
          {triggerLabel}
          {triggerHint ? (
            <>
              {" "}
              <span className="font-normal text-muted-foreground">
                {triggerHint}
              </span>
            </>
          ) : null}
        </SelectValue>
      </SelectTrigger>
      <SelectContent alignItemWithTrigger={false}>
        <SelectGroup>
          {items
            .filter((item) => item.value !== "manage")
            .map((item) => (
              <SelectItem
                key={item.value}
                value={item.value}
                disabled={item.value === "split"}
              >
                {item.label}
                {"hint" in item && item.hint ? (
                  // The space is a real text node on purpose: without it the
                  // accessible name computes as "AutoOpus".
                  <>
                    {" "}
                    <span className="text-muted-foreground">{item.hint}</span>
                  </>
                ) : null}
              </SelectItem>
            ))}
        </SelectGroup>
        <SelectSeparator />
        <SelectGroup>
          <SelectItem value="manage">
            <Settings2Icon
              className="size-3.5 text-muted-foreground"
              aria-hidden="true"
            />
            Configure models…
          </SelectItem>
        </SelectGroup>
      </SelectContent>
    </Select>
  );
}
