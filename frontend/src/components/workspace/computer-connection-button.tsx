import { MonitorIcon, MonitorOffIcon } from "lucide-react";
import { Button } from "@/components/ui/button";

type ComputerConnectionButtonProps = {
  enabled: boolean;
  mcpServerName: string;
  machineName?: string;
  onOpen: () => void;
};

const PLACEHOLDER_MACHINE_NAMES = new Set([
  "",
  "managed computer",
  "unlabelled target",
]);

const displayMachineName = (machineName?: string) => {
  const candidate = machineName?.trim() ?? "";
  return PLACEHOLDER_MACHINE_NAMES.has(candidate.toLocaleLowerCase())
    ? "Managed computer"
    : candidate;
};

export function ComputerConnectionButton({
  enabled,
  mcpServerName,
  machineName,
  onOpen,
}: ComputerConnectionButtonProps) {
  const label = enabled ? displayMachineName(machineName) : "Chat only";
  const state = enabled ? "configured" : "no computer";
  const description = enabled
    ? `${label}. ${mcpServerName} is configured. Target reachability is checked when computer work begins.`
    : "No managed computer is configured. Chat and research tools remain available.";

  return (
    <Button
      type="button"
      size="sm"
      variant="ghost"
      className="min-w-0 max-w-[34vw] justify-start px-1.5 sm:max-w-52"
      aria-label={
        enabled
          ? `Open managed computer. ${description}`
          : `Open computer connection details. ${description}`
      }
      title={description}
      onClick={onOpen}
    >
      {enabled ? (
        <MonitorIcon data-icon="inline-start" />
      ) : (
        <MonitorOffIcon data-icon="inline-start" />
      )}
      <span className="min-w-0 truncate">{label}</span>
      <span className="shrink-0 text-[10px] font-normal text-muted-foreground sm:text-[11px]">
        {state}
      </span>
    </Button>
  );
}
