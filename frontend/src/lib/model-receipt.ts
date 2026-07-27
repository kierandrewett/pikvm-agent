export type ModelReceipt = {
  provider: string;
  model: string;
  latencyMs?: number;
};

const record = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};

export const parseModelReceipt = (
  value: unknown,
): ModelReceipt | undefined => {
  const receipt = record(value);
  const provider =
    typeof receipt.provider === "string" ? receipt.provider.trim() : "";
  const model =
    typeof receipt.model === "string" ? receipt.model.trim() : "";
  if (!provider && !model) return undefined;
  const rawLatency = receipt.latency_ms ?? receipt.latencyMs;
  const latencyMs =
    typeof rawLatency === "number" && Number.isFinite(rawLatency)
      ? rawLatency
      : undefined;
  return { provider, model, latencyMs };
};

export const parseModelSelectionReceipt = (
  args: unknown,
): ModelReceipt | undefined => {
  const receipt = record(record(args).__receipt);
  return parseModelReceipt(receipt.selected_by);
};
