# AskHR WxO Latency and Streaming Implementation Plan

Status: implementation proposal only; no repository code has been changed

Audience: AskHR backend, widget, platform, QA, and security engineers

Delivery shape: plan-scoped, phased delivery

Estimated size: medium overall; Phase 1 is a small, high-value fix

Risk: S3 because the plan changes the final dispatch-authorization path; it deliberately does not weaken the chat-turn lease

## Outcome

Make an AskHR agent turn feel live as soon as WxO produces useful output, locate the actual remaining latency with safe phase telemetry, and remove avoidable AskHR overhead without weakening authorization, session isolation, transaction safety, or SSE failure semantics.

The delivery is successful when:

- WxO `message.delta` content is forwarded without waiting for a newline or terminal event.
- the widget receives the first AskHR delta within a small, measured forwarding budget after the backend receives the first provider text;
- an agent turn performs exactly one uncached, fail-closed registry authorization read immediately before provider dispatch;
- current remote lease fencing remains intact; its cost is measured before any separate optimization is considered;
- concurrent cold-cache requests share IAM, registry-discovery, and language-taxonomy refreshes where applicable;
- provider time, AskHR pre-dispatch time, forwarding time, and completion tail are separately observable without prompts, answers, profiles, tokens, headers, tool arguments, or raw provider payloads;
- a 100-agent registry and a 100-concurrent-session synthetic streaming workload meet the acceptance gates below; and
- every behavior change can be disabled independently during canary rollout.

## Non-goals

- Do not redesign WxO agents, collaborator topology, prompts, tools, Workday, Apigee, or provider model selection in this delivery.
- Do not promise that AskHR can remove time spent inside WxO, a collaborator, Workday, or a transactional tool. The provider trace shows that upstream work is material.
- Do not bypass `tieredRouter`, reuse a route decision across turns, route directly from the browser, or let dissatisfaction force an agent route.
- Do not move IAM credentials, Workday tokens, Redis data, agent registry data, or provider calls into the widget.
- Do not relax country, region, role, status, platform, token, circuit-breaker, card, continuity, or turn-lease gates.
- Do not remove the fresh dispatch authorization read. The goal is one fresh read, not zero.
- Do not make intermediate provider text, status messages, tool arguments, employee data, or exception text visible or persistent.
- Do not treat the external Python runner as a production client, a security reference implementation, or an end-to-end AskHR benchmark.
- Do not add retries around an action-agent dispatch. An automatic retry can duplicate an irreversible Workday action.
- Do not tune provider timeouts, Redis command timeouts, or routing thresholds without deployed evidence.

## Evidence and confidence

### Measured facts

- The supplied native trace reports **25.136 seconds** total latency for one streamed run.
- Within that trace, the top-level orchestration generation spans about 23 seconds. A collaborator subtree spans about 14 seconds, and its transactional tool subtree spans about 11 seconds, including an approximately 9-second downstream HTTP/execute span. Those spans overlap and must not be summed as independent overhead.
- The supplied direct-provider runs were reported at **about 12 seconds per turn**. They demonstrate that direct streaming is materially faster in that sample, but the supplied direct-run log does not include trustworthy per-event receive timestamps, so it cannot establish time to first byte, time to first text, or the exact AskHR-only delta.
- The direct WxO samples contain real incremental output: one response has 44 provider events, including 14 `run.step.intermediate` events and 24 `message.delta` events; another has 33 events, including 7 intermediate events and 20 deltas. Thus the provider is not returning only one terminal body.
- The direct runner prints each `message.delta` text fragment immediately. It also recognizes intermediate step events, although the captured samples do not contain a usable, bounded status string in the fields the runner checks.

### External-runner non-parity and security gaps

The Python runner is useful only as structural evidence that WxO emits deltas. It is not comparable to the AskHR product path:

- It calls WxO directly with an API key/IAM token. It bypasses the widget, session JWT validation, Redis turn lease/session load, identifier redaction, language resolution, `tieredRouter`, registry discovery and fresh authorization, circuit breaker/outage handling, governed agent context, SSE re-framing/backpressure, continuity persistence, card revalidation, message-index allocation, analytics, and App Service/proxy hops.
- It selects agents from live discovery and accepts a locally chosen persona rather than enforcing AskHR's server-bound employee identity and audience/geo gates.
- It disables TLS certificate verification and suppresses the resulting warnings. That is unacceptable for AskHR or any benchmark used as security evidence.
- Its fallback persona and endpoint are hardcoded, and its interactive output displays identity/agent identifiers. These are local conveniences, not portable configuration patterns.
- It persists full request payloads, context, raw SSE events, assembled answers, routing breadcrumbs, and tool argument/result previews to local JSON. AskHR telemetry must do none of those things.
- It has a process-local IAM cache but no concurrency test, no AskHR cache stack, no lease loss, no browser backpressure, and no proxy buffering measurement.
- The log's response entries reuse the request timestamp and do not timestamp each received SSE event, so event counts are trustworthy but first-event/first-text/inter-delta timing is not.

Any paired benchmark must use the same prompt class, WxO release, agent/environment, new-versus-resumed thread state, region, and time window, while keeping test identities synthetic and logs content-free. A direct result is the provider control; the AskHR result is the full path. Compare distributions, not one turn.

### Current-source facts

- `packages/backend/src/routes/chatMessage.ts` flushes the HTTP headers before upstream work and emits `typing` after the turn lease and bound Redis session are loaded.
- The same route performs control-plane flag and registry reads, possible language analysis, routing, outage checks, context construction, registry authorization, IAM acquisition, and provider connection before provider text can arrive.
- The normal routed-agent and card-owner paths currently resolve a fresh agent before calling `serveAgentAnswer`; `serveAgentAnswer` then performs another uncached `getFreshPublishedAgent` check immediately before dispatch and compares the registry revision. That is two fresh reads in the ordinary successful path.
- `packages/backend/src/services/agents/wxoRunManager.ts` appends provider text to `lineBuffer` and yields only complete newline-terminated lines. A provider delta without a newline can therefore remain invisible until a later newline, `done`, or EOF.
- `wxoRunManager.ts` ignores `run.step.intermediate`; the user sees the already-emitted typing indicator but no changing, safe progress state during this period.
- `serveAgentAnswer` calls `turnLease.assertOwned()` for every provider event. `RedisChatTurnLease.assertOwned()` performs a Redis renewal command, so provider event count currently drives Redis round trips on the hot stream path.
- `getIamToken()` caches successful tokens but has no in-flight single-flight guard. `getPublishedAgents()` and `getSupportedLanguages()` similarly have value caches without coalescing concurrent misses. By contrast, Key Vault and feature-flag reads already use the repository's `coalesce()` helper.
- After provider `done`, AskHR may resolve/stage cards, persist continuity, allocate a Mongo-backed message index, emit reasoning, emit client `done`, and append history. Continuity intentionally completes before client `done`; history intentionally remains after `done` but before lease release.
- `agentFirstTokenMs` starts at the WxO HTTP call and records the first provider text event. It does not expose session/route/context/auth time, provider-header time, provider-first-event time, backend-first-delivery time, per-event Redis cost, or provider-done-to-client-done tail.

### Hypotheses to prove, not assumptions to ship

- **H1 — newline buffering is a major perceived-latency multiplier.** It is certainly capable of delaying output, but the supplied provider samples lack receive timestamps and fragment text must not be copied into this plan. Instrument the first provider text and first downstream delta before quantifying the gain.
- **H2 — pre-provider work explains a material part of the roughly 13-second sample difference between the 25.136-second trace and approximately 12-second direct turns.** The runs are not controlled pairs, and provider work differs, so the difference is directional evidence only.
- **H3 — per-event Redis assertions may add stream latency and Redis load.** The source proves one remote assertion per event, but the supplied runs contain only tens of text deltas. Measure it; do not weaken the lease boundary in this delivery merely because the calls exist.
- **H4 — cache expiry waves cause latency spikes.** Missing single-flight guards make a herd possible; deployed cache-hit/wait/refresh counters are required to prove frequency and size.
- **H5 — the completion tail is user-visible.** The ordered awaits exist, but current telemetry cannot isolate their duration.

Do not present any hypothesis as a measured production root cause. Phase 0 is the prerequisite for prioritizing anything beyond the unambiguous newline-buffering defect.

### Expected improvement, stated honestly

The first release primarily improves **time to first visible text**, not the time at which WxO and Workday finish:

- In the captured no-newline shape, immediate forwarding can expose the first provider text as soon as it arrives instead of withholding all 20–24 deltas until completion. If provider generation occupies 5–15 seconds of a 12–19 second turn, perceived first-text latency can improve by much of that window. This is an estimate until the new clocks produce paired measurements.
- Removing one duplicate fresh Mongo read and coalescing synchronized cache/IAM misses should reduce AskHR pre-provider overhead by tens to hundreds of milliseconds on healthy warm turns and potentially more during expiry waves. Do not advertise a fixed saving before Phase 0.
- These changes do not shorten a slow WxO reasoning loop, collaborator call, Workday request, or business tool. A 12-second provider run may still take roughly 12 seconds to complete even though the employee starts seeing useful text earlier.

The success message for this work is therefore “streaming feels live and AskHR overhead is measured/bounded,” not “every transaction completes in a few seconds.”

## Architecture and security constraints

These are acceptance constraints, not tradeable optimization targets:

- The widget continues to call only `POST /api/chat` on the Express backend over SSE.
- WxO, IAM, Key Vault, Redis, MongoDB, App Configuration, Apigee, and Workday remain backend-owned.
- The existing `context` request property, provider host allowlist, redirect rejection, abort signal, stream budgets, card validation, and opaque card contract remain unchanged.
- The fresh authorization operation must remain an uncached `getFreshPublishedAgent(agentKey, employeeFilter)` read, must require `published`, must reapply environment/geo/audience/token gates, must compare the revision used for dispatch, and must fail closed on Mongo errors or timeouts.
- Exactly one fresh dispatch authorization read is required for every provider dispatch. Discovery cache reads do not satisfy this requirement and cannot replace it.
- Lease loss and client disconnect remain breaker-neutral. They must abort upstream work, suppress later deltas and `done`, and prevent card/continuity/history state commits.
- Continuity remains durable before client `done`; a fast next turn must not resume stale provider state.
- No provider retry is introduced after a request may have reached WxO.
- SSE delta delivery remains backpressure-aware through `writeSseEventAwaitingDrain`.
- Safe progress events do not count as renderable output and cannot convert an empty/failed provider stream into success.
- Telemetry fields are bounded primitives and stable enums. Never record employee text, response text, provider payloads, raw intermediate status, tool arguments/results, tokens, headers, profiles, or raw exception messages.

## Work-PC drift-first startup

The implementation owner must treat this plan as a map to verify against the work-computer checkout, not as a patch script.

1. Read the work-PC `AGENTS.md`, `README.md`, `docs/handbook/engineering/00-codebase-map.md`, `docs/handbook/engineering/01-system-overview.md`, `docs/handbook/integrations/02-wxo-agents.md`, `docs/handbook/operations/07-metrics-and-analytics.md`, and the backend/widget scoped instructions. Architecture records may be absent on a filtered work checkout and are not required.
2. Fetch `origin`; resolve the current `origin/main` commit; create a clean isolated `codex/*` worktree from that commit. Report base commit, branch, worktree path, and clean status before editing. Do not implement in the shared checkout.
3. Re-run targeted symbol searches for every symbol named below. Compare current behavior and tests with the facts in this plan. If a symbol moved, use the current owner. If behavior changed, stop and record the drift before adapting the plan.
4. Check the latest delivery checkpoint at the top of `BUILD-STATUS.md` only. Do not preload old plans or history.
5. Classify the delivery as plan-scoped. Complete and verify one phase at a time; continue after a green phase until the approved delivery is complete or a stated stop condition occurs.
6. Use one implementation owner in the worktree. Obtain fresh correctness, security/privacy, and QA reviews before promoting the integrated authorization and streaming changes.
7. Never copy the supplied evidence JSON or external runner to the work computer. Record only the aggregate, redacted measurements in this plan.

Stop conditions are: a current-source conflict with an architecture invariant; inability to prove one fresh authorization read; inability to preserve lease-loss behavior; new PII in telemetry; a provider event contract not represented by controlled fixtures; or a canary regression against any rollback gate.

## Latency model and measurement points

Use one monotonic request-relative clock for durations. Wall-clock timestamps are useful only for correlation. For agent turns, model:

`client-visible first text = lease/session + control-plane + language + routing + post-route/context/auth + IAM/provider headers + provider think/tool time + AskHR forward`

`client-visible completion = first-text path + provider remainder + completion tail`

Add the following bounded fields to `AgentTelemetry` in `packages/backend/src/types/agentTelemetry.ts`, pass them to `finishAgentExecution()` in `packages/backend/src/services/agents/agentLifecycleLogger.ts`, and add the subset needed for durable percentile reporting to `logMessageExchanged()` in `packages/backend/src/services/ops/analyticsLogger.ts`:

| Field | Definition |
| --- | --- |
| `leaseAcquireMs` | request receipt to turn-lease result |
| `sessionLoadMs` | bound Redis session load/decrypt duration |
| `controlPlaneMs` | parallel feature-flag plus registry discovery duration |
| `languageResolutionMs` | supported-language and optional analysis duration |
| `routingMs` | reuse current `routingDurationMs`; do not create a competing definition |
| `postRouteToDispatchMs` | route resolution to start of the one fresh authorization read, including optional outage/handoff/context work |
| `dispatchAuthorizationMs` | the one fresh registry read plus revision validation |
| `preProviderDispatchMs` | request receipt to start of the outbound provider fetch |
| `iamTokenMs` | `getIamToken()` duration, with separate hit/wait/refresh outcome |
| `providerHeadersMs` | outbound fetch start to accepted response headers |
| `providerFirstEventMs` | provider call start to first valid decoded provider event |
| `providerFirstIntermediateMs` | provider call start to first recognized intermediate event, or null |
| `providerFirstTextMs` | provider call start to first nonempty text; this replaces ambiguity around the current `firstTokenMs` only after dashboards migrate |
| `backendFirstDeltaMs` | request receipt to first successful downstream delta write |
| `providerToBackendFirstDeltaMs` | first provider text receipt to completion of the first downstream write/drain |
| `providerFirstRenderableTextMs` | provider call start to first text containing a non-whitespace Unicode code point |
| `backendFirstRenderableDeltaMs` | request receipt to the downstream write containing the first nonblank/renderable text |
| `providerDoneMs` | provider call start to explicit provider `done` |
| `completionTailMs` | provider `done` receipt to attempted downstream `done` |
| `leaseRemoteCheckCount` / `leaseRemoteCheckMs` | number and total time of explicit remote lease assertions |
| `providerEventCount`, `providerIntermediateCount`, `providerDeltaCount` | bounded counts only |
| `iamCacheOutcome` | stable `hit`, `shared_wait`, `refresh`, or `failure` enum |
| `registryDiscoveryCacheOutcome` | stable `hit`, `shared_wait`, `refresh`, `stale`, or `failure` enum |
| `languageTaxonomyCacheOutcome` | stable `hit`, `shared_wait`, `refresh`, `fallback`, or `failure` enum |

Implementation rules:

- Use `performance.now()` or an injected monotonic clock for interval measurement; retain `Date.now()` only where the surrounding API requires epoch time.
- Record timings even on failure/cancel, with null for phases never reached.
- Keep `operationId`, stable `agentKey`, platform, outcome, phase, error code, cache outcome, counts, and durations. Do not add provider event IDs or request bodies.
- `firstTokenMs` currently means provider-call-to-first-text. During migration, populate both old and new fields from the same clock and document the deprecation; do not silently change its meaning.
- Avoid one log event per provider event. Aggregate counters and milestone timings into the existing terminal lifecycle event.
- Update `packages/backend/src/services/agents/agentLifecycleLogger.test.ts`, `packages/backend/src/services/ops/analyticsLogger.test.ts`, `packages/backend/src/routes/chatMessage.test.ts`, and `packages/backend/src/utils/telemetry.test.ts` as their existing contracts require.

Store an ordered set of monotonic milestones in memory and derive non-overlapping critical-path intervals at terminal logging time; do not add overlapping durations and call the result end-to-end latency. At minimum the successful path records `requestAccepted -> leaseAcquired -> sessionLoaded -> controlReady -> languageReady -> routeReady -> freshAuthorizationStart -> freshAuthorizationEnd -> providerStart -> providerDone -> clientDoneWrite -> socketClosed`. Provider headers/first event/first text and parallel sub-operation durations are annotations, not extra terms in that sum. Pre-provider failures use `requestAccepted -> errorWrite -> socketClosed`; cancellation/disconnect uses `requestAccepted -> abortObserved -> socketClosed`. The evaluator must emit a `pathKind`, the sum of critical-path intervals, measured request-to-close duration, and residual. A missing or negative boundary is a telemetry failure, not zero.

## Phased implementation

### Phase 0 — Instrument the current path

Purpose: create a trustworthy baseline before changing timing behavior.

Expected touched files and symbols:

- `packages/backend/src/types/agentTelemetry.ts` — extend `AgentTelemetry` with milestone timing/count fields.
- `packages/backend/src/routes/chatMessage.ts` — instrument `handleChat`, `serveAgentAnswer`, the single delivery helpers, authorization, and completion tail.
- `packages/backend/src/services/agents/wxoRunManager.ts` — measure IAM, provider headers, first event/intermediate/text, provider completion, and aggregate event counts.
- `packages/backend/src/services/agents/agentLifecycleLogger.ts` — extend the bounded `agent_execution_completed|failed|cancelled` terminal record.
- `packages/backend/src/services/ops/analyticsLogger.ts` — persist the reviewed SLO fields on `message_exchanged`; retain fire-and-forget analytics behavior.
- `docs/handbook/operations/07-metrics-and-analytics.md` and `docs/handbook/integrations/02-wxo-agents.md` — document exact timing definitions and null/failure semantics.

Checklist:

- [ ] Add failing tests for each clock boundary and for null fields on pre-dispatch failure.
- [ ] Use fake timers/injected time; do not add sleeps.
- [ ] Prove telemetry output contains no input, output, context value, raw event, token, header, or exception message.
- [ ] Capture warm-cache and cold-cache dev/QA baselines by platform, route tier, new-versus-resumed provider thread, and text-versus-card turn.
- [ ] Do not tune behavior in this phase.

Gate: at least 100 side-effect-free QA turns are enough to validate field population and phase reconciliation, not p99. Require phase totals to reconcile to end-to-end duration within 5% or 100 ms, whichever is larger. Release percentiles use the larger sample rules under “Initial SLOs and rollback gates.” If timings do not reconcile, fix measurement before proceeding.

### Phase 1 — Forward WxO deltas when received

Purpose: remove the confirmed newline buffer from time to visible text.

Expected touched files and symbols:

- `packages/backend/src/services/agents/wxoRunManager.ts` — `sendWxOMessage` and its `message.delta` branch.
- `packages/backend/src/services/agents/wxoRunManager.test.ts` — current `message.delta text streaming` suite.
- `packages/backend/src/routes/chatMessage.ts` — no routing change; retain `deliverDrain` for each yielded delta.
- `packages/backend/src/routes/chatMessage.test.ts` — end-to-end SSE ordering/backpressure assertions.
- `packages/backend/src/services/config/featureFlags.ts`, `packages/backend/.env.example`, and `docs/handbook/engineering/08-feature-flags-and-config.md` — add the **proposed new** `AskHR:WxOImmediateDeltaStreamingEnabled` / `WXO_IMMEDIATE_DELTA_STREAMING_ENABLED` rollback flag, default off until QA evidence exists.
- `packages/backend/src/scripts/loadTestAgentStreaming.ts` and `packages/backend/package.json` — guarded in-process/deployed-QA streaming executor described below.
- `scripts/qa/measureAgentStreaming.mjs`, root `package.json`, and `package-lock.json` — guarded real-proxy first-renderable-text measurement with an exactly pinned browser-test dependency.

Design:

1. Concatenate all text parts from one provider `message.delta` in order, as today.
2. When the concatenated string is nonempty, update response budget/raw-delta telemetry, then yield that exact string immediately. Set first-renderable-text telemetry only when the accumulated output first contains a non-whitespace Unicode code point. Do not wait for `\n`, add a newline, trim it, or merge it with later provider events.
3. Preserve whitespace-only fragments on the wire but keep the existing rule that only nonblank text satisfies `hasRenderableOutput`.
4. Remove `lineBuffer` only in the enabled path. During rollout, retain the legacy branch behind the flag so rollback changes behavior without deployment.
5. Preserve `SseFrameDecoder`, UTF-8 streaming decode, maximum pending frame size, total response character budget, raw byte/event budgets, aborts, and incomplete-stream handling.

Required tests:

- [ ] A no-newline first delta is yielded before a later event and before provider `done`.
- [ ] Multiple content parts remain ordered and are yielded once per provider delta.
- [ ] UTF-8 split across network chunks is not corrupted.
- [ ] Whitespace is preserved but whitespace-only output does not satisfy renderability.
- [ ] EOF without provider `done` still produces `INCOMPLETE_STREAM`; buffered partial output is not duplicated.
- [ ] A slow downstream waits for `drain` and abort/disconnect stops consumption.
- [ ] Response/event/byte budgets still fail with their existing stable codes.
- [ ] Legacy flag-off behavior is covered until the flag is retired.

Gate: `providerToBackendFirstDeltaMs` p95 no greater than 100 ms and p99 no greater than 250 ms in the deterministic load suite. The correctness oracle is the exact concatenation of provider `message.delta` text in provider order—not the legacy newline-buffered output, which may have dropped blank lines or synthesized one final newline. Assert no missing, duplicated, reordered, trimmed, or invented provider text and no increase in invalid/incomplete streams.

### Phase 2 — Remove avoidable pre-dispatch and hot-loop overhead

#### Phase 2a — Keep exactly one fresh dispatch authorization read

Expected touched files:

- `packages/backend/src/routes/chatMessage.ts` — `handleChat`, card-owner branch, normal routed-agent branch, `serveAgentAnswer`, and `resolveFreshDispatchAgent` use.
- `packages/backend/src/routes/chatMessage.test.ts` — update the existing fresh-authorization tests that currently expect two calls.

Design:

- Pass the cached, already filtered and dispatchable registry candidate into `serveAgentAnswer`.
- Remove the earlier uncached resolution in both normal and card-owner paths.
- Retain the final `resolveFreshDispatchAgent` call in `serveAgentAnswer` after context/feature preparation and immediately before `dispatchAgent`.
- Dispatch only the freshly returned row, not the cached row. Compare its revision with the routed candidate; if it changed, return `AGENT_AUTHORIZATION_CHANGED` and do not call the provider. This avoids combining stale platform configuration with fresh authorization.
- Reassert remote lease ownership after the fresh read, exactly as the current security comment requires.
- Return a typed final-authorization result to the route and preserve the current **route-specific** SSE contract. The card-owner-unavailable path keeps its configured unavailable `delta` plus `done`; the ordinary routed-agent missing/revoked path and final revision/lookup failures keep their current stable recoverable `error` behavior unless product explicitly approves a separate change. Do not let the refactor accidentally unify these outcomes.

Tests must prove exactly one fresh read for normal, card, resumed-thread, and handoff dispatches; zero provider calls on revoked, missing, changed-revision, lookup-failed, lease-lost, suspended, or undispatchable targets; and unchanged employee filter inputs.

#### Phase 2b — Coalesce cache misses

Expected touched files:

- `packages/backend/src/utils/iamTokenManager.ts` — use the existing `coalesce()` pattern around the complete Key Vault plus IAM refresh transaction; expose a test-only reset consistent with other cache owners.
- `packages/backend/src/utils/iamTokenManager.test.ts` — **proposed new test file**, because no colocated test exists in the inspected checkout.
- `packages/backend/src/services/agents/agentRegistry.ts` and `agentRegistry.test.ts` — coalesce list-cache refreshes while preserving bounded stale fallback and the uncached per-dispatch API.
- `packages/backend/src/services/agents/agentRegistryInvalidation.ts` and `.test.ts` — **proposed focused lifecycle/orchestration module** for the `registry_discovery` generation and bounded Pub/Sub invalidation signal; payload contains only `kind`, `epoch`, and `counter`, with no registry rows or employee data.
- `packages/backend/src/utils/redis.ts` and `.test.ts` — sole owner of the generation/channel key names plus Redis read/increment/publish/subscribe primitives and shutdown cleanup; no service constructs a Redis key inline.
- `packages/backend/src/app.ts`, `app.test.ts`, and `app.cleanup.test.ts` — start exactly one subscriber after Redis readiness, stop it before Redis shutdown, and prove a pending reconnect timer cannot revive it after shutdown begins.
- `packages/backend/src/routes/agentAdmin.ts` and `.test.ts` — publish invalidation only after a registry mutation commits; preserve local invalidation.
- `packages/backend/src/services/config/languageConfig.ts` and `languageConfig.test.ts` — coalesce non-strict taxonomy refreshes; strict admin reads remain direct and fail closed.
- `packages/backend/src/utils/coalesce.ts` — reuse as-is unless current tests reveal a contract gap.

Failure semantics:

- Successful values retain existing expiry rules.
- A rejected refresh clears the in-flight entry; the next call may retry.
- IAM never serves an expired token. An invalid/missing token response remains a failure.
- Registry discovery may use only its current bounded last-known-good window after a refresh failure. This is authorization-safe because final dispatch is fresh, but it is not route-equivalent: stale discovery can omit a newly published or better candidate. Emit a degraded metric and keep the current strict stale bound. Fresh dispatch authorization is never cached or coalesced across turns.
- Language non-strict reads keep the existing safe English fallback; strict reads still propagate failure.

Keep the registry discovery TTL at its current five-minute scale; do not turn it into a 24-hour cache. The employee-profile 24-hour cache is a different data set and is not a precedent for routing authority. Represent the Redis version as an opaque `{ epoch, counter }` token, not one permanently monotonic integer. `utils/redis.ts` atomically initializes a missing generation hash with a random epoch and counter zero; a committed mutation increments the counter within that epoch and publishes only `{ kind: 'registry_discovery', epoch, counter }`. After a registry mutation commits, always clear the serving process, then attempt that increment/publish. This cross-instance signal is best effort after the Mongo commit: if Redis fails, emit a bounded operational failure metric and let other instances recover through the existing five-minute TTL plus final fresh dispatch authorization. Do not claim atomic Mongo/Redis invalidation, retry indefinitely in the request, or add an outbox for this latency optimization.

One subscriber lifecycle is owned per App Service process. `app.ts` starts it only after Redis is ready and shuts it down before closing Redis; shutdown cancels reconnect backoff and an `isStopping` guard prevents timer resurrection. On connect/reconnect and every TTL refresh, reconcile a successful primary read into process-local state under the same publication/invalidation guard: advance a higher counter in the same epoch, adopt a different current epoch, and clear any differently tagged cache.

A lower counter in the same epoch indicates restore/rollback, so it needs one extra fence. Call a `utils/redis.ts` compare-and-swap Lua helper that rotates `{oldEpoch, lowerCounter}` to a fresh random epoch with counter zero only if the durable token still exactly matches; otherwise adopt the token returned by the helper. Clear local cache and continue with the next attempt inside the same loader using the winning token. A Pub/Sub message with the current epoch may advance only to a higher counter. A message with a different epoch must first be confirmed by a primary Redis read; an unconfirmed delayed message from an obsolete epoch is ignored. This makes generation loss/restore recoverable while preventing a late pre-restore message from resurrecting an old epoch.

Fence refresh publication against concurrent mutations with a bounded two-attempt loop **inside one `coalesce()` loader promise**: read the durable token, query Mongo, then read the token again. If the two reads differ, reconcile the second token and continue the loop once. If they match, enter the process-local guard, reconcile that authoritative token as described above, and cache the tagged rows only if it is still the process's current observed token. Never recursively call `coalesce()` for the same key from its active loader; that would self-await the in-flight promise. An invalidation observed before publication therefore prevents stale publication; one observed afterward clears it. If token instability remains after the second attempt, leave the local cache empty and use the existing bounded degraded fallback or fail according to the current contract. A missed message or post-commit Redis failure is recovered by an ordinary TTL refresh even without subscriber reconnect. On normal TTL expiry, callers await one coalesced loader; serve bounded stale data only when refresh fails, mark that degraded outcome, and still require the final uncached authorization read. An admin “refresh” button is unnecessary; add an operator command only if deployed evidence shows a real recovery need.

Cold-wave tests must launch at least 100 simultaneous callers and prove one shared IAM exchange, one registry list query, and one taxonomy read per process/expiry wave, plus retry after rejection. Multi-instance registry tests prove the normal committed-mutation signal invalidates every subscriber; invalidation before, during, and between the final token read and local cache publication cannot be overwritten; a missed message is recovered by an ordinary TTL refresh without reconnect; post-commit Redis failure clears locally and remote instances recover no later than TTL; generation-key loss, new epoch, and same-epoch rollback trigger compare-and-swap epoch rotation and permit a later successful refresh; a delayed old-epoch/high-counter message cannot rebase the process; all callers share one bounded loader loop that settles without recursive single-flight/self-await; the signal contains only kind/epoch/counter; shutdown cancels reconnect and closes before Redis; and a refresh failure is labeled stale without bypassing the final authorization read. Assert all generation/channel key construction remains in `utils/redis.ts`.

#### Phase 2c — Measure the lease cost; do not weaken fencing here

Keep the current remote `turnLease.assertOwned()` behavior before every relayed provider event and every irreversible boundary in this delivery. A local time window cannot prove that Redis still names this request as owner after a reset or competing turn. Removing the remote check could therefore allow stale deltas after ownership changes even when all later state writes remain fenced.

Phase 0 already records `leaseRemoteCheckCount` and `leaseRemoteCheckMs`. Use those fields and the 100-concurrent-session harness to determine whether lease checks are materially contributing to latency or Redis saturation. The immediate streaming fix must pass with the existing fencing first.

If deployed evidence later shows that lease checks violate the SLO, create a separate S3 security design. It must prove the same zero-post-loss-output behavior under reset, takeover, network delay, Redis timeout, event-loop stall, multiple App Service instances, and concurrent turns before changing the lease contract. Do not smuggle that redesign into a streaming performance patch, add a local-lease feature flag now, or lengthen the TTL.

#### Phase 2d — Bound and reduce completion tail

Expected touched files:

- `packages/backend/src/routes/chatMessage.ts` — successful provider-terminal block in `serveAgentAnswer`.
- `packages/backend/src/routes/chatMessage.test.ts` — continuity/message-index/done ordering and failure tests.

After Phase 0 identifies the tail, parallelize only independent work. The safe initial candidate is to start `allocateMessageIndex()` and continuity persistence together after provider success and after a remote lease assertion, then await both before `done`. Do not parallelize card validation/token commits with continuity, do not move continuity after `done`, and do not make analytics success a prerequisite for response success. If measurement shows the tail is already within SLO, skip this subphase.

### Phase 3 — Represent safe progress without exposing provider payloads

Purpose: improve the silent period before first text only after Phase 1 is green.

Expected touched files:

- `packages/backend/src/types/sse-events.ts` and `packages/widget/src/types/sse-events.ts` — add the same **proposed new** event variant, tentatively `{ type: 'agent_progress'; stage: 'connected' | 'working' | 'finalizing' }`.
- `packages/backend/src/services/agents/wxoRunManager.ts` — map reviewed provider event classes to bounded stages and deduplicate them.
- `packages/backend/src/routes/chatMessage.ts` — relay progress with the non-delta writer; do not count it as output.
- `packages/widget/src/components/chat-window.ts`, `chat-window.test.ts`, and `packages/widget/src/styles/chat-window.css` — update the existing typing region rather than append chat bubbles.
- `packages/widget/src/i18n/locales/en.json` plus generated catalogs through `npm run i18n:sync` — add short status copy only if product/UX chooses visible words; never hand-edit generated locale files.
- `packages/backend/src/services/config/featureFlags.ts`, `packages/backend/.env.example`, and the feature-flag handbook — add the **proposed new** `AskHR:WxOSafeProgressEnabled` / `WXO_SAFE_PROGRESS_ENABLED` flag, default off.

Rules:

- Map only event type/state to a reviewed enum. Never forward `status_message`, tool name, agent display name, args, results, IDs, or arbitrary provider strings.
- Rate-limit and deduplicate progress transitions. Repeated intermediate events must not cause repeated DOM announcements.
- Progress is nonterminal, nonrenderable, ignored safely by an older widget, and cleared by the first text/card, `done`, `error`, session expiry, abort, or new turn.
- Accessibility: use one polite live region; do not announce animated dots or repeat “working” for every upstream event.
- Existing optional `agent_connection` bubbles retain their meaning and should not be repurposed as raw progress.

Gate: keyboard/screen-reader behavior is stable, no provider text reaches the progress surface, old-widget/new-backend and new-widget/old-backend combinations degrade to the typing indicator, and there is no increase in message bubbles or feedback targets.

### Phase 4 — Documentation, load evidence, and flag retirement

- Update `docs/handbook/engineering/01-system-overview.md`, `docs/handbook/integrations/02-wxo-agents.md`, `docs/handbook/operations/07-metrics-and-analytics.md`, `docs/handbook/engineering/08-feature-flags-and-config.md`, and `docs/handbook/engineering/13-environment-variables.md` only after behavior is verified.
- Update the top of `BUILD-STATUS.md` after each repository-changing phase with fresh commands, SLO evidence, rollout state, and external gaps.
- After one stable production observation window, remove legacy newline buffering and retire the immediate-delta flag. Retire the progress flag after its own stable window; this delivery adds no lease flag. Do not leave permanent dual paths without an owner/date.

## 100-agent load and concurrency model

Use two tests because registry cardinality and live streaming concurrency stress different resources.

### A. Router cardinality

Use the backend-workspace command. The current root package does not expose this script, and the implementation owner must correct any stale runbook example:

```text
ROUTER_LOAD_TEST_TARGET=qa \
ROUTER_LOAD_TEST_ACKNOWLEDGE=non-production \
APP_ENV=qa \
MONGODB_URI='<approved QA URI whose hostname contains qa>' \
MONGODB_DATABASE='<approved QA database name containing qa>' \
AGENT_COUNT=100 QUERY_COUNT=500 CONCURRENCY=10 \
npm run loadtest:router -w packages/backend
```

Run only against an explicitly named disposable dev/QA database. It writes synthetic registry/utterance rows and can leave residue if interrupted. Confirm cleanup. In the inspected source its small generic query list does not target the generated 100 agents, so it can measure execution latency/QPS but **cannot currently prove route accuracy or destination recall**. Before using it as a correctness gate, extend it with deterministic per-agent queries and expected destinations or pair it with the existing router-quality evaluation. Keep performance and finite-corpus correctness as separate reported results. This test still does not exercise session auth, SSE, IAM, WxO, cards, or completion.

### B. Streaming concurrency

Add `packages/backend/src/scripts/loadTestAgentStreaming.ts`, exposed only from `packages/backend/package.json` as `loadtest:agent-streaming`, with two explicit modes plus a separate real-provider canary:

1. `in-process`: deterministic `wxoRunManager`, Redis, Mongo, IAM, and clock adapters for fragmentation, ordering, counters, fault races, and repeatable CPU/memory measurements. All Redis/Mongo/IAM/network failure injection is confined to this mode. Require `AGENT_STREAM_LOAD_TEST_ACKNOWLEDGE=synthetic-only`.
2. `qa-platform`: send signed synthetic sessions through the real QA reverse proxy and multi-instance App Service using isolated QA Redis/Mongo and an approved `custom_http` streaming stub through the actual Custom HTTP provider path. This measures provider-neutral route, SSE, backpressure, cache, Redis, Mongo, and process behavior at high concurrency; it does **not** claim to exercise the WxO adapter. Require `AGENT_STREAM_LOAD_TEST_TARGET=qa`, `APP_ENV=qa`, and `AGENT_STREAM_LOAD_TEST_ACKNOWLEDGE=shared-qa-synthetic-provider`. The stub accepts only fixed server-owned scenarios and no business writes.

For deployed WxO evidence, use a separately registered, side-effect-free QA WxO agent through the normal unmodified WxO hostname allowlist and adapter. Run it at the low bounded volume approved by IBM/platform owners to prove real fragmentation, first-delta forwarding, terminal handling, and new/resumed-thread behavior. Do not repoint the WxO base URL, weaken its host allowlist, or add a production test hook. Deployed dependency-failure testing is limited to separately approved infrastructure drills; request-controlled fault injection never exists outside the in-process harness.

Both modes write one bounded versioned JSON summary to a caller-selected temporary path, contain no messages/cards/identifiers, tag all synthetic keys with a random run ID, clean them in `finally`, verify cleanup, and exit nonzero on test or cleanup failure. Record mode, app/process count, seed, scenario, repetitions, warm-up, admitted/completed turns, nearest-rank percentiles, and fault counts. Store credentials only in the approved environment/secret provider, not the command line or report.

Example invocations:

```text
AGENT_STREAM_LOAD_MODE=in-process \
AGENT_STREAM_LOAD_TEST_ACKNOWLEDGE=synthetic-only \
npm run loadtest:agent-streaming -w packages/backend -- --output <temporary-summary-path>

AGENT_STREAM_LOAD_MODE=qa-platform \
AGENT_STREAM_LOAD_TEST_TARGET=qa \
APP_ENV=qa \
AGENT_STREAM_LOAD_TEST_ACKNOWLEDGE=shared-qa-synthetic-provider \
npm run loadtest:agent-streaming -w packages/backend -- --output <temporary-summary-path>
```

Workload:

- 100 distinct signed synthetic sessions, never 100 turns on one session because the turn lease correctly rejects same-session concurrency.
- 100 published synthetic agent entries for routing cardinality, with turns distributed 80/20 across ten selected side-effect-free agent keys to model hot and cold agents.
- Ramps at 10, 25, 50, and 100 concurrent sessions. After a 100-turn warm-up excluded from percentiles, run three independent repetitions of at least 1,000 completed turns per scenario/flag state; report each repetition and the pooled distribution.
- In-process WxO fixture per turn: `run.started`, 10–20 safe intermediate events, 100 small `message.delta` events with mixed newline/no-newline and split UTF-8 boundaries, then explicit `done`. The QA-platform stub emits its own documented Custom HTTP contract rather than impersonating WxO.
- Separate text-only and card fixtures. Cards must be synthetic and must not call Workday or any live agent tool.
- Scenarios: warm caches; synchronized registry/language cache expiry; IAM expiry; one percent slow downstream clients causing backpressure; client disconnect at 25%, 50%, and after provider done; provider EOF without done. Redis latency/failure, Mongo failure, IAM failure, and transport fault injection run in-process only; QA observes real dependencies or an explicitly approved infrastructure drill.

Record process CPU, event-loop lag, RSS/heap, open sockets, Redis command rate/pool wait, Mongo query/pool wait, IAM refresh count, registry/taxonomy refresh counts, provider fetch count, error codes, and all latency fields. Do not record bodies.

Acceptance:

- exactly one outbound provider request per admitted synthetic turn and none for rejected/lease-lost/revoked turns;
- exactly one fresh authorization read per outbound request;
- one shared refresh per cache/expiry wave for IAM, registry discovery, and language taxonomy;
- remote lease checks and their time are reported explicitly; no lease-safety regression is accepted;
- no unbounded memory trend after steady state and no resource-budget regression;
- assembled text and card ordering match the fixture exactly;
- zero cross-session continuity/card leakage and zero duplicate synthetic transactions;
- the executor-specific gates below pass; no executor may be cited as evidence for a layer it does not exercise.

Evidence is partitioned deliberately:

| Executor | What it proves | Required scale |
| --- | --- | --- |
| in-process WxO adapter | provider-frame fragmentation, exact delta assembly/order, immediate forwarding logic, budgets, aborts, and injected fault races | ramps through 100 concurrent synthetic sessions; apply adapter/forwarding technical SLOs |
| deployed `qa-platform` Custom HTTP stub | provider-neutral route, session/lease, real proxy/SSE/backpressure, registry/cache, Redis/Mongo pools, and multi-instance capacity | ramps through 100 concurrent synthetic sessions; apply pre-provider, completion-tail, resource, and technical-failure SLOs, not WxO-adapter claims |
| real side-effect-free QA WxO canary | actual IBM hostname/stream envelope, IAM/provider connection, first-delta forwarding, explicit terminal, and new/resumed thread behavior | platform-approved bounded volume; report median/p95 unless larger sample rules are met; no 100-concurrency claim |
| Playwright QA browser | first nonblank DOM render, `done`, UI unlock, and whitespace behavior through the real proxy | at least 300 journeys for browser p95; no 99.9% or p99 claim |

Production readiness requires all four relevant gates. The 100-concurrent provider-neutral result cannot substitute for real WxO evidence, and the low-volume WxO canary cannot establish 100-concurrent capacity.

Real WxO canary evidence is still required after the synthetic gate, using only a side-effect-safe QA agent and approved synthetic personas. Do not aim 100 concurrent transactional calls at WxO or Workday without provider/platform authorization.

## Initial SLOs and rollback gates

These are initial engineering gates for already-initialized chat sessions. Revisit them only with a documented QA/production baseline; do not loosen them to make a release green. Synthetic p95/p99 claims require at least 3,000 completed turns per scenario/flag state after warm-up, using nearest-rank percentiles. A live canary with fewer than 1,000 comparable turns may report median/p95 only; p99 is observational until at least 10,000 comparable turns exist. Predeclare exclusions before the run. Client disconnects, deliberate fault injection, and named provider/business outages are reported separately, never silently removed.

Backend SSE-write timing is not browser visibility. Add `scripts/qa/measureAgentStreaming.mjs` and expose `qa:measure-agent-streaming` from the root package. In QA only, Playwright loads the real deployed widget through the actual reverse proxy, starts a side-effect-free turn against the approved QA agent, and records with `performance.now()` the request start, first nonblank text node rendered, widget `done`, and UI unlock. Pin `@playwright/test` exactly in root `devDependencies`, commit `package-lock.json`, and install the matching browser with `npx playwright install chromium` after `npm ci`. Require `ASKHR_BROWSER_TARGET=qa`, matching `APP_ENV=qa`, `ASKHR_BROWSER_BASE_URL`, `ASKHR_BROWSER_AUTH_STATE`, and `ASKHR_BROWSER_ACKNOWLEDGE=synthetic-qa-only`. The auth-state file must be produced by the approved short-lived QA login bootstrap, live in a permission-restricted temporary path, and be removed in `finally`; if that mechanism is unavailable, record this gate as `BLOCKED` rather than bypassing auth. The script writes a bounded JSON summary without text or identifiers and uses the same sample/percentile convention. It is test instrumentation, not a production employee beacon. Require at least 300 browser journeys for p95; do not claim browser p99 without 10,000. Assert whitespace-only deltas never create or retain a blank assistant bubble.

Example browser invocation:

```text
ASKHR_BROWSER_TARGET=qa \
APP_ENV=qa \
ASKHR_BROWSER_BASE_URL='<approved QA URL>' \
ASKHR_BROWSER_AUTH_STATE='<permission-restricted temporary auth-state file>' \
ASKHR_BROWSER_ACKNOWLEDGE=synthetic-qa-only \
npm run qa:measure-agent-streaming -- --output <temporary-summary-path>
```

| Measure | Target |
| --- | --- |
| Headers open | p95 <= 250 ms, p99 <= 500 ms from backend handler entry |
| Typing visible | p95 <= 1 s, p99 <= 2 s from request receipt |
| AskHR pre-provider dispatch | p95 <= 4 s, p99 <= 7 s; report route tier separately |
| AskHR first-delta forwarding | p95 <= 100 ms, p99 <= 250 ms after provider first text |
| Completion tail, text-only | p95 <= 750 ms, p99 <= 2 s after provider done |
| AskHR-added first-text overhead | p95 <= 2 s for warm deterministic routes and <= 4 s across all route tiers, excluding measured provider time |
| End-to-end first visible text | p95 <= 15 s for the controlled QA agent whose direct baseline is about 12 s; compare paired prompts/threads |
| Agent stream technical failure | < 1% excluding deliberate fault injection/client disconnect |
| Incomplete/truncated successful-looking streams | 0; actual `INCOMPLETE_STREAM` rate < 0.1% |
| Cross-session leak or duplicate action | 0; any occurrence is an immediate rollback |

Segment cold/warm caches, new/resumed WxO thread, card/text, route tier, region, platform, app version, and canary/control. Provider time is reported beside AskHR overhead; never hide a slow provider by averaging it with fast local short-circuits.

Rollback immediately if any of these occur: response bytes are missing/duplicated/reordered; invalid/incomplete stream rate rises; Redis/Mongo saturation or process memory rises materially; one-fresh-read authorization is violated; a revoked agent dispatches; lease loss permits output or state writes; progress exposes arbitrary provider content; accessibility regresses; or error/transaction rates worsen beyond the control slot.

## Rollout and rollback

1. Deploy Phase 0 telemetry to dev, then QA, with no behavior flags enabled. Establish baseline and dashboard queries.
2. Enable `WxOImmediateDeltaStreamingEnabled` in dev and QA. Run targeted tests and the 100-agent/load gates.
3. Deploy the same artifact to an Azure App Service canary slot. Route 5% sticky traffic, then 25%, then 50%, then 100%. At each step require at least one normal peak window and the predeclared minimum sample for the percentile being used. Keep control traffic on the same provider/region, route-tier, cache, and new/resumed-thread mix. Stop if p95 forwarding or technical-failure rate regresses by more than 10% versus the concurrent control, even if the absolute SLO still passes.
4. Roll back immediate streaming by setting its App Configuration flag false. If config refresh is unhealthy, route traffic back to the prior slot/image.
5. Roll out cache coalescing after streaming is stable. It should not change user-visible output; rollback by slot/image if dependency failure semantics drift.
6. Roll out safe progress last, behind its own flag, after UX/accessibility approval and mixed-version tests.
7. Do not combine a provider-agent/tool change with an AskHR streaming canary. A controlled comparison requires the same WxO release and downstream tool behavior.

Configuration ownership:

- Runtime production flags belong in Azure App Configuration. Environment variables are local/dev evaluation overrides only and must be cataloged in `packages/backend/.env.example`.
- App Service operational settings still require restart. Widget copy changes require the normal widget build and single-IIFE deployment.
- Record app version/commit, flag revisions, WxO agent release, test agent key, region, new/resumed thread, and observation window. Do not record persona identity or prompts.

## Verification matrix

During iteration, run the narrowest targeted commands for the touched phase, then the full runtime gate:

```text
npm test -w packages/backend -- wxoRunManager.test.ts
npm test -w packages/backend -- chatMessage.test.ts
npm test -w packages/backend -- redis.test.ts
npm test -w packages/backend -- agentRegistry.test.ts
npm test -w packages/backend -- languageConfig.test.ts
npm test -w packages/backend -- agentLifecycleLogger.test.ts analyticsLogger.test.ts
npm test -w packages/widget -- chatStream.test.ts chat-window.test.ts
npm run verify
```

Also run:

- `npm run verify:ai` and `npm run verify:forbidden` for handbook/workflow changes;
- `npm run verify:env` whenever a flag/env reader is added;
- `npm run i18n:check`; if visible progress copy changes, run the approved `npm run i18n:sync` workflow and verify every generated catalog rather than editing them manually;
- `npm run eval:quality -- --help`, then an explicitly named `dev` or `qa` quality evaluation if routing code or route behavior changes. This plan intends no routing behavior change, but removing/relocating registry resolution must still preserve routing fixtures;
- a rendered widget test in a real browser through the actual reverse proxy to confirm frames arrive incrementally and are not re-buffered by App Service/proxy/CDN;
- the synthetic cardinality and streaming load suites; and
- fresh correctness, security/privacy, and QA review evidence for the integrated result.

Manual QA cases:

- first turn/new provider thread and resumed thread;
- deterministic agent route, Tier-2 route, and Tier-3 rerank route;
- multilingual auto detection, pinned language, and language disabled;
- plain text, card-only, text-plus-card, whitespace-only, no-newline, multi-part, malformed event, upstream failure, truncated EOF, timeout, slow client, and disconnect;
- provider authorization revoked or registry revision changed during context preparation;
- cache expiry burst and dependency outage;
- old widget with new backend and new widget with old backend;
- reasoning/connection flags on and off; and
- confirmed agent outage/breaker short circuit.

## Definition of done

- [ ] Every current-source fact and filename in this plan has been revalidated in the work-PC worktree; drift is documented.
- [ ] Phase telemetry safely separates pre-dispatch, provider, forwarding, and completion-tail time.
- [ ] WxO no-newline deltas reach the widget before provider completion.
- [ ] Exactly one uncached, fail-closed authorization read occurs immediately before every provider dispatch.
- [ ] The cached discovery row is never dispatched after the fresh row differs or disappears.
- [ ] Existing remote lease fencing remains effective, and its count/time are visible in the load evidence.
- [ ] IAM/registry/language refresh herds are coalesced with unchanged failure behavior.
- [ ] Safe progress, if shipped, exposes only reviewed enum stages and passes accessibility/mixed-version tests.
- [ ] Targeted tests, `npm run verify`, AI/forbidden/env/i18n gates, and applicable quality evaluation are fresh and green; blocked external gates are explicitly marked `BLOCKED`, never treated as approval.
- [ ] The 100-agent cardinality and 100-concurrent-session streaming gates pass with saved redacted summaries.
- [ ] QA paired direct-provider versus AskHR evidence reports sample size, p50/p95/p99, cache/thread/route segments, provider version, and flags.
- [ ] Canary meets SLOs with no security, duplication, correctness, memory, dependency, or accessibility rollback signal.
- [ ] Handbook pages and the top `BUILD-STATUS.md` describe verified current behavior and remaining external gaps.
- [ ] Rollback flags, prior slot/image, operator, observation window, and flag-retirement owner/date are recorded.

## Evidence-handling note

The inspected evidence files contain employee/persona data, identifiers, request context, raw provider events, and plaintext credentials. This plan intentionally records only event counts, coarse durations, and structural findings. Do not paste, commit, upload, or quote the raw artifacts, and do not reuse the runner's defaults or logging behavior in product code. Rotate/revoke every credential exposed in those files through its owning system, confirm the files remain untracked, and delete local copies only through the approved evidence-retention process.

## Official external references

Use current versions during implementation because IBM behavior is version-sensitive:

- [IBM Runs streaming API](https://developer.watson-orchestrate.ibm.com/apis/orchestrate-agent/chat-with-orchestrate-assistant-as-stream) and [run-event API](https://developer.watson-orchestrate.ibm.com/apis/orchestrate-agent/get-orchestrate-assistant-run-events) — supported top-level streaming events; nested event `data` remains provider-defined.
- [IBM Agent performance guide](https://developer.watson-orchestrate.ibm.com/tutorials/performance/performance-guide-v2-agent) — measure LLM loops, tool calls, model/context cost, p50/p95/p99, and channel-specific behavior.
- [IBM Tool performance guide](https://developer.watson-orchestrate.ibm.com/tutorials/performance/performance-guide-v2-tools) — separate tool-call overhead, tool execution, cold starts, and downstream API time.
