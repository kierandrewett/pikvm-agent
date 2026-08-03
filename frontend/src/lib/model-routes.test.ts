import { describe, expect, it } from "vitest";
import { MODEL_ROLES } from "@/lib/model-routes";

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
