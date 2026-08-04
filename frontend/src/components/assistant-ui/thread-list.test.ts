import { describe, expect, it } from "vitest";
import { shouldOfferTaskSearch } from "./thread-list";

describe("shouldOfferTaskSearch", () => {
  it("keeps the field away from a list you can already read", () => {
    // The case that prompted this: one task, and a "Search tasks" box above it.
    expect(shouldOfferTaskSearch(0)).toBe(false);
    expect(shouldOfferTaskSearch(1)).toBe(false);
    expect(shouldOfferTaskSearch(7)).toBe(false);
  });

  it("offers it once the list is long enough to be worth searching", () => {
    expect(shouldOfferTaskSearch(8)).toBe(true);
    expect(shouldOfferTaskSearch(40)).toBe(true);
  });
});
