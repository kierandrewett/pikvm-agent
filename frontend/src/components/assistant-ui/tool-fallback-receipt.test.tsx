// @vitest-environment jsdom

import "@testing-library/jest-dom/vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ToolFallbackReceipt } from "@/components/assistant-ui/tool-fallback";

const SEARCH_RESULT = JSON.stringify({
  content: [
    {
      type: "text",
      text: JSON.stringify({
        title: "Download Python | Python.org",
        href: "https://www.python.org/downloads/",
      }),
    },
  ],
  structured_content: {
    result: Array.from({ length: 5 }, (_, index) => ({
      title: `Result ${index + 1}`,
      href: `https://example.test/${index + 1}`,
    })),
  },
});

describe("ToolFallbackReceipt", () => {
  it("summarizes a search response and keeps exact evidence behind Details", () => {
    render(
      <ToolFallbackReceipt
        toolName="web.search_text"
        argsText={JSON.stringify({
          query: "python.org latest stable Python release download",
          max_results: 5,
        })}
        result={SEARCH_RESULT}
      />,
    );

    expect(screen.getByText("5 results returned")).toBeVisible();
    expect(screen.getByText("Query")).toBeVisible();
    expect(
      screen.getByText("python.org latest stable Python release download"),
    ).toBeVisible();
    expect(screen.getByText("Max results")).toBeVisible();

    const details = screen.getByRole("button", { name: "Details" });
    expect(details).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Result:")).toBeNull();

    fireEvent.click(details);

    expect(details).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Exact arguments")).toBeVisible();
    expect(screen.getByText("Raw result")).toBeVisible();
    expect(screen.getByText(SEARCH_RESULT)).toBeVisible();
  });

  it("summarizes structured completion without inventing verification", () => {
    render(
      <ToolFallbackReceipt
        toolName="files.inspect"
        argsText='{"path":"report.xlsx"}'
        result={{ status: "completed" }}
      />,
    );

    expect(screen.getByText("Completed")).toBeVisible();
    expect(screen.queryByText("Verified")).toBeNull();
  });
});
