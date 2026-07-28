// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import {
  hasFreshRunActivity,
  LiveUpdateBadge,
} from "@/components/workspace/live-update-badge";

afterEach(cleanup);

describe("LiveUpdateBadge", () => {
  it("names a live stream without claiming the computer itself is connected", () => {
    render(<LiveUpdateBadge status="live" />);

    expect(screen.getByText("Live")).not.toBeNull();
    expect(
      screen.getByLabelText(
        "Computer actions and model activity are updating live.",
      ),
    ).not.toBeNull();
    expect(screen.queryByText(/MCP connected/i)).toBeNull();
  });

  it("keeps degraded delivery explicit while polling remains available", () => {
    render(<LiveUpdateBadge status="offline" />);

    expect(screen.getByText("Updates offline")).not.toBeNull();
    expect(
      screen.getByLabelText(/Bounded polling is keeping the run current/),
    ).not.toBeNull();
  });

  it("never presents stale model activity while updates are degraded", () => {
    expect(hasFreshRunActivity("idle")).toBe(true);
    expect(hasFreshRunActivity("connecting")).toBe(true);
    expect(hasFreshRunActivity("live")).toBe(true);
    expect(hasFreshRunActivity("retrying")).toBe(false);
    expect(hasFreshRunActivity("offline")).toBe(false);
  });
});
