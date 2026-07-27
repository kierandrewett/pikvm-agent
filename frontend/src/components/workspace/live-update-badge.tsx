import {
  CheckCircle2Icon,
  RadioIcon,
  RefreshCwIcon,
  WifiOffIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import type { LiveUpdateStatus } from "@/types";

type LiveUpdateBadgeProps = {
  status: LiveUpdateStatus;
};

const presentation = {
  idle: {
    label: "Harness ready",
    description: "The authenticated harness is ready for a new task.",
    variant: "outline" as const,
    icon: CheckCircle2Icon,
  },
  connecting: {
    label: "Connecting",
    description: "Connecting to the live run update stream.",
    variant: "outline" as const,
    icon: RefreshCwIcon,
  },
  live: {
    label: "Live",
    description: "Computer actions and model activity are updating live.",
    variant: "secondary" as const,
    icon: RadioIcon,
  },
  retrying: {
    label: "Reconnecting",
    description:
      "The live update stream was interrupted. Reconnecting while bounded polling keeps the run current.",
    variant: "outline" as const,
    icon: RefreshCwIcon,
  },
  offline: {
    label: "Updates offline",
    description:
      "The live update stream is unavailable. Bounded polling is keeping the run current.",
    variant: "destructive" as const,
    icon: WifiOffIcon,
  },
} satisfies Record<
  LiveUpdateStatus,
  {
    label: string;
    description: string;
    variant: "outline" | "secondary" | "destructive";
    icon: typeof RadioIcon;
  }
>;

export function LiveUpdateBadge({ status }: LiveUpdateBadgeProps) {
  const item = presentation[status];
  const Icon = item.icon;
  return (
    <Badge
      variant={item.variant}
      title={item.description}
      aria-label={item.description}
      aria-live="polite"
    >
      <Icon data-icon="inline-start" aria-hidden="true" />
      {item.label}
    </Badge>
  );
}
