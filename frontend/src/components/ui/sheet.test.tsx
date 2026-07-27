// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  Sheet,
  SheetContent,
  SheetTitle,
} from "@/components/ui/sheet";

describe("Sheet", () => {
  it("closes instantly and invisibly for reduced-motion users", () => {
    render(
      <Sheet open>
        <SheetContent>
          <SheetTitle>Models</SheetTitle>
        </SheetContent>
      </Sheet>,
    );

    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveClass("motion-reduce:transition-none!");
    expect(dialog).toHaveClass("motion-reduce:data-closed:hidden!");
    expect(document.querySelector('[data-slot="sheet-overlay"]')).toHaveClass(
      "motion-reduce:transition-none!",
      "motion-reduce:data-closed:hidden!",
    );
  });
});
