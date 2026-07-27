import { BotIcon } from "lucide-react";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { ProviderMap } from "@/types";

type ModelPickerProps = {
  providers: ProviderMap;
  value: string;
  onValueChange: (value: string) => void;
};

export function ModelPicker({
  providers,
  value,
  onValueChange,
}: ModelPickerProps) {
  const items = [
    {
      label: "Auto route",
      value: "auto",
      disabled: false,
      support: "Choose the best ready provider for each role",
    },
    ...Object.entries(providers).map(([name, health]) => ({
      label: `${health.configured_model || name} · ${name}`,
      value: name,
      disabled: health.ready === false,
      support: [
        health.support_tier
          ? `${health.support_tier} support`
          : "support unclassified",
        health.credential_owner === "provider_cli"
          ? "provider-owned login"
          : health.credential_owner === "harness_environment"
            ? "server credential"
            : "",
      ]
        .filter(Boolean)
        .join(" · "),
    })),
  ];

  return (
    <Select
      items={items}
      value={value || "auto"}
      onValueChange={(next) =>
        onValueChange(!next || next === "auto" ? "" : next)
      }
    >
      <SelectTrigger
        size="sm"
        aria-label="Model provider"
        className="max-w-56 border-transparent bg-transparent px-1.5 shadow-none"
      >
        <BotIcon aria-hidden="true" />
        <SelectValue placeholder="Choose model" />
      </SelectTrigger>
      <SelectContent alignItemWithTrigger={false} align="start">
        <SelectGroup>
          {items.map((item) => (
            <SelectItem
              key={item.value}
              value={item.value}
              disabled={item.disabled}
            >
              <span className="flex flex-col items-start gap-0">
                <span>{item.label}</span>
                <span className="text-muted-foreground text-[11px] font-normal">
                  {item.support}
                </span>
              </span>
            </SelectItem>
          ))}
          <SelectSeparator />
          <p className="text-muted-foreground max-w-72 px-2 py-1.5 text-[11px] leading-relaxed">
            Tier ≠ live-tested readiness. Unready providers cannot be selected.
          </p>
        </SelectGroup>
      </SelectContent>
    </Select>
  );
}
