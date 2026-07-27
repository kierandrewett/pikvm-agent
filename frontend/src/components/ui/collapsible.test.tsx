// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";

describe("Collapsible", () => {
  it("retains final panel geometry when reduced motion disables animation", () => {
    const view = render(
      <Collapsible defaultOpen>
        <CollapsibleTrigger>Details</CollapsibleTrigger>
        <CollapsibleContent>Receipt</CollapsibleContent>
      </Collapsible>,
    );

    const panel = view.container.querySelector(
      '[data-slot="collapsible-content"]',
    );
    expect(panel).toHaveClass("motion-reduce:animate-none!");
    expect(panel).toHaveClass("motion-reduce:data-open:h-auto!");
    expect(panel).toHaveClass("motion-reduce:data-closed:h-0!");
  });
});
