# AskHR WxO routing post-implementation hardening

> **Scope:** a standalone follow-up for four contracts found after the main WxO
> routing and performance implementation was completed. Do not replay
> `ASKHR-WXO-ROUTING-PERFORMANCE-IMPLEMENTATION-PLAN.md` or reopen the older
> AskHR change guide. Preserve the completed routing design and change only what
> this document names.

## How to use this document

Apply this follow-up to the current AskHR work-PC repository. Current source,
tests, `docs/handbook/`, and `docs/runbooks/` remain authoritative. Resolve
current symbols before editing; paths below are navigation hints from the
development-Mac snapshot, not instructions to overwrite newer work.

Before editing:

1. Read the repository `AGENTS.md`, root `README.md`, routing handbook pages,
   security/auth handbook page, and scoped backend/widget instructions.
2. Record the current branch, commit, clean status, and completed routing-plan
   delivery evidence.
3. Trace the implemented exact lane, registry discovery cache/invalidation,
   final dispatch authorization, agent continuation, turn completion, and
   rollout telemetry. If a contract below is already present, add or run the
   named proof instead of creating a duplicate mechanism.
4. Work on a fresh approved branch/worktree. Add failing tests before changing
   behavior. Keep both fast-path modes at `off` or `shadow` while this follow-up
   is incomplete.

## Objective

Keep the completed single-router architecture while closing four narrow gaps:

1. describe employee-attribute authorization freshness truthfully;
2. prevent an invalidated registry refresh from repopulating stale discovery;
3. make a completed agent turn durable before the client can send its follow-up;
4. give operations measurable fast-lane stop criteria.

The desired routing architecture does not change:

```text
approved exact manual authority  ─┐
owner-bound card / strict reply  ─┼─> final fail-closed dispatch checks
existing hybrid retrieval/rerank ─┘
```

## Non-goals

- Do not add another router, domain hierarchy, workflow engine, or provider.
- Do not add per-agent trigger lists or make vector similarity dispatch authority.
- Do not reimplement the completed exact, active-flow, card, language, date,
  streaming, or token-recovery work.
- Do not add a Workday, Entra, Mongo, or Redis round trip to every turn without
  an approved freshness requirement and measured capacity need.
- Do not change session/profile TTLs as an incidental routing fix.
- Do not log messages, card bodies, tool arguments, tokens, employee IDs, or
  other employee data to evaluate routing.
- Do not build a new cache framework. Extend the completed bounded registry
  cache and its existing single-flight/invalidation path.

## Workstream 1 — truthful authorization freshness

### Problem

The final uncached Mongo lookup proves that the selected **agent registry row**
is current: published, enabled, in scope, dispatchable, and at the expected
revision. It does not independently refresh the employee's roles, region, or
country. Those inputs are bound to the authenticated session/profile and follow
the platform's existing session and profile freshness policy.

Calling this simply “fresh authorization” overstates the guarantee. It can lead
future engineers and reviewers to assume that a mid-session employee role or
country change is discovered on every dispatch.

### Required contract

Use this precise term in code comments, telemetry descriptions, tests, and
handbook guidance:

> **Fresh agent-registry authorization against session-bound employee
> attributes.**

Preserve these boundaries:

- The backend re-reads the selected registry row immediately before provider
  dispatch and fails closed on missing, changed, revoked, out-of-scope, or
  unavailable registry state.
- Employee identity remains subject-bound to the authenticated session.
- Employee roles, region, and country use the current platform session/profile
  snapshot; they are not described as real-time directory or Workday reads.
- Provider/business tools retain their own subject and write authorization.
- No browser value and no provider-supplied value becomes authorization input.

Inventory and document the actual maximum age of every employee attribute used
by agent discovery or dispatch:

| Attribute | Current source | Bound/refresh event | Authorization use |
|---|---|---|---|
| Employee subject | authenticated server session | current session lifetime | subject binding |
| Roles | session/profile role derivation | current repository behavior | audience gate |
| Region | subject-bound profile/session | current repository behavior | regional gate |
| Country | subject-bound profile | current repository behavior | country gate |

Do not copy the development-Mac TTLs without verifying the work-PC source. Name
the Security/Product owner who accepts the resulting maximum revocation window.

### Principal-engineer decision

Do not add a per-turn Workday profile or Entra entitlement request as part of
this performance follow-up. At AskHR scale, that would move identity-system
latency and availability into every agent turn and would work directly against
the routing SLO.

If the existing session/profile window is approved, the implementation is a
truthful naming, documentation, telemetry, and test correction only.

If policy requires faster employee-level revocation for a restricted agent,
stop only that agent's live enablement and create a separate security design
using an authoritative revocation/version source already available to AskHR.
Do not invent an employee-version cache without a producer that updates it.
Until that source exists, session invalidation or profile refresh is the honest
operational control. The general exact and continuation lanes may remain live
for agents whose approved authorization window is satisfied.

### Required tests and evidence

- A registry unpublish, scope change, or audience change is rejected by the
  final registry read even when discovery is stale.
- Client-provided role, country, region, or agent key cannot affect dispatch.
- Tests and telemetry use the precise registry-versus-employee freshness terms.
- The handbook states the verified maximum employee-attribute age and the
  approved revocation owner.
- Any stricter restricted-agent requirement has a named authoritative source,
  failure behavior, latency budget, and load proof before live enablement.

## Workstream 2 — invalidation-safe registry refresh

### Problem

Single-flight prevents a refresh herd, but it does not by itself make
invalidation race-safe:

```text
refresh generation 8 starts reading Mongo
  -> mutation publishes invalidation and advances to generation 9
  -> generation 8 query finishes
  -> old result overwrites the now-invalid cache
```

The final registry read still protects dispatch authorization, but stale
discovery can omit a new or better candidate and send an ambiguous request down
the wrong routing comparison. Invalidation must prevent an older in-flight
refresh from committing.

### Required design

Add one monotonic in-process generation to the completed registry-discovery
cache. Keep it in the cache module; do not expose it through public APIs.

```ts
let discoveryGeneration = 0;

function invalidateDiscovery(): void {
  discoveryGeneration += 1;
  cachedSnapshot = null;
  cacheLoadedAt = 0;
}

async function refreshDiscovery(): Promise<Snapshot> {
  const generationAtStart = discoveryGeneration;
  const rows = await readPublishedRegistryRows();

  if (generationAtStart !== discoveryGeneration) {
    throw new SupersededRegistryRefreshError();
  }

  const snapshot = buildSnapshot(rows);
  cachedSnapshot = snapshot;
  cacheLoadedAt = Date.now();
  return snapshot;
}
```

Adapt the shape to the completed cache rather than copying this pseudocode.
The invariants are:

- every local mutation and every accepted cross-instance invalidation advances
  the generation before clearing the matching discovery cache;
- a refresh captures the generation before its database read;
- it commits only if the generation is unchanged after the read and snapshot
  construction;
- a superseded refresh never becomes the stale fallback and never overwrites a
  newer refresh;
- callers retry through the current generation at most once, then use the
  existing safe fallback behavior;
- after an explicit authorization-affecting invalidation, do not resurrect the
  prior generation as last-known-good discovery;
- the completed exact-corpus cache and `registry_discovery` invalidation remain
  separate; do not rebuild exact authority for transient health changes.

Keep one in-flight refresh per generation using the repository's existing
promise-coalescing primitive. An invalidation must detach future callers from
the superseded promise; it does not need to cancel Mongo I/O.

Cross-instance invalidation must reconnect and resubscribe after Redis/Pub/Sub
disconnect. Use bounded backoff and existing client lifecycle patterns. Never
send registry contents through Pub/Sub; send only the existing bounded
invalidation kind/version.

### Required tests and evidence

- Invalidate while a refresh is paused; release the old query and prove it
  cannot populate the cache.
- A caller after invalidation receives the new generation or the documented
  fail-safe result, never the superseded snapshot.
- Concurrent callers in one generation share exactly one Mongo refresh.
- A second invalidation during retry stays bounded and does not loop.
- Registry invalidation does not rebuild the exact corpus unless the mutation
  affects both named domains.
- Subscriber disconnect/reconnect resubscribes and the next invalidation is
  observed on every test instance.
- Cache metrics distinguish hit, refresh, coalesced wait, superseded refresh,
  bounded stale fallback, and refresh failure without recording registry data.

## Workstream 3 — durable continuation before completion

### Problem

A client can react to the terminal `done` SSE frame immediately. If AskHR writes
the assistant prompt, turn history, or `agentContinuation` after `done`, that
next request can miss its deterministic continuation evidence. It can also race
the prior turn lease and receive “still finishing” even though the client was
told the turn was complete.

### Required completion boundary

For a successful agent turn, `done` means all routing-authoritative session
state needed by the next turn is durable and the prior turn no longer blocks a
new request.

Order the successful terminal path as follows:

1. Observe and validate the provider's successful terminal.
2. Finish card resolution/submission-token work and all other state mutations
   fenced by the current turn lease.
3. Build the bounded employee-visible assistant prompt used by the strict reply
   classifier. Do not store raw tool output or reasoning.
4. In one focused, lease-fenced session mutation, append the bounded user/bot
   turn history and store/clear `agentContinuation` and `activeFlow` according
   to the completed routing contract. Terminal clear wins.
5. Complete any remaining mutation that requires ownership. No session,
   routing, card, or provider-continuity state may be written afterward.
6. Release the turn lease with the existing owner-safe idempotent mechanism.
7. Emit the terminal `done` frame and close the response. After release, only
   terminal delivery and fire-and-forget scrubbed operational telemetry are
   allowed.

If the current route structure cannot release before writing `done`, refactor a
small terminal helper shared by successful agent exits. Do not scatter manual
Redis unlocks. The outer `finally` remains an idempotent safety release for
errors and disconnects.

If the durable continuation mutation fails after a provider transaction may
have completed:

- never repeat the provider call or external write;
- do not claim continuation or active-flow ownership was persisted;
- preserve truthful business outcome text already produced;
- emit the repository's bounded continuity-degraded telemetry;
- make the next turn fall back safely to normal routing;
- follow the current error/SSE contract rather than inventing a second terminal
  success frame.

No state write may happen after `done`. This includes history, continuation,
active flow, agent thread IDs, card state, learned routing evidence that still
requires the turn lease, or reset-sensitive state. Move non-critical analytics
off the completion boundary only when it is scrubbed and does not need the
lease.

### Required tests and evidence

- Pause the durable session write and prove `done` has not been emitted.
- On receipt of `done`, immediately submit a strict reply; it acquires the lease
  and takes the correct continuation lane.
- Run the immediate-reply test with and without a WxO thread ID.
- A persistence failure never retries a possibly completed external write and
  the next turn uses the safe normal router.
- Terminal `report_action` clear wins over a pending card/active-flow write.
- Reset/leave/lease-loss races produce no post-loss state mutation or output.
- Exactly one terminal frame is emitted and the response always closes.
- Measure persistence-to-done and lease-release-to-done latency; both must be
  bounded and must not materially change the fast-lane SLO.

## Workstream 4 — measurable live stop criteria

### Problem

“Disable the lane if routing confusion occurs” is not an operational rule.
Production telemetry must identify invariant failures and degradation without
storing employee messages.

### Required rollout contract

Keep the completed centrally refreshed controls and per-agent allowlists. Add a
short runbook table naming the metric, threshold, response, and owner.

| Signal | Threshold | Required response |
|---|---:|---|
| Unauthorized, wrong-scope, collision, stale-revision, or wrong-owner dispatch | 1 confirmed event | Set the affected mode to `off` immediately; incident review before re-enable |
| Post-completion state resurrection, duplicate external write, or post-lease output | 1 confirmed event | Set active-flow mode to `off`; incident review |
| Exact lane does not resolve to its authoritative manual owner | 1 confirmed event | Set exact mode to `off` |
| Fast lane added AskHR latency versus direct provider | p95 above 500 ms for the approved window | Investigate; roll back the responsible phase if sustained |
| General first-turn routing | p95 above 1.5 s or p99 above 3 s for the approved window | Investigate by phase spans; do not disable correctness gates to recover latency |
| Registry refresh superseded/failure or Pub/Sub disconnect | sustained rate above the environment baseline | Alert platform owner; fail safely according to cache contract |

Before live enablement, the owner must record:

- observation window and minimum sample per enabled agent;
- expected peak and twice-peak load model;
- exact-lane decision count, safe-fallback count, and durable-revision failures;
- continuation decision count by bounded intent category;
- current-router counterfactual disagreement during shadow;
- provider/agent mix and language-analysis inclusion;
- the named on-call or platform owner authorized to set each mode to `off`;
- the maximum App Configuration propagation time.

Use row IDs, agent keys from the trusted registry, route-lane enums, bounded
reason codes, timings, and counters only. Do not persist matched utterance text.
An exact decision can be correlated to its authenticated-admin route-row ID;
the row content remains in its existing authorized store.

Do not create an automatic global kill switch based only on a noisy aggregate
percentage. Per-request invariants already fail closed. Alert the named owner
and use the existing centrally refreshed mode control. Security/correctness
events have a zero-tolerance stop rule; latency degradation uses a sustained,
predeclared window to avoid disabling routing during a transient provider event.

### Required tests and evidence

- Synthetic invariant events generate the expected alert without employee data.
- The on-call procedure changes the relevant mode to `off`, and every instance
  observes it within the documented refresh bound without restart.
- `off` restores the existing hybrid path and ignores optional fast-lane state.
- Shadow and off remain behaviorally identical for routing, provider calls,
  session authority, SSE, and widget state.
- The production dashboard separates AskHR routing overhead from WxO/tool time
  and output-buffering time.
- Re-enable requires an incident cause, regression test, DEV/QA proof, owner
  approval, and a new bounded observation window.

## Recommended implementation order

1. Correct authorization terminology and record the employee-attribute
   freshness decision. This prevents the following work from preserving a
   false security claim.
2. Add the registry generation fence and reconnection tests.
3. Move routing/session durability before `done` and prove the immediate-next-
   turn race.
4. Add the live thresholds, dashboard counters, alert ownership, and runbook.
5. Run the complete routing correctness and twice-peak performance gates before
   returning exact or active-flow modes to `live`.

Likely code owners, resolved against current source:

- registry cache/invalidation and its tests;
- Redis invalidation subscriber/client lifecycle and tests;
- agent dispatch/final registry authorization terminology and tests;
- chat agent terminal path, Redis session mutation helper, and tests;
- backend and widget SSE completion handling only if required by the resolved
  completion boundary;
- agent telemetry/lifecycle logging and operations dashboards;
- routing, security/auth, environment/config, and incident-response handbook or
  runbook pages.

## Verification

During implementation, run the narrow tests for each owner. Before completion:

```bash
npm run verify
npm run eval:quality -- --help
```

Run environment-backed routing evaluation only against an approved DEV or QA
target. Use the repository's exact current commands and record the target,
commit, configuration revisions, sample sizes, and results. Do not substitute
unit tests for live App Configuration propagation, Pub/Sub reconnect, Mongo
query behavior, or direct-WxO comparison.

Repeat the named same-utterance direct-WxO versus AskHR journeys after this
follow-up. The fast paths retain the completed targets:

- approved exact and strict continuation pre-provider overhead: p95 at or below
  250 ms and p99 at or below 500 ms when no model language analysis is required;
- added AskHR latency versus direct WxO: median at or below 250 ms and p95 at or
  below 500 ms on the fast lanes;
- general first-turn routing: p95 at or below 1.5 seconds and p99 at or below
  3 seconds;
- zero unauthorized, collision, stale-authority, wrong-owner, duplicate-write,
  or post-lease-output events.

## Definition of complete

This follow-up is complete only when:

- “fresh authorization” is replaced by the precise registry/session-bound
  contract everywhere it matters;
- the verified employee-attribute revocation window and accountable owner are
  documented, with restricted agents held back if they need a missing stronger
  source;
- an invalidated in-flight refresh cannot commit or become stale fallback;
- every instance resubscribes and receives registry invalidation after a tested
  disconnect;
- successful agent history, prompt, continuation, active-flow outcome, and
  other routing-authoritative state are durable before `done`;
- an immediate post-`done` reply acquires the lease and routes correctly;
- production counters, thresholds, alerts, owners, mode-off propagation, and
  re-enable criteria are proven without employee text;
- targeted race/security tests, `npm run verify`, and the available DEV/QA
  routing/performance gates are fresh and green;
- handbook/runbook guidance matches the final code; and
- remaining external evidence is reported explicitly as blocked, not assumed.

## Handoff statement

The main routing architecture is already implemented and remains the approved
design. This document is a bounded post-implementation hardening pass. Do not
reopen completed work unless a failing test proves that one of these four
contracts depends on it. Do not deploy or enable broader live cohorts without
the repository owner's authorization.
