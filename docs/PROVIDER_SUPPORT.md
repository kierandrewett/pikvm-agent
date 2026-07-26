# Model-provider support policy

The harness owns one provider-neutral reason → act → verify loop. Model
providers supply structured decisions and pixels; they never receive raw
PiKVM tools or direct authority over the target.

This document describes the adapter contract. It does **not** claim that every
provider account, model alias, CLI version, region, or quota works live. Dated
compatibility and performance evidence belongs in the
[public benchmark scorecard](../bench/README.md).

## Support tiers

- `stable` — a first-party adapter with fail-closed configuration, isolated
  pixel input, structured-output validation, error normalization, timeout and
  cooldown handling, secret-redaction tests, and a backwards-compatibility
  commitment.
- `beta` — a first-party adapter with the same local safety contract, but an
  incomplete live account/client/version matrix. Its configuration may change
  with an explicit migration note.
- `bridge` — a harness contract around an external protocol or process. The
  harness validates its boundary, but cannot guarantee the behavior or
  compatibility of the remote implementation.

A `stable` adapter is eligible for production use only when the exact
provider/client version and configured model also have current live evidence.

## Canonical matrix

| Provider kind | Tier | Interface | Authentication and owner | Pixel input | Output contract |
|---|---|---|---|---|---|
| `codex_cli` | `stable` | Codex exec | saved CLI login; provider CLI | native image attachment | strict JSON Schema |
| `claude_cli` | `stable` | Claude print mode | saved CLI login; provider CLI | isolated Read artifact | strict JSON Schema |
| `gemini_cli` | `beta` | Gemini headless mode | saved CLI login; provider CLI | isolated `@` image artifact | harness-validated JSON |
| `openai_responses` | `stable` | OpenAI Responses API | API-key environment; harness environment | native image input | strict JSON Schema |
| `anthropic_api` | `stable` | Anthropic Messages API | API-key environment; harness environment | base64 image block | JSON Schema |
| `gemini_api` | `stable` | Gemini `generateContent` | API-key environment; harness environment | inline image data | JSON Schema |
| `azure_openai_responses` | `beta` | Azure OpenAI Responses API | API-key/bearer environment or exact CLI bearer command; environment or provider CLI | native image input | strict JSON Schema |
| `vertex_gemini` | `beta` | Vertex AI Gemini `generateContent` | bearer environment or exact CLI bearer command; environment or provider CLI | inline image data | JSON Schema |
| `openai_compatible` | `bridge` | Chat Completions API | API-key environment; harness environment | image data URL | strict JSON Schema |
| `subprocess_json` | `bridge` | custom subprocess | external or bridge-owned | bridge-defined | harness-validated JSON |

The executable code source of truth is
`pikvm_agent/harness/provider_support.py`. Tests require every configurable
provider kind to have exactly one contract and require this policy to name
every contract.

## Credential ownership

The harness never reads or copies a saved CLI credential. Codex, Claude, and
Gemini CLI processes own their login stores. API secrets are named by
environment variable and are neither written to harness configuration nor
returned by readiness, health, support-bundle, UI, or conformance endpoints.

Azure and Vertex command credentials are obtained through an exact argument
vector with empty standard input and an allow-listed environment. Only the
resulting authorization header is used for that provider request. Raw command
output and credential values are not persisted.

`subprocess_json` is deliberately marked external: credential handling inside
the bridge remains the bridge operator's responsibility.

## Readiness is not compatibility

Readiness checks are local and non-billable. They prove only that a configured
executable, dedicated profile directory, or named environment value is
present. They do not prove login validity, quota, model access, endpoint
compatibility, output accuracy, or latency.

Live compatibility requires the explicit, potentially billable blind
conformance command. Its report includes unavailable and failed providers in
the denominator and records the configured model, returned model identities,
exact/schema accuracy, failure classes, usage, median latency, and p95 latency.
The operator UI labels prerequisite-only state as unproven.

## Version and change policy

- Stable provider kinds and configuration fields are not silently removed.
  A replacement is introduced first, the old form remains accepted for at
  least one minor release, and the release notes state the migration.
- Beta changes require a migration note but may land without a deprecation
  window when an upstream provider makes the existing interface unusable.
- Bridge compatibility is versioned at the harness boundary only. Third-party
  servers and subprocesses must be re-qualified by blind conformance.
- A provider, CLI, model, or endpoint change invalidates prior live evidence
  for that combination until the same benchmark is rerun.
- Authentication failures, rate limits, timeouts, schema failures, and model
  mismatches remain visible as coarse durable evidence; they never silently
  select an unsafe machine action.

## Promotion criteria

A beta adapter can move to stable after its local contract is complete and at
least two independently repeated live runs pass the blind conformance suite
for supported authentication modes, with client/provider versions and dated
evidence published. Office and public computer-use benchmark results are
reported separately because provider conformance alone does not measure safe
end-to-end computer operation.
