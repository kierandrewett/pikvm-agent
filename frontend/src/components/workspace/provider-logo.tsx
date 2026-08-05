import { useState } from "react";
import type { ModelCatalog } from "@/types";
import { cn } from "@/lib/utils";

/** models.dev serves per-provider SVGs; our adapter kinds map to those ids. */
const logoUrlForKind = (
  kind: string | undefined,
  catalog: ModelCatalog | undefined,
) => {
  if (!kind || !catalog?.available) return null;
  const providerId = catalog.kinds[kind]?.[0];
  if (!providerId) return null;
  return catalog.providers[providerId]?.logo_url ?? null;
};

const initials = (name: string) =>
  name
    .replace(/[^a-zA-Z0-9]+/g, " ")
    .trim()
    .split(" ")
    .slice(0, 2)
    .map((word) => word[0]?.toUpperCase() ?? "")
    .join("") || "?";

/**
 * A provider mark, from the models.dev logo where one exists. Everything
 * degrades to initials: an offline harness has no catalog, an unmapped kind
 * has no logo, and the network can still fail after the URL is known — so the
 * fallback is the default state, not an error path.
 */
export function ProviderLogo({
  kind,
  name,
  catalog,
  className,
}: {
  kind?: string;
  name: string;
  catalog?: ModelCatalog;
  className?: string;
}) {
  const [failed, setFailed] = useState(false);
  const url = logoUrlForKind(kind, catalog);
  const shell = cn(
    "flex size-5 shrink-0 items-center justify-center overflow-hidden rounded bg-muted text-[9px] font-semibold text-muted-foreground",
    className,
  );

  if (!url || failed) {
    return (
      <span className={shell} aria-hidden="true">
        {initials(name)}
      </span>
    );
  }
  return (
    <span className={shell} aria-hidden="true">
      {/* models.dev serves these marks as flat BLACK artwork — every logo the
          catalog returns is pure black with no colour in it at all, which on a
          dark surface rendered them as empty grey squares. Inverting in dark
          mode paints them light instead. A coloured mark would come out wrong
          here, so this holds only while they stay monochrome. */}
      <img
        src={url}
        alt=""
        loading="lazy"
        className="size-4 object-contain dark:invert"
        onError={() => setFailed(true)}
      />
    </span>
  );
}
