// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

describe("Tooltip", () => {
  it("cannot leave a closed reduced-motion portal in document layout", () => {
    render(
      <TooltipProvider>
        <Tooltip open>
          <TooltipTrigger>Models</TooltipTrigger>
          <TooltipContent>Configure models</TooltipContent>
        </Tooltip>
      </TooltipProvider>,
    );

    const tooltip = document.querySelector('[data-slot="tooltip-content"]');
    if (!(tooltip instanceof HTMLElement)) {
      throw new Error("Tooltip content did not mount.");
    }
    expect(tooltip).toHaveClass(
      "motion-reduce:data-closed:hidden!",
      "motion-reduce:animate-none!",
    );
    expect(tooltip.parentElement).toHaveClass(
      "motion-reduce:data-closed:hidden!",
    );
  });
});
