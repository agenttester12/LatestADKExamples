# AskHR WxO routing and performance implementation plan

> **Scope:** one self-contained handoff for the outstanding AskHR routing,
> performance, date-context, and WxO card work. It does not direct the
> implementer to reopen completed work from `ASKHR-CODEBASE-CHANGE-GUIDE.md`,
> but it verifies the completed contracts before relying on them.

## How to use this document

Implement this plan against the current AskHR work-PC repository. Current
source, tests, `docs/handbook/`, and `docs/runbooks/` are authoritative. File
names below are navigation hints; resolve current symbols before editing.

Before changing code:

1. Read the repository `AGENTS.md`, root `README.md`, codebase map, routing
   handbook page, agent-integration handbook page, and scoped backend/widget
   instructions.
2. Run the prerequisite contract gate below. Preserve contracts that are
   already correct. If a check fails, repair only that named prerequisite before
   starting latency work; do not blindly replay the older guide.
3. Create a fresh branch/worktree from the approved current base. Record the
   base commit and clean status.
4. Run the existing narrow routing tests and `npm run eval:quality -- --help`
   before changing behavior. Use only approved DEV or QA dependencies.
5. Implement one phase at a time with failing tests first. Do not enable a live
   fast path until the complete quality and load gate passes.

### Prerequisite contract gate

The development-Mac snapshot used to prepare this plan still contains old bare
WxO control names and an old token gate. The work-PC repository may be newer.
Treat the following as checks, not assumptions:

- no temporary deployment gate blocks a published WxO agent solely because it
  receives the Workday token in context;
- the current Workday token is sent in the Runs API `context`; on one downstream
  authentication failure the tool resolves a fresh token once by `sessionId`
  and retries once, never loops, and never sends employee identity in the
  resolver request;
- transactional WxO language is limited by the centrally configured WxO locale
  allowlist and otherwise falls back to English;
- the selected registry agent declares the exact qualified control names
  `askhr_platform_tools:stage_card` and
  `askhr_platform_tools:report_action`;
- the WxO adapter accepts only the exact observed and registered control names,
  never bare or suffix-matched names; and
- Work Offsite can receive `current_date` only after the date-context phase in
  this plan is deployed.

Before Phase 1, add or run focused tests proving those contracts. If the live
tenant's sanitized stream fixture has not yet established the exact tool-call
name and argument envelope, keep the affected card-capable agent out of live
traffic until Phase 0 captures it. Never relax to a bare/suffix matcher or infer
the event envelope from the YAML name alone.

Resolve current filenames first, then run at least the equivalents of:

```bash
npm run verify:agents
npx vitest run \
  packages/backend/src/services/agents/agentTokenGate.test.ts \
  packages/backend/src/services/agents/agentContext.test.ts \
  packages/backend/src/services/agents/agentDispatcher.test.ts \
  packages/backend/src/services/agents/wxoRunManager.test.ts
```

The WxO tests must positively prove the verified qualified names and negatively
prove bare, suffix, unknown, and unregistered names. Record the work-PC commit
and fresh result; an old guide or this Mac snapshot is not evidence.

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
- Do not emit any provider output after turn-lease loss. Bounded output
  coalescing is allowed only when ownership is asserted immediately before each
  flush and the race suite proves the same fence.
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

Use one router with two bounded first-turn lanes:

1. **Approved exact route:** a unique, administrator-authored manual route row
   may skip embedding, Atlas retrieval, and the reranker after all scope,
   collision, revision, and authorization checks pass.
2. **Existing hybrid route:** every other first turn keeps deterministic
   shortcuts, bounded retrieval, and the constrained Tier-3 reranker.

Owner-bound card submissions and strict prompt-proven continuations route
directly to the prior agent. An objectively pending transaction additionally
gets visible `activeFlow` state until completion, exit, revocation, or expiry.
Unrelated or substantive free text keeps the existing router. This is bounded
continuation, not a warm connection: WxO still receives a new Runs API request
for each turn.

Do not add a general trusted-hint lane in V1. The current `suggestionId` is
untrusted metadata, and designing a replay-safe destination token is not needed
to solve the measured problem. Existing one-time card submission tokens remain
the only client-carried owner route.

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
- first SSE headers/typing write;
- first renderable server output: the first successful nonblank assistant
  `delta` or valid `card` write;
- first widget render in synthetic-browser tests;
- first observed business-tool call, stage-card observation, Redis stage, card
  resolution, and card SSE write;
- provider completion, continuity persistence, and total turn.

Call the answer metric `requestToFirstRenderableOutputMs`. Exclude SSE headers,
keepalives, `typing`, `language`, `reasoning`, and `agent_connection`: those can
arrive quickly while the employee still waits 12–19 seconds for an answer. Keep
separate `requestToHeadersMs` and `requestToTypingMs` diagnostics. An Express
`res.write` still does not prove browser paint, so use a synthetic browser
journey for visible latency; add a PII-free production render beacon only if
operations needs one.

Never persist message text, card bodies, tool arguments, tokens, arbitrary
provider names, or arbitrary provider display strings in performance telemetry.

Before a load run, commit a PII-free load-model artifact containing:

- forecast peak concurrent turns and requests per second;
- WxO, Foundry, and custom HTTP traffic shares;
- read, card, and write journey mix;
- cold/warm and cache-expiry mix;
- duration;
- minimum completed turns and card deliveries.

For the employee's reported exact utterance, also record its current
`route_utterances` source, confirmation, normalized collision set, effective
scope, chosen lane, and which model/search calls ran. If it is not a confirmed
manual authority row, review it through the existing authenticated admin
utterance workflow before expecting the exact lane; never silently promote it.
The approved literal utterance then becomes a named live-lane acceptance case.

Run the same named model at expected peak and twice peak. Keep finite-corpus
correctness separate from statistical production-SLO confidence.

## Phase 1 — immediate output and bounded control-plane work

### Stream WxO text immediately

Update the WxO adapter so decoded non-empty `message.delta` text no longer waits
for a newline or `done`. Preserve the streaming `TextDecoder` for split UTF-8,
join multi-content parts in order, keep required whitespace, retain
event/byte/text budgets, and never duplicate a tail at `done`.

Do not turn every upstream token into a Redis round trip. Start with direct
passthrough and measure raw event rate plus lease-assertion cost. If the twice-
peak gate shows Redis pressure, coalesce text only at the AskHR delivery boundary
with both a small time ceiling (initially no more than 25 ms) and a small
character ceiling (initially no more than 256 characters). Assert lease
ownership immediately before every flush. The batch must preserve byte-decoded
text exactly and must never flush after loss, reset, disconnect, or terminal.
Tune the ceilings from evidence, not as new App Configuration knobs.

Required tests:

- one paragraph without a newline reaches SSE before `done`;
- split UTF-8 and multi-content reconstruct exactly;
- no synthetic newline is inserted;
- no final tail is emitted twice;
- whitespace-only output does not count as a renderable answer;
- truncation and budgets remain fail-safe;
- batching, if enabled, preserves exact text, stays within its time bound, and
  produces zero output after simulated lease loss; and
- Redis command rate, pool wait, and p95/p99 remain within the named load model.

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
missing or normally expired    -> await one coalesced Mongo refresh
refresh succeeds               -> atomically replace snapshot
refresh fails within max stale -> serve bounded stale discovery with a degraded metric
refresh fails beyond max stale -> fail closed
```

Do not routinely serve stale while a successful refresh runs. Stale discovery
is authorization-safe only because final dispatch revalidates, but it is not
route-equivalent: it can omit a newly published or better candidate and choose
an older still-authorized one. Use stale only for a measured transient Mongo
failure, record its age, and test missed invalidation/new-agent/new-overlap
cases. The refresh stays single-flight, so normal expiry produces one small
registry query rather than a herd.

Cached registry data is discovery only. It is never final dispatch authority.
The fresh pre-dispatch authorization read remains mandatory.

Create one registry/routing invalidation publisher and one dedicated Redis
subscriber connection per App Service instance. Use two bounded signal kinds or
channels: `registry_discovery` and `exact_corpus`. Never reuse a subscribed Redis
connection for normal commands. Start and stop it with the application lifecycle.

Publish the appropriate version signal after every successful mutation:

- `registry_discovery`: agent create, sync, publish, update, suspend, resume,
  delete, automatic suspension, and recovery;
- `exact_corpus`: authoritative manual text/language/confirmation/owner/scope
  changes and confirmed knowledge-collision changes; and
- both kinds when one mutation genuinely changes both candidate discovery and
  exact scope/ownership.

Every instance clears only the cache named by the signal. Suspend/recovery must
not rebuild the exact index; text/language/owner/scope/collision changes must.
Publish only kind/version, never registry or utterance contents. The short TTL
is the missed-message backstop. A manual refresh button may exist for operations
convenience but is not a correctness mechanism.

## Phase 2 — approved exact routing without a second trigger list

Do not add `exactTriggers`. Derive a small exact index from the existing
`route_utterances` corpus.

V1 terminal authority is limited to authenticated-admin-created rows with
`source: "manual"` and `confirmed: true`. Seed-generated, classifier-promoted,
and traffic-derived rows remain retrieval evidence only. All confirmed
knowledge rows remain collision blockers.

Add optional, admin-reviewed `language` metadata to manual route rows. Existing
rows remain valid and require no destructive migration. In Auto mode, an exact
row skips the language model only when that metadata is present and valid. The
row language identifies the utterance; transactional output still intersects it
with the centrally configured WxO locale allowlist and falls back to `en` when
unsupported (for example, an exact Polish route may select the correct agent but
must ask WxO to answer in English). A pinned widget language remains
authoritative. Missing/unreviewed language falls back to the normal analyzer.

Perform the small in-memory exact lookup before starting Auto language analysis:

```text
valid exact route + reviewed/pinned language -> no analyzer or hybrid work
valid exact route + missing language         -> language analysis only
invalid/ambiguous exact route                -> language analysis and hybrid work
```

For a valid exact destination with missing language, start hybrid work only if
its revision/scope/final authorization later fails; do not pay embedding, Atlas,
and reranking speculatively on the common success path. On the hybrid lane, run
independent language and retrieval work concurrently. Never send anything to a
provider until the final route, language, scope, revision, and fresh
authorization checks agree.

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

For the no-language-model fast lane, also require a valid reviewed row language
or a valid pinned language. Otherwise the exact destination may still be used,
but its latency is reported separately because language analysis remains.

Reject conflicting authoritative rows in overlapping scopes at admin write
time. Runtime collision always falls through to the existing semantic router;
never select the first database row.

### Durable exact-corpus consistency

A cached exact index is discovery evidence. Maintain a durable corpus revision
atomically with every mutation that changes exact ownership or adds/removes a
knowledge collision. If the Mongo deployment cannot provide this atomicity,
keep exact routing `off` or `shadow`.

Use one explicit `routing_control` record with
`{ _id: "exact-corpus", revision: Long, updatedAt: Date }`, and
one transaction-owning mutation helper. The helper receives the Mongo session,
performs the authority-affecting row/agent mutation, and increments the revision
in the same transaction. Define this by fields, not by a brittle list of current
routes: every authoritative-row create/update/delete that changes original or
normalized text, reviewed language, confirmation, owner, or row scope must bump
the revision and publish invalidation. The same applies to a confirmed knowledge
row change that creates/removes a collision and to registry scope fields used to
decide overlapping ownership. Transient health, token, and publication-status
changes remain outside the exact index and are fenced by final fresh
authorization; do not trigger a global exact rebuild for them. Non-authoritative
traffic evidence does not increment the revision. Do not swallow a revision or
authoritative-write failure; fail the admin/job mutation atomically and audit
the stable failure category.

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
manual-row deletion/reassignment, text/language edits, scope changes, and a
newly added knowledge collision.
Also assert that every authority-affecting mutation owner uses the transaction
helper and that an unavailable transaction-capable Mongo deployment leaves the
exact lane off rather than partially safe.

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

Provider thread continuity, a prompt continuation hint, and visible
transactional ownership are separate.

### Server-owned prompt continuation

Add a small optional session field that can nominate the immediately previous
agent only for the existing strict deterministic-reply classifier:

```ts
interface AgentContinuation {
  agentKey: string;
  expiresAt: string;
  responseLanguage: string;
}
```

After any successful agent turn, store or replace this hint under the current
lease, even when no card or provider `thread_id` exists. Do not duplicate prompt
text in the field; compare the next message only with the existing immediate
`botLastMessage`/latest bot turn. The hint alone is never routing authority. It
routes directly only when `classifyDeterministicReply` recognizes a strict reply
to that immediate prompt. Otherwise the turn uses normal routing and the stale
hint is cleared or replaced by the new outcome. Clear it on knowledge response,
agent change, reset, leave, revocation, or ten-minute expiry.

Use the effective transactional language (supported WxO locale or English
fallback) for that direct one-step continuation, avoiding Auto analysis. A
substantive reply or explicit language switch is not deterministic and returns
to normal language analysis and routing. This state works with or without a WxO
thread ID and is not displayed in the widget.

In `shadow`, write only an isolated TTL-bounded shadow hint and use it for
counterfactual telemetry; it must not change live routing or session fields.
Test text-question to short-answer journeys with and without `thread_id`, a
chain in which the agent asks a second clear question, expiry, and adversarial
topic/language switches.

Routing precedence is deterministic:

1. a valid consumed card submission token owns its turn, regardless of current
   continuation or task presentation;
2. otherwise a valid immediate `agentContinuation` owns only a strict
   deterministic reply to its prompt; and
3. `activeFlow` may direct free text only when it names the same owner as that
   immediate continuation and the reply is deterministic. In every other case
   it is context/bias only and the normal router decides.

Therefore, if pending Agent A remains visible, the employee substantively
switches to Agent B, and B asks a short question, the short reply routes to B;
submitting A's still-valid card continues to route to A through its owner token.

### Pending transactional ownership

Provider thread continuity and AskHR routing ownership are separate:

```ts
interface ActiveFlow {
  agentKey: string;
  expiresAt: string;
  responseLanguage: string;
  pendingKind: 'form' | 'confirm' | 'selectable_table';
}
```

Persist `activeFlow` even when WxO returns no thread ID. Update
`agentThreads[agentKey]` only when a validated provider thread ID exists.

Activate or refresh ownership only after AskHR successfully writes an
objectively pending interaction to the current SSE response:

- a form;
- a confirmation card;
- a selectable table.

Do not activate from text, a read-only table, or a generic choice card. Work
Offsite uses a choice card to show informational reasons. A submitted choice
still routes once through its owner-bound card token without taking ownership
of unrelated later topics.

Active flow is a routing optimization, never card-submit authority. One-time
card tokens already bind the destination and language. Commit card submit tokens
first, write the card SSE, and only after a successful server write persist
`activeFlow` with the same lease. Then emit `task_state`. If persistence fails,
leave the already delivered card usable through its owner token, record a stable
continuity failure, and use normal routing for later free text. If delivery
fails, do not activate. If the same successful run contains a valid terminal
`report_action`, terminal clear wins and no flow is activated. This ordering
does not claim browser paint; restoration exposes only safe task state.

While ownership is live:

- owner-bound card submissions bypass language analysis and routing using the
  consumed submit token;
- prompt-proven continuations such as a direct answer to the immediately prior
  agent question may bypass embedding, Atlas, and the reranker through the
  existing deterministic-reply classifier;
- substantive or unrelated free text, including a new policy question while a
  form is pending, uses the normal router; active flow may be bounded context or
  bias but is never final authority for that text;
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

Do not use semantic similarity as a direct active-flow authority. Add pending-
form/confirm/selectable-table adversarial cases such as “What is the PTO
policy?” and “I want a different HR task”; neither may be silently sent to the
current transactional agent.

### Language on fast continuations

Store the effective transactional response language in `activeFlow` and
`agentContinuation`: a supported WxO locale, or `en` after unsupported-language
fallback. Card submissions use the language already bound into their one-time
submit token. Pinned user-language turns do not need Auto analysis. A short
prompt-proven continuation reuses the effective continuation language without a
model call.
A substantive Auto-mode text turn still runs language analysis so an explicit
mid-flow language or topic switch works correctly; run independent exact-route
lookup concurrently where safe, but never dispatch before both routing and
language are resolved. Measure analyzer call counts separately and do not claim
the fast-lane target for those substantive Auto turns.

Tests must cover every supported transactional locale, unsupported Polish to
English fallback, pinned language, a short continuation that performs zero
language-model calls, a substantive mid-flow language switch that performs one,
and language preserved through card submission.

### Behavior-neutral shadow state

`shadow` must never write `activeFlow`, `agentContinuation`, `agentThreads`,
user-visible `task_state`, or any field consumed by the current router. If multi-
turn shadow evaluation needs persistence, use separate TTL-bounded
`shadowActiveFlow`/`shadowAgentContinuation` Redis keys read only by
counterfactual telemetry. They contain no provider thread ID or message text and
are deleted at expiry/reset. Prove `off` and `shadow` produce identical route
decisions, provider calls, session authority, SSE, and widget state.

Treat legacy `{ activeFlow: { agentKey } }` rows without expiry/evidence as
expired. Ten minutes is a fixed safety window for the expected sub-five-minute
transaction; do not add another tuning setting without operational evidence.

### Locale-safe leave control

The widget must show the trusted registry label for the active task and a
localized “Leave task” or “Start over” control. Its stable internal value is
`leave_task` in every locale.

Do not overload full-conversation reset. Add an authenticated, rate-limited
`POST /api/session/leave-task` (or the current repository's clearly equivalent
scoped route). It never accepts an agent key. Under a normal acquired turn lease
it reads the trusted `activeFlow.agentKey`, clears `activeFlow` and only that
agent's `agentThreads` entry, clears a same-owner `agentContinuation`, and
preserves other agent continuity and `turnHistory`. It is idempotent when no
task is active.

If another turn owns the lease, return `409` and keep the current conservative
“turn still finishing” behavior. Do not abort the local stream until the server
accepts leave. A forced takeover cannot prove whether an external Workday write
already committed, so it can create an unknown outcome and unsafe retry. The
widget should disable Leave task while it is locally submitting and display the
recoverable 409 message if a race still occurs. Full “Start over” continues to
use `POST /api/session/reset`, clears all conversation state, and likewise never
steals an active write.

Expose only `{ active, label, expiresAt }` to the widget. The label comes from
the trusted registry, not a provider event. Send a typed `task_state` SSE event
on change and include the same safe state in authenticated session restoration.
Never expose provider thread IDs or trust the client copy for routing.

Likely owners:

- `packages/backend/src/utils/redis.ts`;
- `packages/backend/src/routes/sessionReset.ts` and tests;
- a focused `packages/backend/src/routes/sessionLeaveTask.ts` (or resolved
  equivalent), `packages/backend/src/app.ts`, API reference, and tests;
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

The Python function intentionally returns only a small acknowledgement. It does
not stage the card itself. The end-to-end ownership is:

```text
business tool returns complete ChatBlock
  -> agent calls askhr_platform_tools:stage_card(card)
  -> WxO includes the tool call in its Runs stream
  -> wxoRunManager validates ChatBlock and lease-stages it in Redis
  -> onCard records the opaque cardId in chatMessage for this turn
  -> only after a successful WxO terminal does chatMessage consume/revalidate
     the body, create the one-time owner submit token, and write card SSE
  -> widget renders the ChatBlock
```

The Python container cannot stage into AskHR Redis without outbound network
access, and it must not read Redis directly. A body can be staged before the
provider terminal and still be withheld from the employee. Failed, truncated,
or disconnected turns never emit or consume their cards. While the turn still
owns the lease, cleanup deletes its staged bodies and submit tokens. After lease
loss it must not delete possible successor-turn state; unconsumed bodies expire
through the existing five-minute staged-body TTL. Never say the adapter itself
delivers the card to the widget.

Preserve the existing card-resolution order unless a separate approved change
explicitly removes it: consume/revalidate the WxO Redis body first, then try the
current country/region-scoped Mongo card-template fallback. Test both sources
and never let fallback bypass ChatBlock validation or owner-bound submission.

Do not add a standalone HTTP staging tool. That would keep the extra
model-selected tool action while adding Apigee latency, credentials, retries,
rate limiting, and another failure surface.

Backend requirements:

- first capture and redact a real `run.step.delta` tool-call fixture from the
  target tenant; IBM documents the top-level Runs event but leaves nested `data`
  open, so nested `step_details`, qualified event names, and object/string args
  are observed compatibility contracts, not guaranteed IBM schema;
- define exact control constants from that verified fixture, expected to be
  `askhr_platform_tools:stage_card` and
  `askhr_platform_tools:report_action`; never accept bare or suffix matches;
- pass the selected registry `agent.toolNames` and trusted `agent.name` through
  `agentDispatcher` into the WxO run manager;
- require the selected registry agent to declare the exact qualified control
  names and update both repository shells (`work_offsite.json` and
  `evl_agent.json`) plus sync/deploy validation; do not infer trust from a
  provider-supplied tool or display name;
- reject bare, suffix-matched, unknown, or unregistered tool names;
- retain object and JSON-string argument parsing only when the sanitized tenant
  fixture proves both shapes are needed;
- validate with the canonical ChatBlock schema;
- retain card/tool/payload budgets and one-time submissions;
- never persist or display raw unknown tool names or provider display names;
  telemetry uses the trusted registry agent name and a bounded sentinel such as
  `unknown_tool` for rejected calls;
- never deliver or consume a card after failure, truncation, lease loss, or
  stopped delivery; discard pre-terminal state only while the same lease is
  owned, otherwise leave successor state untouched and rely on the five-minute
  stage TTL;
- preserve employee-facing business-tool fallback text through agent behavior;
- maintain a live harmless card canary using a sanitized real SaaS event
  fixture.

### Duplicate, conflict, and terminal-action rules

Keep a bounded per-run set of observed tool-call IDs and a map of parsed cards:

- the same valid tool-call ID with the same qualified name and structurally equal
  parsed arguments is idempotent and consumes no second tool/card budget;
- reuse of one tool-call ID with a different name or arguments is a protocol
  conflict that fails the run;
- the same `cardId` with the same parsed ChatBlock is one stage and one delivery;
- the same `cardId` with a different parsed body is a conflict that fails the
  provider turn and delivers neither version;
- distinct valid cards retain provider order; and
- no event after a provider terminal can stage, report, or deliver anything.

Compare schema-parsed values with the repository's existing small structural
deep-equality approach; do not use raw or parsed `JSON.stringify`, because table
row record keys can arrive in different insertion order. Do not create a general
canonical-JSON framework. Define missing, malformed, and oversized tool-call ID
behavior from the captured tenant fixture: a missing ID may be handled with the
bounded card/name identity only if observed and tested; malformed/oversized IDs
are rejected and never logged raw.

Treat `report_action` as bounded orchestration telemetry, not proof that an
external system committed. Accept it only from the registered exact control
name, with a non-empty `action_type` matching `^[a-z][a-z0-9_]{0,63}$`. During
the stream retain only the bounded validated value as pending. Commit
transaction telemetry and clear active flow only after the run's successful
terminal; a failed/truncated run discards it. Malformed calls neither set
transaction completion nor clear active flow. Never use the marker to auto-
retry or assert the outcome of an uncertain write. If operations later needs
per-agent action allowlists, add them only with evidence—do not add another
registry field in this delivery.

Instrument the first observed business-tool call (or toolkit-owned completion
when that toolkit exposes a safe metric), stage-card observation, Redis stage,
run completion, card resolution, submit-token creation, and card SSE write. Do
not claim business-tool completion from an IBM event the tenant fixture does not
contain. The no-op tool itself is small; the additional ReAct step can still add
latency and must be measured.

Add dedicated fields to `AgentTelemetry` and the lifecycle logger for these
durations and stable failure categories. Never record arguments, card bodies,
tool-call IDs, provider display strings, tokens, or employee text.

### Live contract canary

Do not reuse the existing connection probe; it intentionally rejects tool/card
activity. Add a dedicated DEV/QA card-contract canary, and run it in production
only with an approved synthetic identity and operations owner. It must:

1. invoke a fixed non-actionable journey that produces one harmless ChatBlock
   and no Workday write;
2. observe the exact qualified tool name and accepted argument shape;
3. prove lease-fenced Redis stage, successful provider terminal, one resolve,
   one submit token, and one card SSE event;
4. prove zero `/api/internal/config` or other HTTP staging call;
5. clean up bounded state and store only sanitized event-shape fixtures/metrics;
6. fail on a missing/duplicate/conflicting card or contract drift.

Before enabling live card traffic, the runbook must name the schedule, owning
team, notification destination, and stop threshold. A practical initial stop
condition is any three consecutive canary failures or any validated card-
contract failure during the canary window; tune only after measured baseline.

Likely card owners to resolve against current symbols:

- `packages/backend/src/services/agents/wxoRunManager.ts` and tests;
- `packages/backend/src/services/agents/agentDispatcher.ts` and tests;
- `config/agents/evl_agent.json` and `config/agents/work_offsite.json`, plus
  config/sync/deploy tests;
- `packages/backend/src/routes/chatMessage.ts` and tests;
- `packages/backend/src/utils/redis.ts` and tests only if the existing
  lease-stage/cleanup helpers need a focused change; per-run duplicate/conflict
  detection belongs in the WxO adapter;
- `packages/backend/src/types/agentTelemetry.ts`;
- the lifecycle/analytics logger and any safe operations consumer;
- a dedicated card-canary script, test, and runbook; and
- the WxO, cards, and routing handbook pages.

IBM Python-toolkit capacity is a deployment gate. Inventory the tenant's actual
toolkit quota and concurrency. The referenced IBM Premium documentation states
five toolkits per tenant and a standard five workers across two replicas (ten
concurrent executions per toolkit). Do not assume 100 WxO agents can each own a
separate toolkit. Produce a map of every WxO agent and tool to its business and
shared platform toolkit, tenant tier/quota, forecast concurrency, call duration
and timeout. Measure queue and execution p95/p99 for every shared toolkit—not
only `askhr_platform_tools`—at twice peak. If one exceeds capacity, use measured
evidence to pursue IBM entitlement/capacity, intentionally regroup compatible
tools, or select another already supported provider boundary. Do not shard or
wrap tools speculatively.

### Card cutover and rollback

Deploy compatible backend parsing/allowlisting and repository registry shells
before or with the agent YAML/toolkit. In DEV/QA, first accept the verified new
qualified name while the old production build is unchanged; after the fixture,
negative tests, and canary pass, cut all components together. Do not add a
permanent dual matcher and never accept a bare-name fallback in production.

Card rollback is a build/config rollback, not one of the exact/active-flow mode
switches. Stop rollout on card loss, duplicate/conflict delivery, unknown tool
telemetry, schema rejection regression, or canary failure. Restore the last
known compatible backend + registry shell + agent/toolkit set as one versioned
unit; keep the useful text fallback so the employee is never left with a blank
turn.

## Phase 4 — correctness and 100-agent proof

Replace or extend the current router load script so it covers the full request
path, not only `routeMessage`.

Test 1, 10, 50, and 100 agents with:

- 20–30 realistic positive utterances per agent;
- nearest-agent and same-vocabulary negatives;
- knowledge-versus-action sentinels;
- geography, role, environment, health, revocation, and token gates;
- approved exact, prompt-proven/card continuation, hybrid, and knowledge lanes;
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
- reviewed exact-row language skips Auto analysis, unsupported row language
  falls back to English for WxO, and a legacy row without language uses the
  analyzer;
- snapshot revision construction is consistent under concurrent mutation;
- missed pub/sub, deletion, reassignment, and knowledge collision are caught by
  the fresh revision read;
- final registry authorization still runs and fails closed.

### Active flow and reset

- a successful text-only agent question creates a bounded continuation hint
  with or without `thread_id`;
- a strict short answer uses that owner and language without analyzer/search/
  rerank, while an unrelated or substantive answer uses normal routing;
- knowledge response, agent change, reset, leave, revocation, and expiry clear
  or replace the hint;
- form, confirm, and selectable table activate with or without `thread_id`;
- text, generic choice, and read-only table do not activate;
- informational reason choice does not capture a later PTO question;
- its one-time card selection still owner-routes correctly;
- card submissions and prompt-proven continuations perform no
  language-analysis/embedding/search/rerank;
- substantive or unrelated text during a pending card still uses normal
  language/routing behavior and never silently dispatches to the flow owner;
- pending Agent A, substantive switch to Agent B, then B's strict short-answer
  routes to B while A's unexpired owner-bound card still routes to A;
- completion/reset/revocation clears atomically;
- terminal clear wins over an earlier card in the same run;
- transient failure preserves but does not refresh an existing pending flow;
- no uncertain write auto-retries;
- legacy no-expiry state does not direct-route;
- `shadow` never changes real session state, routing, provider calls, SSE, or
  widget state compared with `off`;
- localized `leave_task` calls no router/provider, preserves conversation
  history and other agent threads, and clears only the active owner when idle;
- Leave task and Start over both return 409 rather than steal a lease from an
  in-flight read, reasoning step, or possible external write;
- refresh restores only safe task-state presentation;
- reset/leave before dispatch, during provider reasoning, during an external
  write, and after commit/before `report_action` never creates an unknown replay
  or state resurrection.

### Registry, streaming, and cards

- concurrent cold/expiry callers perform one refresh;
- all mutation owners publish invalidation;
- every instance subscribes and shuts down cleanly;
- unique key/indexed final authorization and pool latency are proven;
- immediate deltas reconstruct exactly;
- exact registered qualified card/action calls work;
- bare/suffix/unregistered names have no side effect;
- malformed `report_action.action_type` cannot set completion or clear flow;
- identical tool-call replay is idempotent without a second budget charge;
- the same tool-call ID with changed name/args and the same card ID with a
  different body across different call IDs both fail with no delivery;
- table rows with equivalent keys in different insertion order deduplicate;
- distinct cards preserve order; missing/invalid/oversized call IDs follow the
  sanitized tenant-fixture contract; no post-terminal tool/card event acts;
- malformed/over-limit/distinct-card overflow fails safely;
- failure, truncation, and disconnect clean state while still owner; lease loss
  never deletes a successor turn's same-cardId state and stale bodies expire in
  five minutes;
- Redis-first and country/region Mongo-template fallback resolution both retain
  validation and one-time owner submission;
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

- approved exact, prompt-proven continuation with or without `activeFlow`, and
  card-submission pre-provider overhead: p95 ≤ 250 ms and p99 ≤ 500 ms at twice
  peak when no model language analysis is required;
- general first-turn routing: p95 ≤ 1.5 seconds and p99 ≤ 3 seconds;
- added AskHR latency versus identical direct WxO warm/cold journeys: median
  ≤ 250 ms and p95 ≤ 500 ms on fast lanes;
- exact lane: 100% correct eligible destination and zero unauthorized or
  collision dispatches;
- finite golden corpus: 100% pass for security/knowledge/action sentinels and
  the approved per-agent cases;
- active flow: 100% owner, completion, exit, expiry, failure, and revocation
  behavior in the deterministic suite;
- cards: 100% malformed/stale/cross-owner/replay rejection; for the 99.9%
  valid stage-to-widget reliability target, predeclare the denominator and
  outage-classification owner and run at least 3,000 independent valid journeys
  with zero platform-attributable failures for a one-sided approximately 95%
  upper failure bound near 0.1%; otherwise label the result an observation, not
  statistical proof;
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

Maintain this phase rollback matrix in the runbook:

| Phase | Stop condition | Rollback | Compatible state |
|---|---|---|---|
| Measurement | telemetry logs unsafe/raw values or adds material latency | disable/remove the new spans | no routing state change |
| Streaming/control-plane | text reconstruction/card loss, auth regression, Redis or Mongo pool regression | roll back the canary/slot build | old and new optional telemetry fields decode |
| Exact route | any confusion, revision mismatch, or unauthorized dispatch | centrally set exact mode `off` | hybrid router remains authoritative |
| Active flow | topic capture, language error, unsafe leave/reset, or state resurrection | centrally set active mode `off`; ignore optional active/shadow fields | card owner tokens still work |
| Date context | invalid/missing date reaches a write or widget/backend contract fails | keep Work Offsite shell disabled and roll back widget/backend together | old sessions omit the optional zone/date |
| Cards | canary, delivery, dedupe, schema, or tool-identity failure | restore last compatible backend + registry + agent/toolkit set | text fallback remains usable |

Use deployment slots or the repository's bounded canary mechanism for runtime
changes. Record the exact artifact/config action and owner before each phase;
“set both modes off” is not rollback for streaming, cards, reset semantics, or
date context.

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
- prompt continuation works without a provider thread ID and grants authority
  only to strict deterministic replies;
- active flow activates only from objective pending state, directly routes only
  owner-bound/prompt-proven continuations, stays behavior-neutral in shadow, and
  exits safely without stealing an in-flight write in every enabled locale;
- Work Offsite receives employee-local `current_date`;
- Work Offsite and EVL are in the real routing/evaluation corpus;
- every current ChatBlock kind renders through exact qualified controls;
- the qualified-name/backend/registry prerequisite gate is green on the actual
  work-PC branch, not inferred from this development-Mac snapshot;
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
- IBM toolkit naming and agent configuration:
  <https://developer.watson-orchestrate.ibm.com/tools/toolkits/overview>
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
