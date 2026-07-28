import { useEffect, useState } from "react";
import { RefreshCwIcon } from "lucide-react";
import { Button } from "@/components/ui/button";

const CHECK_INTERVAL_MS = 15_000;

export const isDifferentUiBuild = (
  candidate: unknown,
  current = __PIKVM_UI_BUILD__,
) =>
  typeof candidate === "object" &&
  candidate !== null &&
  "build" in candidate &&
  typeof candidate.build === "string" &&
  candidate.build.length > 0 &&
  candidate.build !== current;

export function UiUpdateBadge() {
  const [available, setAvailable] = useState(false);

  useEffect(() => {
    if (available) return;
    let active = true;
    const check = async () => {
      try {
        const response = await fetch("/app/version.json", {
          cache: "no-store",
        });
        if (!response.ok) return;
        if (active && isDifferentUiBuild(await response.json())) {
          setAvailable(true);
        }
      } catch {
        // Connection state already owns offline reporting. A version check is
        // advisory and must never replace or obscure that signal.
      }
    };
    void check();
    const timer = window.setInterval(check, CHECK_INTERVAL_MS);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [available]);

  if (!available) return null;
  return (
    <Button
      type="button"
      size="xs"
      variant="outline"
      onClick={() => window.location.reload()}
      title="A newer interface is ready. Reload when convenient."
      aria-label="Reload interface update"
    >
      <RefreshCwIcon data-icon="inline-start" aria-hidden="true" />
      Update ready
    </Button>
  );
}
