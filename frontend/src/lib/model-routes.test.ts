import { describe, expect, it } from "vitest";
import { MODEL_ROLES, unifiedSelection } from "@/lib/model-routes";

describe("model routes", () => {
  it("keeps normal chat independent from computer planning and input", () => {
    expect(MODEL_ROLES.map((role) => role.key)).toEqual([
      "assistant",
      "reasoner",
      "controller",
      "verifier",
    ]);
    expect(MODEL_ROLES[0]).toMatchObject({
      label: "Chat",
      shortLabel: "Chat",
    });
  });
});

describe("unifiedSelection", () => {
  /* Both the composer's picker and the Models sheet ask this what the current
     preferences mean. They used to ask separate copies of it, so the two could
     have described the same state differently — one showing Auto while the
     other showed Split. */
  it("calls it Automatic when nothing is pinned", () => {
    expect(unifiedSelection({})).toBe("auto");
  });

  it("names the provider when one runs every stage", () => {
    const every = Object.fromEntries(
      MODEL_ROLES.map((role) => [role.key, "claude-account"]),
    );
    expect(unifiedSelection(every)).toBe("claude-account");
  });

  it("calls it split when the stages differ", () => {
    const [first, second] = MODEL_ROLES;
    expect(
      unifiedSelection({
        [first!.key]: "claude-account",
        [second!.key]: "codex-account",
      }),
    ).toBe("split");
  });

  it("treats a partly pinned set as split, not as that one provider", () => {
    const [first] = MODEL_ROLES;
    // Only one stage set: the others are not that provider, so "one provider
    // runs everything" is not true.
    expect(unifiedSelection({ [first!.key]: "claude-account" })).toBe("split");
  });
});
