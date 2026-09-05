# AskHR Provider-Neutral Card Delivery

Status: proposed implementation plan; no runtime change has been made

Audience: AskHR backend, agent-platform, security, SRE, and agent-tool engineers

Delivery class: `S3` (authenticated cross-boundary callbacks, employee workflow state, Redis concurrency, and transactional telemetry)

Estimated size: large for the complete provider-neutral migration; medium for the first WxO callback release when existing Custom HTTP v1 remains unchanged

Scope: plan-scoped. Once approved, implement and verify each phase in order; stop only at a named external gate or failed safety check.

## 1. Outcome

Replace WxO card and action detection from streamed model tool calls with a deterministic callback made by the **same business tool that creates the card or knows the business outcome**. A model must no longer make a second `stage_card` or `report_action` call.

The stable flow is:

```text
AskHR opens a turn and issues a one-turn deliveryOperationId
  -> business tool produces a card
  -> that same tool POSTs the card callback and awaits its acknowledgement
  -> CardDeliveryService atomically validates and appends it to the turn's Redis inbox
  -> business tool returns to its provider
  -> provider reaches an explicit successful completion
  -> AskHR freezes and reads the turn-scoped card slot
  -> AskHR validates/prefills cards and commits one-time submission policies
  -> AskHR emits {type:"card", card, submissionId} over the existing /api/chat SSE
  -> the existing widget renders the ChatBlock
  -> AskHR emits the existing done event
```

This order is a contract, not an implementation hint: **card callback -> Redis -> successful provider completion -> SSE -> widget rendering**. A card is never emitted before the provider succeeds. A card callback received after finalization starts is rejected and is never carried into another turn. An action report follows its separate bounded Mongo evidence contract and may be accepted after finalization without changing card or session state.

The steady-state provider modes are:

| Platform | Card-delivery mode | Callback network path | Provider completion | Action outcome |
| --- | --- | --- | --- | --- |
| `wxo` | `callback` | WxO business tool -> Apigee -> AskHR | explicit WxO `done` | same business tool callback |
| `custom_http` v1 | `legacy` | existing internal stage endpoint plus opaque provider card signal | current validated SSE/JSON terminal | current contract, unchanged in the first release |
| `custom_http` v2 | `callback` | approved internal workload -> direct private AskHR ingress | valid SSE `[DONE]` or fully validated JSON EOF | same business service callback |
| `foundry` | `none` | no delivery runtime or card ingress | Responses API completion | no card contract; action reporting remains unsupported until explicitly designed |
| future provider | must declare one of the above modes | must name an authenticated ingress | must define an unambiguous success terminal | must define deterministic evidence |

ChatBlock remains the only card schema. The browser-facing SSE and widget renderer do not change. No DirectLine, Bot Framework, Adaptive Cards, provider-native widgets, polling, Service Bus, or workflow created solely to transport cards is introduced.

## 2. Why the current contract must change

Current source and handbook behavior is internally consistent but has an unreliable provider dependency:

- `wxoRunManager.ts` parses `run.step.delta`, recognizes a model-selected `stage_card` call, validates `args.card`, writes `cardstage:*`, and invokes `onCard(cardId)`. It similarly treats intercepted `report_action` as successful transaction telemetry.
- `/api/internal/config` implements `stage-card`, but it is described as a backup. Its `report-action` branch is deliberately a no-op because the stream is considered authoritative.
- `chatMessage.ts` queues card IDs and resolves them only after the run manager yields a successful `done`. It then commits owner-bound one-time submission tokens and emits the full cards.
- `custom_http` separately stages a body and signals only an opaque ID in provider SSE/JSON. Foundry is text-only.
- Redis protects existing staging with a turn-lease-derived capability, but the staging key is session/card based rather than an explicit turn inbox.

The supplied agent packages make the weakness concrete. Business tools already build the complete card and return it. A separate, connection-free `askhr_platform_tools:stage_card` exists only so the model can make another tool call that AskHR hopes to observe in the WxO stream. `report_action` has the same extra-model-step shape. In the supplied `wxo_logs.json`, the two captured runs contain normal `run.started`, `run.step.intermediate`, `message.delta`, completion, and `done` events but **zero `run.step.delta` events, tool calls, control-tool names, arguments, or card bodies**. The current AskHR interceptor therefore had no card payload to stage. That proves failure for these captures, not a universal IBM event guarantee.

The same runner capture shows an empty outbound context object because the standalone runner did not populate AskHR context. It does **not** prove that AskHR's production request omitted its configured context variables. Verify the AskHR path independently by logging only a bounded allowlisted list of context key names/presence booleans in dev/QA and by a tool canary that returns presence booleans—not values. Never use the raw runner as a context-parity test. The large execution artifact also demonstrates that provider traces can contain sensitive employee and credential material; raw provider logs are therefore not acceptable runtime evidence or fixtures.

This proposal moves the side effect to the code path with authoritative knowledge:

- a read/validation business tool knows the exact card it returned;
- a write business tool knows whether the operation completed, failed, or became ambiguous;
- the provider model is responsible only for choosing the business operation and presenting safe text, not transporting UI state or asserting success.

The existing current-state docs are not “wrong” before implementation. This is an intentional contract replacement; source, tests, handbook, architecture records, and agent packages must move together so they do not describe two truths.

## 2.1 Work-PC drift-first startup

This document is the complete implementation handoff. It does not depend on an older plan having been applied.

1. Read the work-PC `AGENTS.md`, `README.md`, `docs/handbook/engineering/00-codebase-map.md`, `docs/handbook/engineering/01-system-overview.md`, `docs/handbook/engineering/04-security-and-auth.md`, `docs/handbook/engineering/10-api-reference.md`, `docs/handbook/integrations/02-wxo-agents.md`, `docs/handbook/integrations/03-foundry-and-custom-http.md`, and `docs/handbook/integrations/05-cards-reference.md`.
2. Before touching backend or widget files, read `.github/instructions/backend.instructions.md` and `.github/instructions/widget.instructions.md`. Follow any current scoped instructions that replaced those paths.
3. Fetch `origin`, resolve current `origin/main`, and create a clean isolated `codex/*` worktree. Report base commit, branch, worktree, and clean status before editing.
4. Re-find every named symbol/file with `rg` or the available semantic navigator. Compare the current source/tests with this plan. If behavior, schema, key names, protocol versions, or ownership changed, stop, record the drift, and adapt this plan explicitly before coding; do not force stale line assumptions onto current code.
5. Read only the latest `BUILD-STATUS.md` checkpoint. Architecture records under `docs/architecture/` are optional rationale and may be absent on filtered work-PC checkouts; they are updated only when present and applicable, never a completion blocker.
6. Use one implementation owner and phase-scoped diffs. Keep existing Custom HTTP v1 behavior green until a specific registration is intentionally migrated.

Immediate evidence hygiene: the supplied raw logs contain credentials and employee data. Do not copy or commit them to the work PC. Rotate/revoke every credential exposed in those artifacts through its owning system, confirm the files remain untracked, and retain only sanitized aggregate findings.

## 3. Decisions and invariants

### 3.1 One delivery service, two authenticated ingress adapters

Create `CardDeliveryService` as the only code allowed to create, append, finalize, or discard a turn delivery inbox. Both ingress paths normalize to the same service input:

1. **Gateway ingress**: `POST /api/internal/card-deliveries`, reached by WxO business tools through the approved Apigee product. Apigee authenticates the originating workload; AskHR validates the forwarded workload identity with a security-approved cryptographic mechanism.
2. **Private workload ingress**: `POST /api/agent-callbacks/card-deliveries`, reached directly by an approved Custom HTTP workload using managed identity (or another approved workload credential). It is not a browser route and is not authenticated with a widget session token.

The route handlers validate transport/authentication, derive a `CardDeliveryPrincipal`, parse the versioned command, call the service, and map its typed result to HTTP. They contain no Redis/card business logic. If infrastructure ultimately provides one URL with two authentication schemes, retain two middleware adapters and one service; do not duplicate service behavior.

The two paths exist because WxO is outside the network boundary and must traverse Apigee, while an internal Custom HTTP workload should not take an unnecessary external gateway hop. They do **not** create different card semantics.

### 3.2 One-turn correlation, not a model-held secret

AskHR creates and pins these after acquiring the chat-turn lease and before provider dispatch:

- `deliveryOperationId`: a cryptographically random UUID generated by AskHR for this single provider attempt.
- a fixed callback mode known from the provider capability registry.

The operation ID is a short-lived correlation handle, **not authentication and not a secret capability**. It is never supplied by the widget, selected from an agent context profile, accepted from employee text, interpolated into instructions, persisted in provider conversation memory, or passed as a model-chosen business-tool argument. Possessing it without the separately validated workload identity grants nothing.

The internal dispatch type carries a separate `deliveryRuntime` object. Each provider adapter must prove how trusted business code obtains the operation ID and ignores any model-supplied alternative:

- WxO passes `delivery_operation_id` in run context. The business tool reads it from `AgentRun.request_context`; it is not a function argument. IBM documents run context as agent-accessible, so the design does not pretend this value is tool-only or rely on its secrecy. Do not interpolate it into instructions or output it.
- Custom HTTP v2 receives a top-level `runtime` block distinct from `message`, minimized employee `context`, and `thread_id`. The provider implementation must keep `runtime` out of model input and expose it only to trusted business code.
- Foundry receives no delivery runtime.

The live tenant gate must prove that the exact operation ID sent by AskHR is available to `AgentRun.request_context` for the invoked tool. If it is missing or changed, callback mode remains off. Do not put a reusable secret, workload credential, session token, callback ticket, or turn-lease owner in WxO context.

The callback must pass workload authentication. `CardDeliveryService` then resolves the operation record and requires its stored workload, provider, agent, mode, and absolute expiry to match. For a card entry it additionally requires `OPEN` and the same live lease owner. For an action report it deliberately does not require `OPEN` or the current lease: a known unexpired workload-matched operation may record durable Mongo evidence after finalization/lease rotation, but that callback may never mutate card or session state. The body cannot grant or override any of these fields.

### 3.3 Scoped workload authentication

Normalize successful ingress authentication to:

```ts
interface CardDeliveryPrincipal {
  provider: 'wxo' | 'custom_http' | string;
  workloadId: string;             // bounded registry identity, not a free-form header
  allowedAgentKeys: readonly string[];
}
```

The request payload may not grant itself an `agentKey`. `CardDeliveryService` resolves the expected provider, workload, agent, and session from the server-created turn record, then requires the principal to match.

The present `api-key-internal-agents` key is shared and establishes only `clientId = internal-agents`; it cannot prove which provider workload or registry agent called. It is insufficient for this endpoint. Implement these two explicit adapters:

- **WxO/Apigee:** each IBM business toolkit authenticates to Apigee with its own scoped OAuth client credential stored in the toolkit connection. Apigee maps that client to a registered `workloadId`, then creates a per-request short-lived signed JWT for the AskHR callback audience. Require an approved asymmetric algorithm, pinned issuer/audience, expiry no greater than two minutes, unique `jti`, HTTP method/path, and SHA-256 body digest. AskHR validates signature/JWKS, claims, and body binding, then atomically claims the replay key with `SET <replay-key> 1 NX PXAT <assertion-expiry-plus-bounded-clock-skew>` before constructing the principal. Reject the request if Redis cannot establish that expiring replay claim. Never create a replay key without an expiry. Key rotation must accept current+next keys for a bounded overlap.
- **Custom HTTP v2 private ingress:** require an Entra managed-identity app-only access token for the AskHR callback audience. Validate it with the existing Entra verifier conventions plus the approved tenant, `idtyp = app`, absence of delegated `scp`, and one dedicated application role such as `AskHR.AgentCallback`; require assignment and reject delegated tokens even when `azp/appid` belongs to an allowlisted client. Map immutable `azp/appid` server-side to a registered `workloadId` and allowed agent keys. Do not accept a caller-provided workload name.

If the enterprise gateway cannot create the per-request signed assertion, stop and obtain a security-approved equivalent that provides cryptographic original-workload identity and request binding. Do not silently fall back to the shared key.

Do not trust an unsigned `X-Workload-Id`, a body field, source IP, or Apigee reachability as identity. Do not reuse a Workday bearer, widget JWT, IBM IAM token, Custom HTTP outbound credential, or the shared internal-agent key as the new principal.

### 3.4 Explicit registration- and attempt-level capability mode

Replace the boolean `PLATFORM_SUPPORTS_CARDS` with an exhaustive typed provider capability registry plus a registration-owned protocol/mode. The selected agent's **fresh dispatch row** pins the effective mode into the operation record for that attempt; it never changes mid-turn. For example:

```ts
type CardDeliveryMode = 'legacy' | 'shadow' | 'callback' | 'none';

interface PlatformCapabilities {
  supportedCardDelivery: readonly CardDeliveryMode[];
  ingress: 'apigee' | 'private_workload' | 'none';
  runtimeTransport: 'run_context' | 'protocol_runtime' | 'none';
  successfulTerminal: 'wxo_done' | 'custom_sse_done_or_json_eof' | 'foundry_response';
}

const PLATFORM_CAPABILITIES: Record<AgentPlatform, PlatformCapabilities> = {
  wxo: { supportedCardDelivery: ['legacy', 'shadow', 'callback'], ingress: 'apigee', runtimeTransport: 'run_context', successfulTerminal: 'wxo_done' },
  custom_http: { supportedCardDelivery: ['legacy', 'shadow', 'callback', 'none'], ingress: 'private_workload', runtimeTransport: 'protocol_runtime', successfulTerminal: 'custom_sse_done_or_json_eof' },
  foundry: { supportedCardDelivery: ['none'], ingress: 'none', runtimeTransport: 'none', successfulTerminal: 'foundry_response' },
};
```

Any future `AgentPlatform` addition must fail compilation until it states supported modes. Registry validation rejects a selected mode unsupported by that platform/protocol. Agent readiness/publish checks block `callback` agents whose workload mapping, callback ingress, runtime transport, or protocol version is absent. Existing Custom HTTP v1 rows remain `legacy` unless deliberately migrated; this plan must not silently change them.

Dispatch ordering is fixed: route with discovery data; perform the one final uncached, fail-closed agent authorization read; derive the mode/workload/tool metadata only from that fresh row; open the delivery operation; reassert remote turn-lease ownership; then dispatch that same fresh row. If operation creation fails in `callback` mode, do not call the provider. A registry revision or lease change aborts without dispatch. Never open from a stale discovery row and never refresh the pinned mode mid-turn.

Use explicit configuration ownership:

- each agent registration stores `cardDeliveryMode`, `cardDeliveryProtocolVersion`, `cardDeliveryWorkloadId`, and the server-owned allowed terminal `actionTypes`;
- schema/readiness validation cross-checks those fields against `PLATFORM_CAPABILITIES`;
- `AskHR:CardDelivery:CallbacksEnabled` is the centrally refreshed emergency kill switch, default false until rollout;
- `AskHR:CardDelivery:SubmissionTokenWriteVersion` is `1 | 2`, default 1 until the reader-first migration gate passes;
- callback limits/timeouts are bounded App Configuration values with startup validation and safe defaults documented in the environment catalog.

The kill switch stops creation of new callback operations. It does not change the mode of an in-flight attempt or silently fall back after dispatch. Registrations must have an explicitly tested legacy path before an operator changes them from `callback` to `legacy`.

### 3.5 No transport fallback at steady state

There is one authoritative mode per provider attempt. Callback-authoritative turns never inspect streamed tool arguments for cards or action results. A missed callback means no card, accompanied by safe fallback text from the business tool; it does not silently fall back to stream interception. This prevents duplicates and makes reliability measurable.

A temporary `legacy | shadow | callback` WxO deployment mode is permitted only during migration. It is removed after the rollback window. Shadow callbacks must never make a card actionable or set transaction status.

## 4. Versioned callback contract

### 4.1 Request

Both HTTP adapters accept the same strict JSON schema. Example values are synthetic:

```json
{
  "version": 1,
  "deliveryOperationId": "11111111-1111-4111-8111-111111111111",
  "deliveryId": "22222222-2222-4222-8222-222222222222",
  "entry": {
    "type": "card",
    "card": {
      "kind": "choice",
      "cardId": "example-choice",
      "title": "Choose an option",
      "options": [{ "value": "example", "label": "Example" }]
    }
  }
}
```

An action report uses the same envelope:

```json
{
  "version": 1,
  "deliveryOperationId": "11111111-1111-4111-8111-111111111111",
  "deliveryId": "33333333-3333-4333-8333-333333333333",
  "entry": {
    "type": "action_report",
    "actionType": "example_write",
    "outcome": "completed"
  }
}
```

Rules:

- The schema is strict: unknown fields and unsupported versions fail `400`.
- `deliveryOperationId` and `deliveryId` are UUIDs. The tool creates one `deliveryId` per logical callback and reuses it on retries.
- `card` must pass the current `ChatBlockSchema`, including the opaque `cardId` rules. `card.cardId` is the only card ID; no duplicate envelope field exists.
- `actionType` uses a bounded machine-name schema and must match a canonical server-owned allowlist on the selected agent registration. Unknown values are rejected and never enter telemetry or state.
- `outcome` is exactly `completed | failed | unknown`.
- No `sessionId`, employee selector, registry owner, workload identity, language, submission policy, provider name, or callback URL is accepted from the body.
- Callback v1 permits at most one card per provider turn. Use a 64 KiB route body limit and reject a parsed/canonical card above 48 KiB. Wrap the existing `ChatBlockSchema` in a callback-specific bounded validator: reject unknown object fields; allow at most 50 form fields, facts, options, or columns; at most 100 table rows and 1,000 cells; at most 4,000 characters in narrative/value strings and 256 in labels/field keys/option values. Preserve the existing 100-character opaque `cardId` rule. Do **not** tighten the shared legacy/template schema in this delivery, because that would silently change Custom HTTP v1 and configured templates. A later global hardening requires its own inventory and compatibility rollout.

### 4.2 Response and retries

```json
{ "accepted": true, "result": "created" }
```

The only success results are `created` and `duplicate`. The response must not echo the card, operation ID, session, owner, workload, or employee context.

HTTP mapping:

| Condition | Status | Stable code |
| --- | ---: | --- |
| new valid entry | 201 | `DELIVERY_CREATED` |
| exact idempotent retry | 200 | `DELIVERY_DUPLICATE` |
| malformed/unsupported body | 400 | `DELIVERY_INVALID` |
| missing/invalid workload credential | 401 | `CALLER_UNAUTHENTICATED` |
| authenticated workload outside its scope | 403 | `CALLER_NOT_AUTHORIZED` |
| card entry: unknown/expired operation, non-`OPEN` state, or changed lease owner | 409 | `TURN_NOT_OPEN` |
| action report: unknown/expired operation | 409 | `TURN_NOT_OPEN` |
| action report: known unexpired workload-matched operation in `OPEN`, `FINALIZING`, or `SEALED`, including after lease rotation | 201/200 | `DELIVERY_CREATED` / `DELIVERY_DUPLICATE`; persist evidence only, never session state |
| same ID/card ID with different content | 409 | `DELIVERY_CONFLICT` |
| per-turn count/byte bound exceeded | 413 | `DELIVERY_LIMIT_EXCEEDED` |
| Redis unavailable/uncertain commit | 503 | `DELIVERY_UNAVAILABLE` |
| Mongo unavailable/uncertain action-outcome commit | 503 | `ACTION_OUTCOME_UNAVAILABLE` |
| authenticated workload rate limited | 429 | `CALLER_RATE_LIMITED` |

Business tools may retry only transport failures, `429`, and `503`, with the same `deliveryId`, a short bounded backoff, and a deadline inside their own provider tool deadline. They must not retry `400/401/403/409/413`. A timed-out request whose commit status is unknown is safe to retry because the ID is idempotent.

### 4.3 Deterministic action reporting

The business tool reports what it can prove:

- `completed`: the business system explicitly confirmed the intended write or generation.
- `failed`: the business system explicitly rejected or failed the operation and the tool knows the intended effect did not complete.
- `unknown`: timeout, connection loss after submission, ambiguous provider response, or a multi-step operation with mixed/uncertain effects.

Never infer `completed` because the model called a reporting tool, the provider returned `done`, or the employee-facing answer sounds successful. A callback failure does not retroactively change a proven business result; if retries cannot record it, analytics remains `unknown` and the employee still receives truthful business text.

Add `transactionStatus: completed | failed | unknown | null` to internal telemetry and analytics. Retain `transactionCompleted` temporarily as the derived compatibility field `transactionStatus === 'completed'` because metrics rollup and session finalization currently consume it. Sensitive-trace agents continue suppressing action type everywhere. Migrate consumers, then decide separately whether to remove the boolean.

Action evidence has a separate lifecycle and linearization point from card visibility. Card callbacks are accepted only while `OPEN`; action reports may be durably recorded in `OPEN`, `FINALIZING`, or `SEALED` until the operation's absolute expiry because a proven external write remains true after chat delivery ends. Add a focused `AgentOutcomeRecorder` backed by an awaited Mongo pipeline upsert into `agent_action_events`, not the existing fire-and-forget message analytics path. Its unique `_id` is a server-HMAC of provider/workload/operation ID, so all reports for one attempt meet one atomic reducer. When the operation opens, compute that digest with the current HMAC key and pin it in the Redis operation record; retries reuse the pinned digest, so an HMAC-key rotation cannot split one attempt across documents. The document contains only bounded provider, canonical agent key, effective outcome, at most **eight** HMACed delivery IDs for retry detection, `createdAt`, `updatedAt`, `expiresAt`, and conflict state—no raw IDs, employee/session data, text, arguments, or results. The Mongo pipeline must check an existing delivery ID before the cap: a known exact duplicate succeeds even when eight IDs are stored; a ninth new ID returns `413 DELIVERY_LIMIT_EXCEEDED` and leaves the outcome/document unchanged. `expiresAt` is fixed from first creation and no retry extends it. For a normal agent it may also store the server-allowlisted action type. For `sensitiveTrace`, persist `actionType: null`; if conflict reduction needs identity, persist only a keyed non-reportable action-type discriminator and never expose it to telemetry, logs, metrics, troubleshooting output, or APIs. Add a boot-gated unique `_id` index and an `expiresAt` TTL index with `expireAfterSeconds: 0`; set `expiresAt` to 180 days to align with current message/event telemetry retention. Key rotation accepts current+previous material only for the documented overlap; new operations always use the current key. The Mongo upsert is the action report's linearization point: an identical retry is unchanged, and any conflicting action type/outcome makes the effective result `unknown` without overwriting the first evidence. The callback returns success only after this durable upsert succeeds.

Crash/idempotency order is explicit: validate the unexpired operation and workload in Redis; read its pinned outcome document ID; then perform the awaited idempotent Mongo upsert. Mongo is the sole action-evidence store. The callback does **not** project the outcome into the Redis operation or mutate session/active-flow state, which avoids a cross-store ordering race. A crash before Mongo commit produces no acknowledgement and a retry repeats the same upsert. A crash after Mongo commit but before response returns is an exact duplicate and produces no second event. If Mongo is unavailable, return `503`; the business tool retries the report with the same IDs but never retries the business write. Exact duplicate callbacks receive success without another durable event.

A later provider error, truncation, client disconnect, or card discard must not erase or downgrade an acknowledged `completed` or `failed` business outcome. At provider terminal/finally, the owning chat turn may read the authoritative Mongo outcome by the pinned document ID and apply it to active-flow/session state only after reasserting its live lease. If the report arrives after terminal handling or ownership loss, it remains analytics/reconciliation evidence and must not mutate a successor turn. If every bounded callback attempt fails, AskHR cannot claim durable receipt; the business tool must still report the truthful external result in safe text and surface a stable reporting-failure code to operations.

Within one turn, an exact duplicate action report is idempotent. A different outcome for the same logical action/delivery is a conflict, not last-write-wins. The reducer is deterministic: one unique report yields that outcome; identical duplicates do not count again; any conflicting outcome or multiple different terminal action types yields `unknown`. A conflict never rewrites the first stored evidence. No absence, timeout, or provider failure triggers an automatic retry of the business write.

## 5. Redis model and state machine

### 5.1 Keys and supported Redis topology

Use one versioned Redis hash per provider attempt:

```text
cardturn:<deliveryOperationId>   server-owned envelope, optional single card, card idempotency digests, and pinned action-outcome document ID
cardturnsuccess:<deliveryOperationId>   minimal immutable success marker used only by v2 submission-token consume
```

The turn envelope contains only server-owned data: version, session ID, agent key/revision, provider/protocol, pinned delivery mode, workload ID, state, raw lease owner for Lua comparison (never exposed), optional card body, card delivery/digest fields, the pinned HMAC action-outcome document ID, counts, absolute expiry, timestamps, and a seal reason. It never stores the action outcome itself. Existing session and submission-token keying remains server-side.

The current AskHR lease key and this operation key do not share a Redis Cluster hash slot. Therefore v1 explicitly supports the current non-cluster Redis deployment only. Phase 0 must verify the production/DR Redis topology and fail the release if cluster mode is enabled or planned inside the rollout horizon. Cluster support requires a separate reviewed lease/key migration that puts every key read by one Lua script in the same slot; do not claim cluster compatibility or attempt cross-slot Lua.

Compute one absolute expiry when the operation opens: the minimum of the session expiry and provider timeout plus finalization headroom, capped at five minutes unless a measured provider timeout requires a reviewed lower/greater value. Appends use `PEXPIREAT` (or equivalent) with that stored deadline and never extend it. TTL is crash cleanup, not normal lifecycle control.

On successful card finalization, atomically create the minimal success marker with an expiry equal to the submission token's 30-minute expiry plus a small clock/processing margin. It contains no card or employee data and is never refreshed. Token consume must check it in the same Lua transaction before `GETDEL`/use. Failed or discarded turns never create it.

Do not put card bodies in logs, Mongo analytics, turn history, App Insights attributes, or queue names.

### 5.2 State machine

```text
          accepted callback(s)
OPEN  ----------------------------> OPEN
  |                                  |
  | explicit provider success        | provider error/timeout/abort/lease loss
  v                                  v
FINALIZING -----------------------> SEALED(failed/discarded)
  |
  | card validated + policy/token activated atomically
  v
SEALED(succeeded) ---> existing card SSE delivery
```

- Only `OPEN` accepts **card** callbacks. Action-report acceptance follows the independent unexpired-operation rule above.
- `beginFinalization` atomically checks the current turn lease and changes `OPEN -> FINALIZING`. The optional card snapshot after this transition is authoritative. Action evidence is read separately from Mongo by its pinned ID. No grace period and no polling exists.
- Any **card** callback racing after that transition gets `TURN_NOT_OPEN`, even if it started earlier. Action reports do not use this card-state transition; they follow the unexpired-operation plus Mongo reducer contract in section 4.3.
- Provider failure, invalid success terminal, timeout, client abort, card-resolution failure, submission-token failure, or lease loss seals/discards the card and emits no pending card. Accepted action evidence is retained under its bounded absolute expiry and recorded independently.
- Normal card success seals and activates the submission token before the card SSE write. It does not wait for a browser acknowledgement that SSE cannot provide.
- Repeated finalization/seal calls are idempotent for crash-safe `finally` cleanup; they never reopen a turn.

### 5.3 Atomic card append and indexing

For a **card entry only**, implement a Lua script (or an equivalently atomic Redis transaction proven by tests) that performs all of the following as one operation:

1. Load and validate the turn envelope/version.
2. Require `state = OPEN`.
3. Require the stored provider/workload/agent to match the authenticated principal; the operation ID supplies correlation only.
4. Compare the current `lock:chatturn:<sessionId>` owner with the owner recorded when the turn opened.
5. If `deliveryId` exists, return duplicate only when the canonical payload digest matches; otherwise return conflict.
6. For cards, if `cardId` exists, return duplicate only for identical canonical content; reject a different body. Never overwrite.
7. Enforce exactly one card per turn in v1 and the fixed request/card limits.
8. Write the entry and idempotency/card indexes into the same operation hash and preserve its absolute expiry.

This is the required **atomic stage + index** boundary. A card body without its delivery/card digest indexes, or an index without its body, must be impossible.

Action reports never enter this Lua append path and never require `state = OPEN` or the current lease owner. Their Redis work is a read-only validation of a known, unexpired, workload-bound operation plus retrieval of the pinned outcome document ID; their sole mutation/linearization point is the Mongo reducer in section 4.3.

Canonicalization must be one tested implementation. Prefer stable JSON serialization of the already Zod-parsed value followed by SHA-256. Digest comparison is for idempotency, not secrecy.

### 5.4 Finalization

`CardDeliveryService.beginFinalization(deliveryOperationId, leaseOwner)` changes state and returns the optional single card. It then:

1. revalidates each stored ChatBlock as untrusted persisted data;
2. applies the existing server-side profile prefill allowlist;
3. enforces the provider/card budget;
4. derives a card-submission policy;
5. prepares an inactive v2 one-time submission token;
6. in one final Lua transaction, require `FINALIZING`, compare the stored owner with the **current live** `lock:chatturn:<sessionId>` owner again, mark the source operation `SEALED(succeeded)`, create the immutable success marker, and activate that token;
7. remotely assert ownership once more immediately before the card SSE write, then expose the resolved card to `chatMessage.ts` for existing delivery.

If card validation, policy creation, final lease comparison, or atomic activation fails, leave the token inactive, seal/discard card state, and emit no card. Lease loss after activation but before SSE suppresses the card; the unexposed token remains harmless and expires. V1 allows one card, so the unchanged one-card SSE event cannot create a partial card batch. Once provider success is proven and the operation/token are atomically activated, a later socket disconnect does not invalidate the business result or token; the employee may already have received the card event. Action evidence remains independent as specified above and never turns a provider failure into a successful response.

## 6. Submission policy binding

The existing submission token already binds session/key path, `cardId`, random per-emission `submissionId`, owner agent, and language. Extend it to a versioned policy envelope created **server-side from the validated card**, for example:

```ts
interface CardSubmissionPolicyV2 {
  version: 2;
  sourceOperationId: string;
  agentKey: string;
  cardId: string;
  submissionId: string;
  language?: string;
  allowedActions: readonly ('submit' | 'cancel' | 'select')[];
  dataContract: NormalizedCardDataContract;
}
```

The policy is not accepted from provider or browser input. It records the actions and data keys/types the rendered kind permits:

- form: `submit` with declared field IDs/types and `cancel`;
- confirm: `submit` or `cancel`, empty data;
- choice: `select` with an allowed option value; preserve current documented cancel-keyword behavior if product keeps it;
- selectable table: the documented selected-row shape and bounds; read-only tables must not gain a write action merely from client data.

On the next `/api/chat`, atomically require the immutable source-operation success marker, consume the exact token, validate `cardData.action/data` against its policy, and only then dispatch directly to the bound owner. The success marker must remain at least as long as the 30-minute submission-token TTL. A failed, discarded, expired, or unknown source operation can never authorize submission even if cleanup failed.

Normalize submission data as untrusted structured model input before dispatch. Reject unknown keys, duplicate or invisible control characters, invalid Unicode, wrong types, strings beyond the policy/CardBlock limits, invalid ISO dates/ranges, select values outside the rendered allowlist, table rows not represented by the server-derived fingerprint, and values inconsistent with required/disabled/read-only fields. Serialize only the normalized policy-approved object into the provider message and label it untrusted employee input. Business tools must still revalidate authorization, invariants, and write preconditions; the card policy is not business authorization.

Preserve the current fresh authorization, status, geography/role, breaker, outage, and dispatchability checks. A client cannot select another owner, language, action, option value, field, or table row. Replays and malformed data fail before provider dispatch.

Submission-token migration is reader-first across two releases/flags:

1. Deploy code that reads v1 and v2 but continues writing v1. Verify every live and rollback image is a dual reader.
2. Enable v2 writes with a centrally refreshed flag only after no v1-only instance can receive traffic. Rollback images must remain dual readers.
3. After the last possible v1 token's 30-minute TTL plus safety margin, v1 reads may be removed in a separate deployment.

Test all old/new reader-writer routing combinations. Never let an old reader `GETDEL` a v2 token it cannot understand.

## 7. Provider integration details

### 7.1 WxO

Each card-producing business toolkit gets a tiny shared callback helper. The **same function** that builds/returns a `card` calls the helper before returning. Each write function similarly reports its deterministic action outcome. The helper:

- reads `delivery_operation_id` from `AgentRun.request_context`, never model arguments, and validates its UUID shape;
- reads its fixed callback destination and workload credential from its approved connection/runtime configuration;
- sends the strict envelope through Apigee;
- reuses a generated delivery ID for bounded retries;
- reuses one invocation-scoped async HTTP client for its business and callback calls when practical; do not add a module-global client unless the WxO worker lifecycle explicitly supports clean ownership;
- returns safe employee fallback text even when card delivery fails;
- never logs headers, operation IDs, card bodies, employee fields, URLs with credentials, or raw response bodies.

Remove `askhr_platform_tools:stage_card` and `askhr_platform_tools:report_action` from agent YAML, prompt instructions, tool allowlists, packaging, and tenant imports after callback mode is proven. Business tools must not return a card and rely on the model to forward it. They may return a small non-sensitive `cardDelivered` status so the prompt can choose fallback wording, but it is not delivery authority.

`wxoRunManager.ts` continues parsing WxO SSE for text, safe bounded tool-name telemetry, continuity, failures, resource limits, and explicit `done`. In callback mode it does not parse any tool arguments for cards/actions, does not call Redis card staging, and does not call `onCard`.

### 7.2 Custom HTTP

Introduce a new Custom HTTP protocol revision for callback-capable providers rather than silently changing strict v1. Its request has separate `message`, minimized `context`, `thread_id`, and protected `runtime` blocks. The runtime contains only the one-turn operation ID and an ingress identifier selected by server configuration; the provider must not copy it into model messages or conversation storage.

Card-producing business code uses the private workload callback ingress and managed workload identity. After migration, it does not emit `{type:"card",cardId}` and does not use the legacy `stage-card` action. Text SSE/JSON semantics and explicit success terminals remain unchanged. During transition, existing v1 providers retain their existing opaque-card signal contract; they are not silently treated as v2. A v1 provider may be text-only indefinitely, but a card-capable registration must migrate before legacy staging is deleted.

`customHttpRunManager.ts` in v2 handles only text/terminal/failure/continuity. Card delivery comes from the turn inbox. Connection tests receive no delivery runtime and fail if a callback occurs, preserving their side-effect-safe contract.

In callback mode, both WxO and Custom HTTP v2 run managers may return an explicit successful provider terminal even when they observed no text or legacy card signal. They must not decide final renderability. `chatMessage.ts` begins finalization and applies the combined route-level rule: success requires nonblank provider text **or** one successfully finalized callback card. If neither exists, return the existing no-renderable-output failure. Legacy-mode adapters retain their current renderability behavior unchanged. Add card-only tests for WxO, Custom HTTP v2 SSE, and Custom HTTP v2 JSON.

### 7.3 Foundry

Foundry remains text-only. Do not include a delivery runtime, accept a Foundry card callback, inspect Responses events for cards, or emulate cards with provider UI. Its capability registry entry is `none`, and publish/readiness tests enforce that invariant.

### 7.4 Future providers

A new adapter is incomplete until it defines:

- deterministic runtime/correlation transport;
- scoped workload authentication and ingress;
- success/failure terminal semantics;
- timeout and resource bounds;
- continuity commit point;
- callback ordering/finalization behavior;
- sanitized conformance and load evidence.

It then reuses `CardDeliveryService`; it does not add provider-specific Redis keys or a new widget card type.

## 8. Rate limiting and credential blockers

Two current conditions block enabling callback authority:

1. `/api/internal/*` is protected by one fleet-wide Redis-backed **120 requests/minute, IP-keyed** bucket. Apigee uses one/few egress IPs, so all callbacks share that allowance. Card callbacks would compete with token resolution and configuration calls and can create legitimate `429`s at modest concurrency.
2. `api-key-internal-agents` is a shared credential. It does not bind a callback to one provider workload or allowed agent.

Resolve both before the live canary. Mount the card gateway route so it does not inherit the shared 120/min limiter. Give it:

- a coarse pre-auth abuse ceiling appropriate for the Apigee egress topology;
- a Redis-backed post-auth workload bucket keyed by validated `workloadId`;
- a second per-turn bound enforced atomically in the inbox;
- optional per-provider operational quotas derived from measured peak, never client-selected identity.

Do not guess production limits. Capture forecast peak card-producing tool calls per second, burst factor, number of active agents, callback retries, and Apigee egress fan-in. Load at twice forecast peak and set the default/alert headroom from that evidence. A successful gate has zero legitimate `429`s, bounded Redis growth, and no starvation of `/api/internal/resolve-token`.

Credential prerequisites are external stop conditions, not code TODOs: Apigee product/policy, workload identities, backend verifier trust, Key Vault entries if selected, rotation ownership, non-production/prod separation, and revocation drill must all be documented and exercised.

WxO toolkit capacity is a separate scale gate. Inventory every agent -> business tool -> Python toolkit mapping, the target tenant entitlement/tier, expected peak concurrent calls, tool duration/timeout, and observed queue/call p95/p99. Current IBM documentation describes bounded toolkit deployments and Premium tenant limits; verify the current numbers with the tenant/IBM rather than assuming 100 independent toolkits are deployable. The callback runs inside the already-selected business tool, so it adds no second toolkit invocation and removes the extra model-selected platform-tool invocation. At twice forecast peak, every shared business toolkit—not only a platform helper—must remain within its measured queue SLO. Seek entitlement or deliberately regroup tools only after evidence; do not preemptively shard.

## 9. Failure behavior

| Failure | Required behavior |
| --- | --- |
| callback before provider completion fails validation/auth | reject; tool uses safe text; provider may continue; no card |
| callback times out after an uncertain commit | retry same delivery ID; duplicate is success |
| duplicate same ID and same canonical body | acknowledge duplicate; do not append twice |
| card duplicate ID/card ID with different body | `409` conflict; preserve first card |
| action duplicate ID with different action/outcome | record conflict through the Mongo reducer; effective outcome is `unknown` |
| card with stale/unknown operation, wrong workload, changed lease owner, or non-`OPEN` state | reject; no card or session mutation |
| action with unknown/expired operation or wrong workload | reject; no durable or session mutation |
| action for a known unexpired workload-matched operation after `FINALIZING`, `SEALED`, or lease rotation | persist/reduce in Mongo; acknowledge only after commit; never attach a card or mutate session/active-flow state |
| provider error/timeout/truncation/invalid terminal | seal/discard card state; emit no card; retain and record already accepted action evidence |
| provider success with no text and no finalized callback card | route-level existing no-renderable-output failure |
| invalid persisted card during finalization | fail the turn's single card; no card output |
| Redis unavailable during callback | `503`; bounded idempotent retry; safe fallback text |
| Redis unavailable during finalization/token commit | fail closed; clean up best effort; no actionable card |
| client disconnect before provider success/finalization | abort upstream; discard card state; retain accepted action evidence; breaker-neutral as today |
| disconnect after successful atomic activation | do not revoke the successful source marker/token; the employee may already have received the single card event |
| application crash | TTL cleanup; no polling/replay worker; next request does not resume the old turn |

Configured Mongo card templates should not silently rescue a missing or invalid callback in the new mode. That fallback hides producer defects and makes reliability unmeasurable. If templates remain for explicitly configured, non-dynamic uses, give them a distinct server-selected path; never resolve an arbitrary unstaged callback `cardId` to a template.

## 10. Observability and privacy

Use the existing operation/agent lifecycle telemetry, with bounded dimensions only. Add counters and histograms for:

- callback attempts by provider, ingress, result code, and environment;
- auth rejection, scope rejection, rate limit, invalid, duplicate, conflict, late, and limit-exceeded counts;
- callback request -> Redis acknowledgement latency;
- first accepted callback -> provider success latency;
- provider success -> finalization complete and first card SSE enqueue latency;
- inbox entries/bytes per turn as numeric distributions;
- turn seals by success/failure reason;
- card submission policy accept/reject/replay;
- action outcome `completed | failed | unknown | none`;
- shadow agreement: callback present/legacy present, count/order/digest agreement, without payloads.

Never log or persist card bodies, delivery operation IDs, auth headers, employee/session IDs, form values, tool arguments/results, raw provider events, Workday data, email addresses, or unbounded external error strings. Do not use raw `deliveryOperationId`, `deliveryId`, `cardId`, `actionType`, workload claim, or agent-produced strings as metric labels. Use the existing internal operation ID for trace correlation; if cross-system correlation needs a turn reference, use a backend HMAC truncated for logs and not accepted as authority.

Provider capture used as a test fixture must be manually reduced to the smallest structural event, synthetic, and reviewed for PII/secrets. Do not commit either supplied raw log artifact.

Alerts:

- valid callback acceptance below 99.9% over a meaningful minimum volume;
- late/conflict/auth/rate-limit spikes;
- callback-to-Redis or terminal-to-SSE p95 breach;
- any callback on a `none` provider;
- action `unknown` above the agreed business baseline;
- shadow disagreement above zero after known migration exclusions.

## 11. Performance, load, and SLO gates

These are release targets, not current claims. Before a run, publish an evidence manifest containing forecast requests/second and concurrent turns, provider/protocol mix, card/action mix, warm-up, repetitions, duration, App Service instance count, Apigee egress topology, percentile method, and exact allowed exclusions. Use nearest-rank percentiles. Reliability evidence requires at least 3,000 independent valid card journeys after warm-up with zero platform-attributable delivery failures; that supports a one-sided approximately 95% upper failure bound near 0.1%. A smaller canary is directional evidence and cannot claim 99.9% reliability. Run three repetitions and report each plus pooled results.

- At least **99.9%** valid, authenticated callback cards reach a valid card SSE emission after a successful provider turn under the minimum-volume rule above. The denominator and provider/business outage exclusions are declared before the run by the service owner; platform callback/gateway/Redis failures are not excluded. Stale, cross-workload, cross-turn, malformed, and replay attempts are rejected 100% in the finite test corpus. Browser rendering is a separate deterministic correctness and p95 gate; do not claim 99.9% end-to-end widget rendering from the smaller browser sample.
- Backend callback processing (request admitted through Redis acknowledgement) is p95 <= 150 ms and p99 <= 300 ms at twice forecast peak. Measure Apigee end-to-end separately; target p95 <= 500 ms unless the platform owner approves a measured alternative.
- Provider-success terminal to first card SSE enqueue is p95 <= 100 ms and p99 <= 250 ms for a one-card turn at twice forecast peak.
- The callback adds no model round trip. Compare card-ready -> provider completion against the current extra-control-tool baseline and record the improvement/regression.
- Zero legitimate callback `429`s, zero cross-turn deliveries, zero partial actionable batches, and zero post-lease-loss cards in the twice-peak run.
- Soak infrastructure errors below 0.1%, Redis memory/key count returns with TTL/lifecycle expectations, no event-loop or connection-pool saturation, and no callback herd during retries.

The load profile must include WxO/Apigee callback and existing Custom HTTP v1 traffic in forecast proportions; add Custom HTTP v2 only when that protocol is actually scheduled for migration. Include 0/1-card, exact-limit, duplicate, conflict, late, callback-timeout retry, provider-failure, slow-client, lease-loss, and action-report turns; cold/warm Redis connections; multiple App Service instances; and the actual shared Apigee egress topology. Report completed turns and delivered cards, not only request rate.

Create two named executors:

1. `packages/backend/src/scripts/loadTestCardDelivery.ts`, exposed as `loadtest:card-delivery` from the backend package. `CARD_DELIVERY_LOAD_MODE=in-process` uses deterministic service/store/auth adapters for race/fault correctness. `CARD_DELIVERY_LOAD_MODE=qa` drives signed synthetic sessions through the real QA proxy, multi-instance App Service, QA Redis/Mongo, and either the real QA Apigee callback product or direct private ingress in declared proportions. A dedicated side-effect-free provider/tool stub creates a fixed synthetic card and explicit success/failure terminals; fixed server-owned scenario IDs inject duplicate, timeout, late, lease-loss, and disconnect cases. Redis/Mongo/network fault injection exists only in this in-process mode. Deployed QA uses actual dependency behavior and only separately approved infrastructure failure drills; never add request-controlled failure hooks to a deployed build. Require `CARD_DELIVERY_LOAD_TEST_TARGET=qa`, matching `APP_ENV=qa`, and `CARD_DELIVERY_LOAD_TEST_ACKNOWLEDGE=shared-qa-no-business-writes` for deployed mode.
2. `scripts/qa/measureCardDelivery.mjs`, exposed from the root package as `qa:measure-card-delivery`, uses Playwright against the deployed QA widget/proxy and the same harmless canary agent. It records callback acknowledgement, provider terminal, card SSE receipt, ChatBlock render, and enabled-control state with `performance.now()`, never card text/values. Pin `@playwright/test` exactly in the root `devDependencies`, commit the lockfile, and install the matching Chromium with `npx playwright install chromium` after `npm ci` in the QA executor. Require `ASKHR_BROWSER_TARGET=qa`, matching `APP_ENV=qa`, `ASKHR_BROWSER_BASE_URL`, `ASKHR_BROWSER_AUTH_STATE`, and `ASKHR_BROWSER_ACKNOWLEDGE=synthetic-qa-only`. The auth-state file must come from the approved short-lived QA login bootstrap, live in a permission-restricted temporary path, and be deleted in `finally`; if that bootstrap does not exist, this gate is `BLOCKED`, not bypassed. Require at least 300 successful journeys for browser p95. The 3,000-journey backend/integration gate proves callback-to-card-SSE reliability; the browser run proves rendering correctness and p95 only.

Both executors require a caller-selected temporary output path and write the same versioned evidence envelope: mode, run ID hash, app instance count, seed, topology, scenario, warm-up, repetitions, admitted/completed/provider-success/card-SSE/render counts, failure codes, nearest-rank percentiles, cleanup result, and app/config versions. They include no payloads or identifiers, tag all synthetic state with a random run ID, clean it in `finally`, verify cleanup, and exit nonzero on test or cleanup failure. Credentials come from the approved secret environment, never CLI arguments or the report.

Example commands:

```text
CARD_DELIVERY_LOAD_MODE=in-process \
CARD_DELIVERY_LOAD_TEST_ACKNOWLEDGE=synthetic-only \
npm run loadtest:card-delivery -w packages/backend -- --output <temporary-summary-path>

CARD_DELIVERY_LOAD_MODE=qa \
CARD_DELIVERY_LOAD_TEST_TARGET=qa \
APP_ENV=qa \
CARD_DELIVERY_LOAD_TEST_ACKNOWLEDGE=shared-qa-no-business-writes \
npm run loadtest:card-delivery -w packages/backend -- --output <temporary-summary-path>

ASKHR_BROWSER_TARGET=qa \
APP_ENV=qa \
ASKHR_BROWSER_BASE_URL='<approved QA URL>' \
ASKHR_BROWSER_AUTH_STATE='<permission-restricted temporary auth-state file>' \
ASKHR_BROWSER_ACKNOWLEDGE=synthetic-qa-only \
npm run qa:measure-card-delivery -- --output <temporary-summary-path>
```

## 12. Implementation phases

### Phase 0 - evidence and external prerequisites (stop gate)

1. On a new isolated `codex/*` worktree from freshly resolved `origin/main`, record base commit and clean state.
2. Confirm the current work-PC source/tests/handbook still match the evidence listed here.
3. Classify as S3 and obtain named implementation, security/privacy, correctness, and QA owners.
4. Prove `delivery_operation_id` reaches the WxO business tool through `AgentRun.request_context` and is not accepted as a model argument; its visibility to the agent is not an authorization assumption.
5. Approve scoped workload auth for Apigee and Custom HTTP, provision non-production identities, and document rotation/revocation.
6. Measure forecast callback volume and separate the route from the shared internal 120/min IP bucket.
7. Inventory Custom HTTP v1 registrations. Define a v2 protocol identifier only for registrations intentionally scheduled to migrate; v1 remains supported.
8. Verify production and DR Redis are non-clustered. If either is clustered or scheduled to become clustered during rollout, stop and design the lease/key migration before implementation.
9. Inventory current WxO card producers and production traces for multi-card turns. Callback v1 intentionally supports one card because the existing widget receives one card event at a time and cannot make a multi-card batch atomic. If any supported WxO journey requires more than one card per turn, stop: either preserve that agent on `legacy` or approve a versioned card-batch SSE/widget contract before migrating it. Do not silently truncate or reject an existing journey.

Do not proceed to a provider-enabled phase if items 4-6 or 8-9 are unresolved. Unit code may be built dark, but no live callback-mode agent gets a delivery operation ID.

### Phase 1 - service and Redis state, dark

1. Add strict callback and stored-envelope types.
2. Implement open, atomic append/index, begin-finalization, seal, and discard operations in `CardDeliveryService`/Redis.
3. Add dual v1/v2 submission-token readers while continuing to write v1. Add the source-success marker and inactive-token/atomic-activation primitives dark. Do not enable v2 writes in this phase.
4. Replace the boolean capability map with the exhaustive registry.
5. Add privacy-safe telemetry.
6. Keep all providers in effective legacy/none mode; run unit/race/load tests against synthetic data.

Gate: all atomicity, idempotency, lease race, state transition, cleanup, and policy tests green; S3 correctness and security reviews have no unresolved high-severity finding.

### Phase 2 - authenticated ingress and provider runtime

1. Add gateway and private middleware/routes delegating to the same service.
2. Add dedicated post-auth workload rate limiting and route-specific body limit.
3. Create operation records only for attempts pinned to `shadow` or `callback`; send no delivery runtime to `legacy`/`none` attempts.
4. Implement WxO `AgentRun` correlation and, when scheduled, Custom HTTP v2 `runtime`; Foundry gets neither.
5. Update readiness/publish/connection tests to fail closed on missing mapping or callback attempt during a probe.

Gate: negative auth/scope/replay/load tests, credential rotation/revocation drill, and network reachability tests pass in dev/QA.

### Phase 3 - producer migration and shadow comparison

1. Update one harmless WxO business read tool to callback directly when it creates a card, while temporarily keeping the marker tool for comparison.
2. In `shadow`, accept/store callback entries in an isolated shadow namespace/digest summary but keep legacy interception authoritative. Shadow state can never create a submit token or transaction status.
3. Compare presence, count, and canonical digest across enough card turns; inspect late/conflict/latency and Apigee capacity.
4. Run regression coverage against Custom HTTP v1. Migrate one Custom HTTP registration to v2/private callback only if it is independently approved; this is not required for the first WxO release.

Gate: every card callback entry is structurally valid, idempotent, correctly scoped, and matched when a legacy card signal is actually present; absence of legacy `run.step.delta` data is recorded rather than counted as callback disagreement. Action reports are evaluated separately against their Mongo reducer contract. The statistically defined callback reliability/load gate, latency SLOs, and safe fallback text must pass. Any duplicate, wrong-owner, conflicting-body, or unexplained callback defect stops promotion.

### Phase 4 - callback authority and live canary

1. Switch the canary agent/provider attempt to `callback`; stream interception remains observation-only for the short rollback window.
2. After every live and rollback image is proven to read v2, enable v2 token writes for the canary through the centrally refreshed flag. Run the live Apigee/WxO canary in the approved environment: a harmless business read produces a synthetic/non-sensitive ChatBlock, its callback is acknowledged, WxO explicitly completes, AskHR atomically seals success/activates the token, emits the card, and a real widget renders it. Capture timestamps for tool card-ready, callback acknowledged, provider terminal, activation, backend SSE enqueue, and browser render.
3. Run negative canaries for duplicate retry, stale operation, wrong workload, late callback, provider failure after callback, and client disconnect. Do not perform a destructive business write for the card canary.
4. Repeat for Custom HTTP v2 direct ingress only when a v2 registration is in the approved migration cohort; otherwise rerun full v1 regressions.
5. Expand one agent/environment at a time only after its preceding cohort is healthy.

Rollback immediately on missing/duplicate/wrong-owner cards, unexplained `unknown` actions, auth/scope failures, legitimate rate limits, or SLO breach.

### Phase 5 - remove legacy transport

After the agreed stable observation window:

1. Remove `stage_card` and `report_action` control tools from WxO YAML/toolkits, prompts, agent tool declarations, and deployment instructions.
2. Delete WxO stream argument interception, direct `stageCard` calls, and stream-owned transaction completion.
3. Keep Custom HTTP v1 card signaling/staging while any registration uses it. Its eventual removal is a separate inventory-backed cleanup after every affected registration is deliberately migrated; retain v1 text parsing if still needed.
4. Remove no-op `report-action` and any WxO-only legacy staging surface that has no remaining caller. Keep `/api/internal/config` `stage-card` unchanged while any Custom HTTP v1 registration uses it; keep `get-config` and `/api/internal/resolve-token` intact.
5. Remove temporary shadow/legacy mode and shadow Redis keys/metrics.
6. Update all current-state docs and `BUILD-STATUS.md` with fresh evidence.

Gate: repository search finds no active **WxO** stream-card interception or model-called control tool. Every remaining legacy card signal/config action has an inventoried Custom HTTP v1 owner and regression coverage; none is orphaned.

## 13. Exact expected file impact

Reconcile names with current work-PC source before editing; this list is intentionally explicit so scope growth is visible.

### New AskHR files

| File | Responsibility |
| --- | --- |
| `packages/backend/src/types/cardDelivery.ts` | strict v1 callback, stored entry, typed result/error, principal, and state contracts |
| `packages/backend/src/services/agents/cardDeliveryService.ts` | only open/accept/finalize/seal/discard service and submission-policy derivation |
| `packages/backend/src/services/agents/cardDeliveryService.test.ts` | state, order, validation, idempotency, conflict, bounds, failure cleanup |
| `packages/backend/src/services/agents/agentOutcomeRecorder.ts` and `.test.ts` | awaited idempotent action-evidence upsert and conflict reduction |
| `packages/backend/src/middleware/cardDeliveryAuth.ts` | normalize approved Apigee/direct credentials to scoped principal |
| `packages/backend/src/middleware/cardDeliveryAuth.test.ts` | issuer/audience/scope/rotation/replay/negative auth tests |
| `packages/backend/src/routes/cardDelivery.ts` | gateway/private thin ingress adapters and HTTP mapping |
| `packages/backend/src/routes/cardDelivery.test.ts` | strict schema, body limit, auth order, rate limit, response contract |
| `packages/backend/src/scripts/loadTestCardDelivery.ts` | deterministic in-process and deployed-QA callback/load evidence executor |
| `scripts/qa/measureCardDelivery.mjs` | real-proxy browser render timing/evidence executor |

Repository ownership is not optional: `packages/backend/src/utils/redis.ts` constructs every new Redis key and owns every TTL and atomic Redis primitive. `CardDeliveryService` may orchestrate those focused helpers, but it must not construct keys or issue inline Redis commands. Do not create a parallel Redis store abstraction or a general queue framework.

### Modified AskHR runtime and tests

| File(s) | Change |
| --- | --- |
| `packages/backend/src/app.ts` | mount the dedicated callback ingress/limits in the correct order, outside the shared internal bucket |
| `packages/backend/src/middleware/rateLimit.ts` and `.test.ts` | authenticated workload key support without trusting an unverified body/header |
| `packages/backend/src/utils/redis.ts` and `.test.ts` | sole owner of turn-inbox/replay keys, TTLs, Lua primitives, and submission token v2 |
| `packages/backend/src/routes/chatMessage.ts` and `.test.ts` | create/open delivery turn after fresh authorization, pass runtime, finalize after provider success, single-card SSE emission, cleanup, policy consume/validate |
| `packages/backend/src/services/agents/agentDispatcher.ts` and `.test.ts` | carry separate `deliveryRuntime`; never merge it into employee/model context |
| `packages/backend/src/services/agents/agentContext.ts` and `.test.ts` | assert delivery fields are not selectable/profile/model context |
| `packages/backend/src/services/agents/platformCapabilities.ts` and `.test.ts` | exhaustive provider capability/mode registry |
| `packages/backend/src/services/agents/wxoRunManager.ts` and `.test.ts` | pass/correlate runtime operation; allow callback-mode card-only success; ultimately remove card/action tool-argument interception |
| `packages/backend/src/services/agents/customHttpContract.ts`, `customHttpRunManager.ts`, and tests | v2 runtime separation/callback mode; migrate/remove v1 card signal for card-capable agents |
| `packages/backend/src/services/agents/foundryRunManager.ts` and `.test.ts` | prove no delivery runtime/card callback path |
| `packages/backend/src/services/agents/agentRegistry.ts`, `agentDispatchability.ts`, `agentReadiness.ts`, `agentConnectionTest.ts` and tests | protocol/workload readiness and connection-test side-effect guards |
| `packages/backend/src/services/config/featureFlags.ts` and tests | centrally refreshed callback kill switch and token-write version with strict enum validation |
| `packages/backend/src/routes/config.ts` and `.test.ts` | delegate legacy staging during migration; remove `stage-card` and no-op `report-action` at cleanup |
| `packages/backend/src/types/agentTelemetry.ts` | add tri-state action outcome |
| root `package.json`, `package-lock.json`, and `packages/backend/package.json` | expose guarded load/browser commands and pin the browser-test dependency |
| `packages/backend/src/utils/mongoClient.ts` and tests | boot-gated `agent_action_events` unique and 180-day TTL indexes |
| `packages/backend/src/services/ops/analyticsLogger.ts`, `sessionTroubleshooter.ts`, `jobs/metricsRollup.ts`, `jobs/sessionFinalizer.ts` and tests | store/report tri-state safely; derive legacy completed boolean during migration |
| `packages/backend/src/types/sse-events.ts` and `packages/widget/src/types/sse-events.ts` | no shape change expected; regression assertion only |
| `packages/widget/src/components/chat-window.test.ts`, `message-bubble.test.ts`, `chat-block.test.ts` | prove the existing single-card SSE renders and policy-valid submissions round-trip; production component change is not expected |

### Configuration, docs, and operations

| File(s) | Change |
| --- | --- |
| `packages/backend/.env.example` | exhaustive non-secret callback TTL/size/count/timeout and dedicated rate-limit catalog; never actual capacity credentials |
| `KEY-VAULT-VALUES.md` | approved secret naming/ownership only if per-workload keys are selected; no values |
| `config/agents/work_offsite.json`, `config/agents/evl_agent.json`, and other card-capable agent registrations | callback workload/protocol readiness metadata if registry-owned; remove control tool names |
| `config/platform-overrides/{dev,qa,uat,prod}.json` | only reviewed environment enablement metadata; do not copy credentials |
| `docs/handbook/engineering/04-security-and-auth.md` | non-authoritative correlation handle and scoped workload trust boundary |
| `docs/handbook/engineering/03-data-stores.md` | `agent_action_events` bounded schema, indexes, 180-day retention, and ownership |
| `docs/handbook/engineering/07-routing-and-agents.md` | capability modes and callback/finalization sequence |
| `docs/handbook/engineering/10-api-reference.md` | two ingress contracts, auth, limits, errors; retain documented Custom HTTP v1 config actions until their last consumer migrates |
| `docs/handbook/engineering/13-environment-variables.md` | ownership/lifecycle/safety for new variables |
| `docs/handbook/engineering/08-feature-flags-and-config.md` | exact App Configuration keys, defaults, refresh, emergency-off behavior, and token-version rollout |
| `docs/handbook/integrations/01-agent-builder-guide.md`, `02-wxo-agents.md`, `03-foundry-and-custom-http.md`, `05-cards-reference.md` | same-business-tool callback contract, Custom HTTP v2, Foundry text-only, submission lifecycle |
| `docs/runbooks/deployment.md` and `docs/handbook/operations/05-agent-promotion.md` | identity provisioning, shadow/canary, rollout, rollback, revocation, evidence |
| `postman/` collection/environment artifacts | add both new callback ingress contracts with synthetic placeholders and no credentials |
| optional `docs/architecture/07-Adaptive-Cards-Signals.md`, `agent-card-contract.md`, `custom-http-provider-contract.md` when present | supersede current interception/signal rationale; retain clearly labelled history only |
| `BUILD-STATUS.md` | top checkpoint with fresh commands, reviewer evidence, canary/SLO results, and external gaps |

### External WxO/Custom HTTP artifacts (separate owner/repository)

- `Tools/askhr_platform_tools/` is retired after rollback window.
- Every card-producing function in `Tools/work_offsite_toolkit/tools.py` and `Tools/evl_tools/tools.py` calls the shared callback helper before returning its card; write functions report deterministic outcomes.
- `Agents/work_offsite_agent.yaml` and `Agents/evl_agent.yaml` remove model instructions and declarations for `stage_card`/`report_action`, declare `delivery_operation_id` in `context_variables` only if required for `AgentRun` access, never interpolate or describe its value in instructions, and preserve safe fallback text requirements.
- Each business toolkit connection gains only the callback base URL and its own scoped workload credential/identity material. Do not add the old generic `config_url` merely to stage cards, reuse the EVL token-resolver key, or create a connection for the retired no-op platform toolkit.
- Toolkit tests cover callback success, retry/duplicate, hard reject, timeout fallback, no sensitive logging, and outcome mapping.
- Deployment README/import commands and live evaluation cases are updated.

Do not copy the supplied external repository or raw log files into AskHR.

## 14. Test and verification matrix

### Unit/contract

- strict version/body/card/action schemas, exact limits, unknown fields;
- operation correlation and principal scope match/mismatch;
- assertion replay claim is atomic, duplicate `jti` is rejected, expiry is set to assertion expiry plus bounded clock skew, and Redis failure fails closed;
- card entries: `OPEN` plus current lease accepts; `FINALIZING`, `SEALED`, expired, or lease-rotated rejects;
- action reports: known unexpired workload-matched operations accept in `OPEN`, `FINALIZING`, and `SEALED`, including after lease rotation; expired/unknown/wrong-workload rejects and no callback mutates session state;
- card lease rotates before, during, and after append;
- atomic body/index under injected Redis failure;
- same delivery retry, conflicting delivery, same card ID same/different content;
- concurrent duplicate/conflicting callbacks for the single v1 card slot;
- count/byte cap exact-limit success and plus-one failure;
- provider failure/timeout/abort/disconnect discards card state but retains accepted canonical action evidence;
- successful provider terminal freezes before drain; late race loses;
- card-only success for WxO and Custom HTTP v2 SSE/JSON, plus empty-without-card failure;
- submission policy per card kind; unknown/extra keys, invisible controls, Unicode, length, wrong type/date/option/row, replay, and source-success-marker failure;
- reader-first v1/v2 rollout and all mixed reader/writer/rollback-image combinations;
- action completed/failed/unknown mapping, duplicate/conflict reduction, provider failure/disconnect after accepted completion, and no successor-turn mutation;
- action retry index: eight unique IDs accepted, ninth new ID rejected without mutation, known duplicate at the cap succeeds, and first-created `expiresAt` never moves;
- action-recorder crash before/after awaited Mongo upsert and lost HTTP response; exact retry creates one durable event;
- action report racing provider terminal/finalization, arriving just after terminal, and arriving after lease loss; Mongo remains authoritative and no callback mutates successor session state;
- action report bypasses the card Redis append/`OPEN` restriction while still rejecting unknown, expired, or wrong-workload operations;
- submission after the short callback-operation key expires but before the success-marker/token TTL expires;
- lease rotation after `beginFinalization` but before activation, and after activation but before SSE; neither emits a stale card;
- Foundry and connection probes never receive a delivery runtime;
- sensitive fields absent from logs/errors/metrics snapshots; a `sensitiveTrace` action persists no raw action type and exposes no action-type discriminator.

The soak also asserts replay-cache keys expire and steady-state replay-key cardinality remains bounded by the accepted assertion rate multiplied by the two-minute acceptance window plus clock skew. Any non-expiring replay key is a release blocker.

### Integration

- callback -> Redis -> WxO `done` -> atomic success/token activation -> card SSE -> widget render;
- callback -> Redis -> provider failure -> no card/token;
- Custom HTTP v2 SSE and JSON complete through the same inbox;
- multiple App Service instances accept callback on one instance and finalize on another;
- Apigee and direct identities cannot cross agent/provider scope;
- card submission owner/language/auth stay bound across rolling deployment;
- a failed/crashed/disconnected/lease-lost source operation cannot consume a token even when cleanup is unavailable; a successfully sealed source remains consumable for the token TTL;
- shared internal 120/min traffic cannot throttle the isolated callback lane;
- Mongo template collision cannot rescue or replace a callback card;
- no raw card/tool/runtime data in telemetry.

### Commands and evidence

During implementation use narrow Vitest files/typecheck/build per phase. Before claiming repository completion run fresh:

```text
npm run verify
```

Because agent behavior/routing packages and live provider flows change, start with:

```text
npm run eval:quality -- --help
```

Then run the approved explicitly named `dev` or `qa` target when its dependencies are available. Run the external Python/toolkit suite, sanitized callback conformance suite, twice-peak load/soak, live Apigee/WxO card canary, Custom HTTP canary, credential rotation/revocation drill, and synthetic-browser render timing. `OBSERVED` and `BLOCKED` evidence are not approvals. Update `BUILD-STATUS.md` only with fresh results.

S3 completion requires fresh independent correctness, security/privacy, and QA evidence against the final diff and verification output.

## 15. Rollout and rollback

Rollout order is dev dark -> dev shadow -> QA shadow/load -> QA callback canary -> approved live Apigee/WxO canary -> one low-risk agent -> remaining card agents one at a time. Do not enable production by copying a lower-environment credential or broad allowlist.

Rollback boundaries:

- Before callback authority: turn off shadow collection; no user behavior changes.
- During the short dual-artifact window: set the affected provider back to `legacy` and redeploy the last known-good agent package if marker tools were removed from that revision.
- After legacy deletion: roll back the AskHR application and matching agent/tool package as one tested release unit. Do not turn old interception on against new packages that no longer emit control calls.
- Never roll back Redis by deleting broad keys. Versioned readers and TTLs handle in-flight state; new callback issuance stops, open turns finish/fail under their original mode, and sealed keys expire.

Every rollback test must show no duplicate card, no cross-version actionable token, no lost proven business result, and safe fallback text.

## 16. Done criteria

This work is complete only when all of the following are true:

- a business tool, not the model, deterministically sends every card/action callback;
- WxO/Custom HTTP runtime transport is proven; the operation ID is never accepted from the browser or as a model-selected argument, and workload authentication—not possession of the ID—is authority;
- workload auth is cryptographically validated and scoped to provider/agent; the shared internal-agent key is not callback authority;
- the callback lane is isolated from the shared 120/min IP bucket and passes twice-peak load/soak;
- `CardDeliveryService` is the only delivery-state owner and both ingress paths use it;
- Redis OPEN -> FINALIZING/SEALED transitions, single-card slot, atomic body/index, idempotency, conflicts, late rejection, absolute TTL, and cleanup pass race tests;
- cards emit only after explicit provider success and atomic source/token activation, and render through the unchanged typed SSE/widget contract;
- submission policies are one-time, owner/language/action/data bound and replay-safe;
- action analytics distinguishes `completed`, `failed`, and `unknown` from business evidence;
- WxO/Custom HTTP canaries meet reliability and latency SLOs, including negative cases;
- Foundry remains provably text-only and future platforms fail closed until declared;
- WxO legacy stream interception and model-called control tools are removed after the rollback window; Custom HTTP v1 card signaling/config actions remain until their last registered consumer is deliberately migrated;
- source, tests, handbook, runbooks, environment catalog, agent packages, current `BUILD-STATUS.md`, and any applicable architecture records agree;
- `npm run verify`, approved dev/QA quality evaluation, external toolkit tests, S3 reviews, and operational evidence are fresh and green, with every unavailable external gate reported as `BLOCKED` rather than waived.

## 17. Non-goals

- Changing ChatBlock kinds or adopting Adaptive Cards/provider-native widgets.
- Changing the browser `/api/chat` SSE event shape or redesigning the widget.
- Polling Redis/provider state from the widget or backend.
- Adding Service Bus, Kafka, durable workflows, a background worker, or a workflow used only for card transport.
- Letting providers, callbacks, or clients access Redis directly.
- Moving OBO, Workday tokens, secrets, or identity selection out of the backend.
- Retrying transactional provider requests or replaying a failed provider turn.
- Making Foundry card-capable without a separate reviewed provider contract.
- Reworking routing, knowledge retrieval, active-flow semantics, or unrelated agent latency.
- Treating raw provider logs as fixtures or observability storage.

## 18. Evidence basis and open approvals

This plan was grounded in current AskHR source/tests and the current handbook/architecture pages for routing, providers, auth, API, cards, and streaming; the backend/widget scoped instructions; and the supplied read-only WxO agent/tool packages and structural logs. The artifacts were used only to establish event/tool topology and the presence of sensitive trace material. No secret, token, employee identifier, card content from a real employee, or raw trace was copied here.

The supplied `wxo_logs.json`, `orig_logs.json`, and `digital twins runner.py` contain or can expose credentials, employee/persona data, request context, and raw provider traces. Keep them untracked and out of prompts/fixtures. Rotate or revoke every credential present in them through its owning system, and remove the local files only through the approved evidence-retention process.

The following are deliberately approvals, not assumptions:

1. Live proof that `delivery_operation_id` reaches the WxO tool's `AgentRun.request_context` exactly as sent; no secrecy claim is required.
2. Which cryptographic Apigee-to-AskHR workload identity mechanism security approves.
3. The Custom HTTP v2 protocol identifier and provider migration inventory.
4. Forecast peak callback volume and final dedicated rate limits.
5. The stable observation/rollback window and production canary authorization.

Until those are resolved, the design can be implemented and tested dark, but callback authority cannot be declared production-ready.

## 19. Official external references

Use current versions of these pages during implementation; IBM behavior is version-sensitive:

- [IBM: Authoring Python-based tools](https://developer.watson-orchestrate.ibm.com/tools/create_tool) — `AgentRun.request_context`, async I/O, and Python tool contracts.
- [IBM: Native-agent context variables](https://developer.watson-orchestrate.ibm.com/agents/build_agent) and [web-chat context variables](https://developer.watson-orchestrate.ibm.com/webchat/context_variables) — context is agent-accessible; it is not a secret/tool-only channel.
- [IBM: Runs streaming API](https://developer.watson-orchestrate.ibm.com/apis/orchestrate-agent/chat-with-orchestrate-assistant-as-stream) and [run-event API](https://developer.watson-orchestrate.ibm.com/apis/orchestrate-agent/get-orchestrate-assistant-run-events) — top-level event contract; nested `data` is not a dependable card transport schema.
- [IBM: Python toolkits](https://developer.watson-orchestrate.ibm.com/tools/toolkits/python_toolkits) and [toolkit naming](https://developer.watson-orchestrate.ibm.com/tools/toolkits/overview) — deployment, concurrency, reentrancy, and qualified tool names.
- [IBM: Agent performance guide](https://developer.watson-orchestrate.ibm.com/tutorials/performance/performance-guide-v2-agent) and [tool performance guide](https://developer.watson-orchestrate.ibm.com/tutorials/performance/performance-guide-v2-tools) — each reasoning/tool iteration adds latency; external API work must be measured in the target environment.
- [Microsoft: Access-token claims reference](https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference) — distinguish application-only identities from delegated user tokens and validate audience, tenant, application role, and token type.
