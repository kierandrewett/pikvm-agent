import { SquareTerminalIcon } from "lucide-react";
import { Badge } from "@/components/ui/badge";

const callerValue = (
  caller: Record<string, unknown> | undefined,
  key: string,
) => {
  const value = caller?.[key];
  return typeof value === "string" && value.trim() ? value.trim() : "";
};

export function RunProvenance({
  caller,
}: {
  caller?: Record<string, unknown>;
}) {
  const label = callerValue(caller, "label");
  if (!label) return null;

  const interfaceName = callerValue(caller, "interface");
  const title = interfaceName
    ? `Task submitted through ${interfaceName} by ${label}`
    : `Task submitted by ${label}`;

  return (
    <Badge variant="ghost" title={title} className="hidden sm:inline-flex">
      <SquareTerminalIcon data-icon="inline-start" aria-hidden="true" />
      via {label}
    </Badge>
  );
}
