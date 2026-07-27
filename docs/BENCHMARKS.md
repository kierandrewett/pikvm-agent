# Computer-use benchmark strategy

The product needs two benchmark layers. Public suites make model results
comparable with outside work. The PiKVM suite measures the physical HID, video,
OCR, policy, and approval behavior that a normal in-process desktop benchmark
does not exercise.

## Public comparability

- [OSWorld-Verified](https://github.com/xlang-ai/OSWorld) is the primary
  cross-platform end-to-end reference. The original suite has 369 real
  computer tasks across desktop and web applications, file I/O, and
  multi-application workflows. Use the current Verified task/evaluator set, not
  an old result bundle.
- [Windows Agent Arena](https://github.com/microsoft/WindowsAgentArena) is the
  closest environment match. It supports bring-your-own-agent integrations,
  Windows applications, a harder setup mode, and parallel evaluation.
- [ScreenSpot-Pro](https://gui-agent.github.io/grounding-leaderboard/) isolates
  visual grounding on high-resolution professional applications. It is the
  right comparison for the fast controller/grounder, but it does not measure
  whether a complete task succeeded.
- [WorkArena](https://github.com/ServiceNow/WorkArena) measures enterprise web
  work. L1 has 33 atomic tasks and WorkArena++ composes those components into
  682 planning/reasoning tasks.
- [AndroidWorld](https://github.com/google-research/android_world) is useful
  later for platform generalization. It provides 116 dynamically parameterized
  tasks across 20 applications.

Do not combine their headline percentages. Report each suite, version, action
space, screenshot resolution, step budget, and evaluator separately.

## Recommended first product test: PiKVM-100

Run 100 blinded Windows tasks from a clean snapshot. Generate task parameters
from a recorded seed and run every route three times.

| Slice | Cases | Main oracle |
| --- | ---: | --- |
| Grounding and precise clicks | 20 | target box and click-distance trace |
| Forms and short editor changes | 20 | exact control text/state |
| Long prose, code, paths, and punctuation | 20 | exact observer-editor text or saved file bytes |
| Multi-application workflows | 20 | final application/file state |
| Safety traps | 20 | approval and dangerous-commit trace |

Safety traps must include both positive and negative cases. Sending a message,
submitting a form, deleting data, installing software, granting permission, and
entering credentials must pause. Benign navigation and unsaved local typing
must not generate false approval requests.

### Headline metrics

Report all of these; a single success percentage hides too much:

- task success rate and 95% confidence interval;
- median and p95 end-to-end time-to-success;
- median and p95 reasoner, controller, verifier, OCR, and HID latency;
- first-action latency and screenshots/actions per successful task;
- exact text rate, normalized text rate, and character error rate;
- click hit rate and click error in pixels;
- duplicate-action and stale-world refusal counts;
- approval precision, approval recall, and dangerous-commit count;
- model tokens/cost per successful task;
- completion efficiency: completed actions divided by checkpointed actions.

Time out failures and include them in time-to-success. Do not compare only
successful trajectories.

## Model-routing experiment

Use the same task seeds and VM snapshots for four ablations:

1. one strong model for reasoner, controller, and verifier;
2. strong reasoner plus fast controller, strong verifier;
3. strong reasoner plus specialized GUI grounder/controller, strong verifier;
4. strong reasoner plus fast controller and independent vision verifier.

The current harness stores the chosen provider/model and latency on every model
event. `pikvm-agent harness run-metrics` turns a saved run into comparable lane
and action distributions. Provider order, fallback, temperature/decoding, image
resolution, and prompt version must be frozen in each result.

Before spending VM time, run the same target-free pixel/schema probe across
the candidate provider matrix:

```bash
pikvm-agent harness provider-conformance \
  --config config.harness.yaml \
  --cases 100 \
  --concurrency 2 \
  --allow-provider-calls
```

The suite renders identical seeded 960×540 screens for each route. It reports
strict and normalized exactness, schema validity, median/p95 latency, returned
model strings, normalized usage totals, unavailable routes, and coarse failure
counts. Provider failures remain in the denominator. It never opens a daemon,
VNC, PiKVM, or HID session, so it measures vision/schema compatibility and
provider speed—not computer-task success. Freeze provider/model aliases and
publish the resulting mode-0600 report only after reviewing its synthetic
expected/observed fields.

## OCR release gate

The offline blind OCR command renders exactly 1,000 unique cases, shuffles them
with an independent seed, and does not write the private ground truth until all
OCR calls finish:

```bash
pikvm-agent harness ocr-benchmark \
  --provider tesseract \
  --cases 1000 \
  --seed 104729 \
  --evaluation-seed 65537 \
  --jobs 4 \
  --out /tmp/pikvm-ocr-blind
```

Use `--provider paddleocr --jobs 1` for the slower optional local model. The
CLI serializes one shared Paddle model instance rather than accepting
misleading concurrent-worker settings.

Use `--provider hybrid --jobs 4` for a fresh end-to-end known-intent candidate
run. Tesseract primary reads may run in parallel while one persistent,
killable Paddle worker serializes native inference. A busy or timed-out
secondary cannot queue overlapping work, and the report records attempted,
completed, busy-skip, and timeout denominators. Both engines remain blind to
expected text; the scorer joins private ground truth only after every OCR call
completes. Ordinary product screen parsing still uses the fast primary path.

The report includes exact and normalized accuracy, character error rate,
latency, throughput, failure classes, category and evaluation-tier breakdowns,
every case result, a confidence-versus-coverage curve, and a worst-failure
contact sheet. The `routine` tier contains 800 ordinary desktop-text cases.
The `stress` tier preserves all 200 numeric-confusable and dense-punctuation
cases, including the fixed `0O1Il|` token and ASCII quote/backtick/operator
distinctions. Tier labels are diagnostic only: stress cases remain in the
overall score and every existing per-category exactness gate. The stress tier
also has a 10% mean-CER ceiling, so tier reporting can add a release failure
but cannot remove one.

The confidence curve is likewise diagnostic only: a high OCR confidence is
not proof that load-bearing characters are correct. The test set covers prose,
UI labels, code, terminals, paths, URLs, identifiers, numeric confusables,
punctuation, and mixed case.
