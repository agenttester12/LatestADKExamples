# AskHR WxO routing and performance implementation plan

> **Scope:** outstanding AskHR work only. This document does not repeat or
> reopen the completed token-gate, token-recovery, response-language, registry
> shell, or console work from `ASKHR-CODEBASE-CHANGE-GUIDE.md`.

## How to use this document

Implement this plan against the current AskHR work-PC repository. Current
source, tests, `docs/handbook/`, and `docs/runbooks/` are authoritative. File
names below are navigation hints; resolve current symbols before editing.

Before changing code:

1. Read the repository `AGENTS.md`, root `README.md`, codebase map, routing
   handbook page, agent-integration handbook page, and scoped backend/widget
   instructions.
2. Confirm that the completed token, language, qualified-tool, and card changes
   already present on the work PC remain green. Preserve them; do not
   reimplement them from the older guide.
3. Create a fresh branch/worktree from the approved current base. Record the
   base commit and clean status.
4. Run the existing narrow routing tests and `npm run eval:quality -- --help`
   before changing behavior. Use only approved DEV or QA dependencies.
5. Implement one phase at a time with failing tests first. Do not enable a live
   fast path until the complete quality and load gate passes.

## Objective

Reduce the reported 12–19 second AskHR latency for WxO agent turns and
follow-ups without weakening authorization, routing correctness, card safety,
language behavior, or turn ownership. The design must remain predictable with
100 registered agents and 45,000 employees.

Scale against peak concurrent turns and request rate, not employee count alone.
One hundred registry rows are small; external calls, concurrency, cache-expiry
herds, provider/toolkit capacity, and incorrect routing are the real risks.

## Non-goals

- Do not replace WxO or migrate the agents to Copilot Studio.
- Do not add an agentic workflow engine, domain hierarchy, second router, or
  response cache.
- Do not add a per-agent `exactTriggers` list.
- Do not trust a raw client-provided agent key.
- Do not use a semantic-score threshold as final agent authority.
- Do not cache dispatch authorization for 24 hours.
- Do not make `askhr_platform_tools:stage_card` call AskHR, Apigee, Redis, or
  another HTTP endpoint.
- Do not adopt IBM-native widgets as AskHR's cross-provider card contract.
- Do not weaken the current per-event turn-lease fence in this delivery.
- Do not change completed token recovery or transactional-language behavior.

## Root-cause evidence to confirm first

The copied AskHR source showed several independent latency contributors. Verify
each against the current work-PC branch rather than assuming paths are unchanged.

1. A first-turn agent candidate found by utterance retrieval still proceeds to
   the Tier-3 model reranker, even for an exact, high-confidence phrase.
2. Ordinary typed follow-ups re-enter language analysis, embedding/retrieval,
   and reranking. A WxO `thread_id` preserves provider conversation state; it
   does not currently bypass AskHR routing.
3. Active-flow ownership is persisted only when a provider thread ID is
   captured. If WxO omits it, AskHR loses the direct-continuation signal.
4. Multilingual Auto mode can perform a separate model call before routing,
   including turns that already belong to a pending transaction.
5. Agent dispatch can perform a regional-outage read and two sequential fresh
   registry reads before the provider request.
6. Registry and IBM IAM refreshes may lack single-flight behavior, creating
   bursts at cache expiry.
7. The WxO adapter can receive text immediately but buffer it until a newline or
   `done`, making AskHR appear frozen while the native WxO widget streams.
8. Provider credential time may be excluded from the current first-token
   metric, hiding a real phase.
9. Every streamed provider event can perform a Redis ownership assertion. This
   must be measured, but the fence must not be weakened without a separate
   security proof.
10. The current 100-agent script may call the router directly with generic
    messages, omit full HTTP/Redis/provider/card paths, and measure duration
    without asserting the intended destination.

Do not optimize from static inspection alone. Phase 0 must reproduce the same
utterance through the direct WxO API and AskHR, both warm and cold, and record
the complete phase breakdown.

## Target architecture

Use one router with three bounded entry lanes:

1. **Trusted server hint:** a server-generated, owner-bound UI action may name
   its destination. A raw client agent key is never authority.
2. **Approved exact route:** a unique, administrator-authored manual route row
   may skip embedding, Atlas retrieval, and the reranker after all scope,
   collision, revision, and authorization checks pass.
3. **Existing hybrid route:** every other first turn keeps deterministic
   shortcuts, bounded retrieval, and the constrained Tier-3 reranker.

After an objectively pending transaction is established, ordinary follow-ups
route directly to its owner until completion, exit, revocation, or expiry.
This is “route once, converse directly,” not a warm connection. WxO still
receives a new Runs API request for each turn.

## Phase 0 — measurement and reproducible baseline

Add one correlation ID and monotonic phase spans covering:

- turn-lease acquisition;
- session load;
- App Configuration and registry discovery;
- language analysis;
- route lane;
- exact lookup and durable revision check;
- embedding and Atlas search;
- reranker;
- final authorization and Mongo pool wait;
- provider context construction;
- IBM IAM cache lookup/refresh;
- provider HTTP connection;
- first provider event;
- first nonblank provider text;
- first successful server SSE write;
- first widget render in synthetic-browser tests;
- business-tool completion, stage-card invocation, card emission;
- provider completion, continuity persistence, and total turn.

Call the server metric `requestToFirstSseWriteMs`. An Express `res.write` does
not prove browser paint. Use a synthetic browser journey for visible latency;
add a PII-free production render beacon only if operations needs one.

Never persist message text, card bodies, tool arguments, tokens, arbitrary
provider names, or arbitrary provider display strings in performance telemetry.

Before a load run, commit a PII-free load-model artifact containing:

- forecast peak concurrent turns and requests per second;
- WxO, Foundry, and custom HTTP traffic shares;
- read, card, and write journey mix;
- cold/warm and cache-expiry mix;
- duration;
- minimum completed turns and card deliveries.

Run the same named model at expected peak and twice peak. Keep finite-corpus
correctness separate from statistical production-SLO confidence.

## Phase 1 — immediate output and bounded control-plane work

### Stream WxO text immediately

Update the WxO adapter so every decoded non-empty `message.delta` is yielded as
soon as it arrives. Preserve the streaming `TextDecoder` for split UTF-8, join
multi-content parts in order, keep required whitespace, retain event/byte/text
budgets, and never duplicate a tail at `done`.

Required tests:

- one paragraph without a newline reaches SSE before `done`;
- split UTF-8 and multi-content reconstruct exactly;
- no synthetic newline is inserted;
- no final tail is emitted twice;
- whitespace-only output does not count as a renderable answer;
- truncation and budgets remain fail-safe.

### Collapse duplicate fresh registry reads

Keep one bounded, indexed, fail-closed `agent_registry` read immediately before
employee text, identity context, or token is sent to the provider. Remove the
earlier duplicate only after a race test proves a revocation or revision change
during context preparation is caught by the final read.

Verify a unique `agent_registry.agentKey` index. Capture the DEV/QA query plan
and require indexed equality, not a collection scan. Measure database execution,
connection-pool wait, and p95/p99 at twice peak.

### Coalesce refreshes

Reuse the repository's existing promise-coalescing primitive for registry and
IBM IAM refresh. Concurrent callers at cold start or expiry must await one
refresh, not fan out Mongo or IBM calls.

Retain IAM expiry-minus-buffer behavior. Cache confirmed regional outage state
for only 15–30 seconds, coalesce misses, invalidate after outage mutations, and
use only a bounded last-known-good fallback. Outage or registry-control-plane
failure must not incorrectly penalize the provider circuit breaker.

### Preserve turn fencing

Instrument remote lease-assertion count, cumulative time, and p95. Keep the
current per-event remote check in this implementation. A cadence check can emit
stale data after reset or takeover. Any future change requires a separate
security design proving zero post-loss deltas and atomically fencing every state
mutation.

## Registry discovery cache

Use the existing short discovery-cache shape—approximately five minutes with a
bounded maximum stale window—rather than 24 hours.

Behavior:

```text
fresh snapshot                 -> return immediately
expired, no refresh            -> start one Mongo refresh
expired, refresh in progress   -> await that promise
refresh succeeds               -> atomically replace snapshot
refresh fails within max stale -> discovery may use bounded stale data
refresh fails beyond max stale -> fail closed
```

Cached registry data is discovery only. It is never final dispatch authority.
The fresh pre-dispatch authorization read remains mandatory.

Create one registry/routing invalidation publisher and one dedicated Redis
subscriber connection per App Service instance. Never reuse a subscribed Redis
connection for normal commands. Start and stop it with the application lifecycle.

Publish an invalidation/version signal after every successful mutation that can
change routing or eligibility:

- agent create, sync, publish, update, suspend, resume, delete;
- automatic suspension and recovery;
- manual utterance add/delete;
- seed replacement;
- classifier promotion/deletion;
- knowledge reseed.

Every instance clears its registry snapshot and independently cached exact
index. Publish only a version signal, never registry or utterance contents. The
short TTL is the missed-message backstop. A manual refresh button may exist for
operations convenience but is not a correctness mechanism.

## Phase 2 — approved exact routing without a second trigger list

Do not add `exactTriggers`. Derive a small exact index from the existing
`route_utterances` corpus.

V1 terminal authority is limited to authenticated-admin-created rows with
`source: "manual"` and `confirmed: true`. Seed-generated, classifier-promoted,
and traffic-derived rows remain retrieval evidence only. All confirmed
knowledge rows remain collision blockers.

Use a dedicated `normalizeExactText` on both writes and reads:

```text
Unicode NFKC -> lowercase -> trim -> collapse internal whitespace
```

Build from each row's original `text`. Do not change the existing persisted
`normalizedText`, re-embed rows, strip punctuation, remove words, or introduce a
data migration.

An exact request routes only when:

- one authoritative manual row matches;
- exactly one currently eligible published agent owns it in overlapping scope;
- no confirmed knowledge row has the same normalized text;
- environment, region, country, role, health, context, and token gates pass;
- no other active transaction owns the session;
- the exact snapshot is proven current; and
- final fresh registry authorization succeeds.

Reject conflicting authoritative rows in overlapping scopes at admin write
time. Runtime collision always falls through to the existing semantic router;
never select the first database row.

### Durable exact-corpus consistency

A cached exact index is discovery evidence. Maintain a durable corpus revision
atomically with every mutation that changes exact ownership or adds/removes a
knowledge collision. If the Mongo deployment cannot provide this atomicity,
keep exact routing `off` or `shadow`.

Construct a snapshot consistently:

- Prefer a Mongo snapshot transaction reading the revision and all relevant
  rows.
- Otherwise read revision, read rows, read revision again, and cache only when
  both revisions match. Retry once; mismatch or failure disables the exact lane
  for that request and falls through to semantic routing.

Immediately before exact dispatch, read the durable revision and require it to
match the cached snapshot. This read is separate from fresh registry
authorization. It prevents a missed pub/sub message from preserving deleted,
reassigned, or newly knowledge-conflicted route authority.

Required race tests include mutation during rebuild, missed invalidation,
manual-row deletion/reassignment, and a newly added knowledge collision.

### Centrally refreshed rollout controls

Use existing coalesced App Configuration access, not process environment
variables:

```text
AskHR:FastExactAgentRouteMode = off | shadow | live
AskHR:FastExactAgentKeys = <comma-separated approved agent keys>
```

Validate the closed enum. Missing, malformed, or cold configuration means
`off`. Warm last-known-good behavior may use the existing bounded cache. Prove
that an operator change to `off` reaches every instance within the documented
refresh bound without restart.

Shadow records the proposed destination and avoided calls without changing the
decision. Live is inert for destinations not in the central allowlist. Do not
hardcode agent keys. Enable one agent only after the complete Phase 4 gate.

## Phase 3 — active transactional continuity

Provider thread continuity and AskHR routing ownership are separate:

```ts
interface ActiveFlow {
  agentKey: string;
  expiresAt: string;
  responseLanguage?: string;
}
```

Persist `activeFlow` even when WxO returns no thread ID. Update
`agentThreads[agentKey]` only when a validated provider thread ID exists.

Activate or refresh ownership only after AskHR successfully delivers an
objectively pending interaction:

- a form;
- a confirmation card;
- a selectable table.

Do not activate from text, a read-only table, or a generic choice card. Work
Offsite uses a choice card to show informational reasons. A submitted choice
still routes once through its owner-bound card token without taking ownership
of unrelated later topics.

While ownership is live:

- ordinary typed follow-ups bypass embedding, Atlas, and the reranker;
- authorization and health are revalidated before every dispatch;
- valid card submissions remain one-time and owner-bound;
- terminal `report_action`, explicit leave/reset, cancellation, revocation,
  suspension, or confirmed unavailability clears ownership;
- terminal clear wins atomically over a card observed earlier in the same run;
- a transient provider failure does not create or refresh a flow, but preserves
  a previously pending flow until expiry;
- never automatically repeat an uncertain write;
- `expiresAt` slides to ten minutes only after another successfully delivered
  pending interaction.

Treat legacy `{ activeFlow: { agentKey } }` rows without expiry/evidence as
expired. Ten minutes is a fixed safety window for the expected sub-five-minute
transaction; do not add another tuning setting without operational evidence.

### Locale-safe leave control

The widget must show the trusted registry label for the active task and a
localized “Leave task” or “Start over” control. Its stable internal value is
`leave_task` in every locale.

Map that action to the existing authenticated `POST /api/session/reset`; never
send it as chat text and never route it to WxO. Abort the local stream first.
Extend reset from “409 while active” to a fenced takeover that atomically
invalidates the old turn owner and clears `activeFlow`, `agentThreads`, and
`turnHistory`. The old run must abort upstream and emit no later text, card,
thread, or ownership update.

Expose only `{ active, label, expiresAt }` to the widget. The label comes from
the trusted registry, not a provider event. Send a typed `task_state` SSE event
on change and include the same safe state in authenticated session restoration.
Never expose provider thread IDs or trust the client copy for routing.

Likely owners:

- `packages/backend/src/utils/redis.ts`;
- `packages/backend/src/routes/sessionReset.ts` and tests;
- `packages/backend/src/routes/chatMessage.ts` and tests;
- `packages/backend/src/services/chat/tieredRouter.ts`;
- backend/session SSE types;
- `packages/widget/src/types/sse-events.ts`;
- widget `chatStream.ts`, `sessionReset.ts`, chat-window/orchestration, styles,
  localization catalogs, and tests.

Use centrally refreshed controls:

```text
AskHR:ActiveFlowDirectMode = off | shadow | live
AskHR:ActiveFlowAgentKeys = <comma-separated approved agent keys>
```

Apply the same enum validation, cold `off`, bounded refresh, cross-instance
kill-switch, and allowlist rules as exact routing.

## Employee-local date context required by the updated Work Offsite agent

The updated Work Offsite toolkit consumes `current_date` from AgentRun context
so “today,” “tomorrow,” weekdays, form minimums, and past-date checks do not use
the WxO container's calendar.

Add `current_date` to the Work Offsite registry shell's
`contextProfile.fields`. It is not a connection value or model-supplied tool
argument.

Implementation:

1. At authenticated widget session initialization, collect
   `Intl.DateTimeFormat().resolvedOptions().timeZone`.
2. Validate the IANA name in the backend by constructing
   `Intl.DateTimeFormat` with it. Reject unknown values, controls, and excessive
   length. Store only the validated zone in the existing Redis session.
3. During agent-context construction, compute the calendar date server-side in
   that zone using `formatToParts`, then emit strict `YYYY-MM-DD` as
   `current_date`.
4. Do not infer a zone from country or formatting locale. Do not use server UTC
   as a transactional fallback.
5. If the zone/value is missing, omit `current_date`. The toolkit fails safely
   and asks the employee to refresh instead of guessing.

This is a local calculation, not a network call. The browser-reported zone is a
UX reference rather than authorization—the employee can already select dates,
confirmation remains mandatory, and Workday remains authoritative. Still bind
it to the authenticated session so prompt text cannot change it.

Likely owners are widget session bootstrap/token service, the matching backend
session-creation service, Redis `SessionContext`, agent-context construction,
and focused tests. Deploy backend/widget support before enabling the updated
Work Offsite shell. Existing sessions refresh once to acquire the value.

Test UTC-midnight boundaries, DST zones, malformed/unknown zones, missing
context, exact ISO output, and the value reaching WxO.

## WxO card contract

Keep the current no-network marker tool:

```text
askhr_platform_tools:stage_card
askhr_platform_tools:report_action
```

`stage_card` receives the complete AskHR ChatBlock produced by a business tool.
For example:

```json
{
  "kind": "form",
  "cardId": "offsite-submit-form",
  "title": "Submit a work-offsite request",
  "subtitle": "Choose your dates and reason.",
  "submitLabel": "Review request",
  "cancelLabel": "Cancel",
  "fields": [
    {
      "type": "date",
      "id": "start_date",
      "label": "Start date",
      "required": true,
      "min": "2026-09-04"
    },
    {
      "type": "date",
      "id": "end_date",
      "label": "End date",
      "required": true,
      "min": "2026-09-04"
    },
    {
      "type": "select",
      "id": "reason",
      "label": "Reason",
      "required": true,
      "options": [
        { "value": "Business Reason", "label": "Business Reason" },
        { "value": "Other Reason", "label": "Other Reason" },
        {
          "value": "Remote Flexibility Benefit",
          "label": "Remote Flexibility Benefit"
        }
      ]
    }
  ]
}
```

The Python function intentionally returns only a small acknowledgement. AskHR's
WxO adapter observes the qualified tool invocation, validates the ChatBlock,
stages it under the current turn lease, and emits it to the widget. The Python
container cannot stage into AskHR Redis without outbound network access, and it
must not read Redis directly.

Do not add a standalone HTTP staging tool. That would keep the extra
model-selected tool action while adding Apigee latency, credentials, retries,
rate limiting, and another failure surface.

Backend requirements:

- recognize exact qualified names only;
- require the selected registry agent to declare those names;
- reject bare, suffix-matched, unknown, or unregistered tool names;
- accept documented object and JSON-string argument envelopes;
- validate with the canonical ChatBlock schema;
- retain card/tool/payload budgets and one-time submissions;
- sanitize unknown tool and provider display names before telemetry;
- stage no card after failure, truncation, lease loss, or stopped delivery;
- preserve employee-facing business-tool fallback text through agent behavior;
- maintain a live harmless card canary using a sanitized real SaaS event
  fixture.

Instrument business-tool completion to stage-card observation, run completion,
and widget card emission. The no-op tool itself is small; the additional ReAct
step can still add latency and must be measured.

IBM Python-toolkit capacity is a deployment gate. Inventory the tenant's actual
toolkit quota and concurrency. The referenced IBM Premium documentation states
five toolkits per tenant and a standard five workers across two replicas (ten
concurrent executions per toolkit). Do not assume 100 WxO agents can each own a
separate toolkit. Define intentional sharing and measure the shared
`askhr_platform_tools` queue at twice peak; do not shard without evidence.

## Phase 4 — correctness and 100-agent proof

Replace or extend the current router load script so it covers the full request
path, not only `routeMessage`.

Test 1, 10, 50, and 100 agents with:

- 20–30 realistic positive utterances per agent;
- nearest-agent and same-vocabulary negatives;
- knowledge-versus-action sentinels;
- geography, role, environment, health, revocation, and token gates;
- trusted hint, approved exact, active-flow, hybrid, and knowledge lanes;
- normal follow-ups, form/confirm/selectable-table submissions, informational
  choices, completion, exit, reset, expiry, and topic switch;
- all enabled transactional languages and English fallback;
- cold process, cache/revision/IAM expiry, mutation during refresh, multiple App
  Service instances, Mongo/Redis/provider degradation, client disconnect, and
  toolkit queue pressure;
- read/card/write Work Offsite and EVL journeys;
- destination correctness and unsafe side effects, not duration alone.

Keep semantic Tier 2 to Tier 3 as the universal fallback. Tune Atlas
`numCandidates` only through a recall@10 versus p95 sweep on the real corpus.
Do not copy a generic number from documentation.

## Required tests

### Exact routing

- one eligible manual row avoids embedding/search/rerank;
- generated or traffic evidence never fast-routes;
- overlapping-scope conflicts reject at write time;
- runtime collision falls through;
- knowledge collision blocks exact authority;
- normalization handles Unicode/case/space without migration;
- snapshot revision construction is consistent under concurrent mutation;
- missed pub/sub, deletion, reassignment, and knowledge collision are caught by
  the fresh revision read;
- final registry authorization still runs and fails closed.

### Active flow and reset

- form, confirm, and selectable table activate with or without `thread_id`;
- text, generic choice, and read-only table do not activate;
- informational reason choice does not capture a later PTO question;
- its one-time card selection still owner-routes correctly;
- normal follow-up performs no embedding/search/rerank;
- completion/reset/revocation clears atomically;
- terminal clear wins over an earlier card in the same run;
- transient failure preserves but does not refresh an existing pending flow;
- no uncertain write auto-retries;
- legacy no-expiry state does not direct-route;
- localized `leave_task` calls no router/provider;
- refresh restores only safe task-state presentation;
- concurrent stream plus leave aborts upstream and emits zero later data or
  state resurrection.

### Registry, streaming, and cards

- concurrent cold/expiry callers perform one refresh;
- all mutation owners publish invalidation;
- every instance subscribes and shuts down cleanly;
- unique key/indexed final authorization and pool latency are proven;
- immediate deltas reconstruct exactly;
- exact registered qualified card/action calls work;
- bare/suffix/unregistered names have no side effect;
- malformed/over-limit/duplicate/conflicting cards fail safely;
- stale, replayed, cross-session, and cross-owner submissions reject;
- valid cards render once with a safe text fallback;
- live canary detects stream-envelope or qualified-name drift;
- no WxO card turn calls the private staging endpoint.

### Employee-local date

- valid IANA zone is session-bound;
- unknown/control/oversized values reject;
- server constructs correct ISO date around UTC midnight and DST;
- `current_date` reaches only opted-in Work Offsite context;
- missing value causes a safe refresh message and no write.

## Acceptance targets

These are gates to prove, not current performance claims:

- trusted hint, approved exact, active-flow, and card-submission pre-provider
  overhead: p95 ≤ 250 ms and p99 ≤ 500 ms at twice peak when no model language
  analysis is required;
- general first-turn routing: p95 ≤ 1.5 seconds and p99 ≤ 3 seconds;
- added AskHR latency versus identical direct WxO warm/cold journeys: median
  ≤ 250 ms and p95 ≤ 500 ms on fast lanes;
- exact lane: 100% correct eligible destination and zero unauthorized or
  collision dispatches;
- finite golden corpus: 100% pass for security/knowledge/action sentinels and
  the approved per-agent cases;
- active flow: 100% owner, completion, exit, expiry, failure, and revocation
  behavior in the deterministic suite;
- cards: 100% malformed/stale/cross-owner/replay rejection and at least 99.9%
  valid stage-to-widget delivery in the named load model, excluding declared
  provider/business outages;
- soak: at least 30 minutes at expected and twice expected peak, below 0.1%
  infrastructure errors, no unbounded resource growth, no refresh herd, and no
  correctness regression from one to 100 agents;
- provider inventory fits verified tenant toolkit limits and shared-toolkit
  queue p95/p99 meets the latency budget.

Substantive Auto-language turns must report language-analysis time separately.
Do not claim the 250 ms pre-provider target for a turn that intentionally runs a
model language call until measurement proves it.

## Rollout and rollback

1. Baseline identical direct-WxO and AskHR journeys.
2. Deploy observability and immediate streaming.
3. Verify qualified card controls and live canary.
4. Deploy coalescing, invalidation, short outage cache, and the single final
   registry authorization read.
5. Run exact and active-flow modes in `shadow` only.
6. Complete real-corpus correctness, provider-toolkit inventory, and the full
   100-agent/twice-peak soak.
7. Add one approved agent at a time to each live allowlist.

Emergency rollback sets both centrally refreshed modes to `off`. The existing
hybrid/Tier-3 route remains the universal fallback. No schema migration should
be destructive; new session and telemetry fields must decode as optional.

## Documentation and delivery evidence

Update the current equivalents of:

- routing end-to-end handbook;
- agent builder and WxO integration handbook;
- cards reference;
- performance/incident runbook;
- App Configuration/runbook entries for both modes and allowlists;
- conditional `BUILD-STATUS.md` or equivalent branch/PR evidence, according to
  what exists in the filtered work-PC checkout.

Run narrow tests during each phase, then the repository-required full gates:

```bash
npm run verify
npm run eval:quality -- --help
```

Run `eval:quality` only against an approved DEV or QA target when dependencies
are available. Record unavailable external evidence as `BLOCKED`; do not replace
it with static tests or old logs.

## Definition of complete

Do not mark the AskHR implementation complete until:

- instrumentation identifies the actual latency distribution;
- WxO text streams immediately and exactly;
- discovery refreshes coalesce and invalidate across instances;
- one indexed fresh registry authorization remains at dispatch;
- exact snapshots and pre-dispatch revisions are race-safe;
- active flow activates only from objective pending state and exits safely in
  every enabled locale;
- Work Offsite receives employee-local `current_date`;
- Work Offsite and EVL are in the real routing/evaluation corpus;
- every current ChatBlock kind renders through exact qualified controls;
- the live card canary is green;
- 100-agent full-path quality/load gates pass;
- IBM toolkit inventory and shared capacity are proven;
- both live modes passed shadow review and remain centrally kill-switchable;
- current handbook/runbook/configuration and fresh repository verification are
  complete.

Environment deployment is separate. It is complete only after the approved
App Configuration values are applied, the updated Work Offsite shell is enabled
after widget/backend date support, the agent/toolkit imports resolve, and DEV/QA
live routing, language, card, token, and write smoke tests pass.

## Authoritative external references

- IBM Runs streaming API:
  <https://developer.watson-orchestrate.ibm.com/apis/orchestrate-agent/chat-with-orchestrate-assistant-as-stream>
- IBM Python toolkits:
  <https://developer.watson-orchestrate.ibm.com/tools/toolkits/python_toolkits>
- IBM agent performance guidance:
  <https://developer.watson-orchestrate.ibm.com/tutorials/performance/performance-guide-v2-agent>
- IBM tool performance guidance:
  <https://developer.watson-orchestrate.ibm.com/tutorials/performance/performance-guide-v2-tools>
- IBM agent/tool evaluation:
  <https://developer.watson-orchestrate.ibm.com/evaluate/evaluate>
- IBM widget integration:
  <https://developer.watson-orchestrate.ibm.com/tools/widget_integration>
- IBM known issues:
  <https://developer.watson-orchestrate.ibm.com/release/knownissues>
- MongoDB Atlas Vector Search:
  <https://www.mongodb.com/docs/atlas/atlas-vector-search/vector-search-stage/>

Local/static research does not prove live WxO event shape, Mongo/Redis latency,
App Configuration propagation, Apigee behavior, or tenant toolkit capacity.
Those remain explicit DEV/QA gates rather than assumed facts.
