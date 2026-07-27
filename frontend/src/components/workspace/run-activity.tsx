import { LoaderCircleIcon } from "lucide-react";
import type { RunSnapshot } from "@/types";

type ActiveActivity = RunSnapshot["active_activity"];

export const activityPresentation = (activity: ActiveActivity) => {
  const role = activity?.kind === "model" ? activity.role : "";
  const label =
    role === "reasoner"
      ? "Planning"
      : role === "controller"
        ? "Choosing next action"
        : role === "verifier"
          ? "Checking the result"
          : "Working";
  const model =
    activity?.kind === "model" && activity.model?.trim()
      ? activity.model.trim()
      : "";
  const provider =
    activity?.kind === "model" && activity.provider?.trim()
      ? activity.provider.trim()
      : "";
  const route = [model, provider].filter(Boolean).join(" via ");
  return {
    label,
    model,
    title: route ? `${label} — ${route}` : label,
  };
};

export function RunActivity({
  activity,
  working,
}: {
  activity?: ActiveActivity;
  working: boolean;
}) {
  if (!working || activity?.kind === "tool") return null;
  const presentation = activityPresentation(activity);

  return (
    <div
      className="aui-run-activity"
      role="status"
      aria-live="polite"
      title={presentation.title}
    >
      <LoaderCircleIcon
        className="size-3.5 shrink-0 animate-spin"
        aria-hidden="true"
      />
      <span className="font-medium text-foreground/80">
        {presentation.label}
      </span>
      {presentation.model ? (
        <span className="truncate text-muted-foreground">
          {presentation.model}
        </span>
      ) : null}
    </div>
  );
}
