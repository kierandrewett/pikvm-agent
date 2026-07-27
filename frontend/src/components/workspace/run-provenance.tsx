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
  const identity =
    callerValue(caller, "label") || callerValue(caller, "name");
  if (!identity) return null;

  const interfaceName = callerValue(caller, "interface");
  const route = [
    callerValue(caller, "provider"),
    callerValue(caller, "model"),
  ]
    .filter(Boolean)
    .join(" · ");
  const source = interfaceName
    ? `Task submitted through ${interfaceName} by ${identity}`
    : `Task submitted by ${identity}`;
  const title = route ? `${source} using ${route}` : source;
  const label = `via ${identity}${route ? ` · ${route}` : ""}`;

  return (
    <Badge
      variant="ghost"
      title={title}
      className="hidden max-w-[min(42vw,28rem)] sm:inline-flex"
    >
      <SquareTerminalIcon data-icon="inline-start" aria-hidden="true" />
      <span className="truncate">{label}</span>
    </Badge>
  );
}
