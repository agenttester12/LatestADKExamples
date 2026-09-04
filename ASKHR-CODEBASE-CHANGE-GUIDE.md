# AskHR codebase implementation guide

This is the single engineering handoff for applying all required AskHR
application changes in the real work-PC repository. It describes AskHR changes
only: token delivery and recovery, WxO response-language policy, qualified card
controls, routing latency, registry caching, active transactional continuity,
streaming, scale verification, rollout, and completion criteria. The `Agents/`
and `Tools/` directories beside this file are the separately transferable
watsonx Orchestrate package and must not be copied into the AskHR application
repository unless that repository already has an explicit location for
deployment artifacts.

No AskHR runtime source was changed while producing this handoff. The inspected
development checkout can differ from the work-PC checkout. The implementation
owner must reconcile every instruction with current work-PC source, tests,
handbook, and runbooks before editing; never paste an example over newer work.

## Outcome

After this change:

1. A published WxO agent may use `contextProfile.token: "in-context"`.
2. AskHR prepares a live, subject-bound `wd_access_token` and sends it directly
   in the WxO run context.
3. The WxO tool uses that token first. It does not perform OBO.
4. If Workday rejects the token with 401/403, the tool may call
   `POST /api/internal/resolve-token` once with `{ "sessionId": "..." }`.
5. That explicit callback asks the AskHR backend to perform a fresh OBO exchange
   when the bound session has an IDP assertion, even if the rejected token's
   recorded expiry still looks healthy.
6. The tool retries the Workday read once. A second authentication rejection
   ends the attempt and tells the employee to refresh/reopen AskHR.
7. The removed temporary token gate no longer blocks agent publication,
   discovery, or dispatch.
8. EVL and Work Offsite declare the namespaced
   `askhr_platform_tools:stage_card` and
   `askhr_platform_tools:report_action` tools needed
   by the existing AskHR WxO run manager.
9. AskHR bounds forced recovery to 10 requests per minute per session after
   authenticating the internal-agent caller.
10. WxO text deltas reach the employee immediately instead of waiting for a
    newline or run completion.
11. Cached registry data is used for discovery only; one fresh fail-closed read
    remains immediately before provider dispatch.
12. Registry, IBM IAM, and short outage-cache refreshes are coalesced so cache
    expiry does not create a concurrency herd.
13. A human-approved exact row that is unique among eligible overlapping scopes in the existing
    `route_utterances` corpus can bypass semantic routing without creating a
    second per-agent trigger list.
14. An active transaction routes ordinary follow-ups directly to its owning
    agent even when WxO does not return a `thread_id`.
15. Ambiguous first turns continue through bounded hybrid retrieval and the
    Tier-3 reranker; uncertainty still falls back to knowledge.
16. The existing no-network `askhr_platform_tools:stage_card` transport remains
    the WxO card path and is verified with exact names, schema checks, budgets,
    fallback text, and a live canary.
17. A realistic full-path suite proves routing correctness and latency from one
    through 100 agents at twice forecast peak concurrency.

## Non-negotiable security boundary

OBO remains entirely backend-owned.

- The widget never receives or stores a Workday token.
- WxO never receives `idpToken`, a SAML assertion, the SAML application secret,
  or any input needed to perform OBO.
- The agent receives only the resulting Workday bearer when its registry profile
  explicitly selects `token: "in-context"`.
- IBM defines declared context variables as available to the agent runtime; this
  delivery is not a documented model-versus-tool secret-isolation boundary.
  Treat WxO as a trusted bearer recipient, never interpolate the token into
  instructions, and never claim the YAML declaration makes it tool-only.
- The recovery request contains `sessionId` only. Do not add `employeeId`,
  `employeeKey`, the failed token, `forceRefresh`, or another selector to the
  HTTP body.
- AskHR loads the Redis session and chooses the employee/token server-side.
- `resolve-token` remains restricted to the `internal-agents` API-key identity.
- Its dedicated rate limit is keyed by `sessionId`, so one employee does not
  consume another employee's recovery allowance.
- Custom HTTP and Foundry continue stripping or refusing Workday token delivery
  according to their existing platform contracts. This change is for WxO.

The phrase “the agent refreshes the token” is shorthand only. The agent requests
recovery; AskHR performs the exchange and returns the replacement bearer.

## Repository preparation

Follow the work-PC repository's own `AGENTS.md` before this guide. Current AskHR
has two repository profiles:

- Historical source repository: create the delivery from freshly fetched
  `origin/main`.
- Clean work repository: create the delivery from freshly fetched `origin/dev`.
  If `origin/dev` is missing, stop instead of guessing another base.

Use an isolated worktree and one implementation owner. Do not edit a shared
checkout and do not push, merge, promote, release, or deploy without explicit
authorization.

Before editing, read:

```text
README.md
AGENTS.md
docs/handbook/engineering/00-codebase-map.md
docs/handbook/integrations/02-wxo-agents.md
docs/handbook/engineering/04-security-and-auth.md
.github/instructions/backend.instructions.md
.github/instructions/console.instructions.md
```

Record the base commit and inspect current code before applying anything:

```bash
git status --short
git rev-parse HEAD
rg -n "agentTokenGate|TOKEN_POLICY_BLOCKED|DiscoveryGate|resolveWorkdayToken|resolve-token" \
  packages config docs/handbook postman
```

If the work-PC branch has already removed or redesigned any of these symbols,
adapt to the current source and tests. Do not paste an old patch over a newer
implementation.

## Phase 1 — remove the temporary token interlock

### Delete the obsolete module

Delete both files:

```text
packages/backend/src/services/agents/agentTokenGate.ts
packages/backend/src/services/agents/agentTokenGate.test.ts
```

Do not replace them with another gate or feature flag. The remaining controls
already provide the correct boundaries: platform validation, environment,
publication state, geography, audience, health, fresh dispatchability, and
fail-closed context preparation.

### Remove publication blocking

Edit `packages/backend/src/routes/agentAdmin.ts`.

Remove the import of `getWorkdayTokenGateBlocker` and `AgentTokenPolicy`. Remove
the token-blocker checks from both publication paths:

1. The `PUT /agents/:agentKey` path when the effective status is published.
2. The `POST /agents/:agentKey/promote` path when promoting to published.

Keep the geo check and `publishReadinessBlocker` exactly where they are. The
desired shape is:

```ts
if (before && effectiveStatus === 'published') {
  const geoBlocker = getGeoGateBlocker({ ...before, ...effectiveUpdates } as AgentGeoPolicy);
  if (geoBlocker) {
    res.status(400).json({ error: `Cannot publish: ${geoBlocker}` });
    return;
  }

  const blocker = await publishReadinessBlocker({
    ...before,
    ...effectiveUpdates,
    status: 'published',
  });
  if (blocker) {
    res.status(400).json({ error: `Cannot publish: ${blocker}` });
    return;
  }
}
```

In the promotion route, preserve the existing publication audit lifecycle and
every non-token rejection reason. Remove only the `TOKEN_POLICY_BLOCKED` branch.

### Remove discovery blocking

Edit `packages/backend/src/services/agents/agentRegistry.ts`.

Apply four focused removals:

1. Remove the `agentTokenGate` import.
2. Remove `getWorkdayTokenGateBlocker(a)` from `applyFilters`.
3. Remove `'token'` from the `DiscoveryGate` union.
4. Remove the token result from `explainAgentDiscovery`, its `tokenPassed`
   variable, and the token term in `eligible`.

The base discovery gates should now be returned in this order:

```ts
['status', 'environment', 'region', 'audience', 'country', 'health']
```

`dispatchability` remains the separate fresh pre-dispatch result appended by the
session troubleshooter. Do not change the fresh registry read performed at the
irreversible dispatch boundary.

The final eligibility expression should be equivalent to:

```ts
const eligible =
  environmentPassed &&
  statusPassed &&
  regionPassed &&
  audiencePassed &&
  countryPassed;
```

### Update backend tests

In `packages/backend/src/routes/agentAdmin.test.ts`, change the former token-gate
publication test to prove that a review-state WxO agent with
`contextProfile.token: "in-context"` can be promoted. Assert HTTP 200 and one
registry update.

In `packages/backend/src/services/agents/agentRegistry.test.ts`:

- Expect an enabled, published WxO token consumer to remain in discovery.
- Expect `explainAgentDiscovery` to report it as eligible and discovered.
- Expect six base gates rather than seven.
- Expect no `token` gate in the ordered gate list.

Do not weaken environment, geography, audience, status, or health tests.

## Phase 2 — make explicit recovery perform a real backend refresh

The subtle bug to avoid is returning the same rejected cached token merely
because its recorded expiry has not elapsed. The regular per-turn payload path
should stay cache-efficient, while the explicit recovery endpoint should request
a new exchange.

### Add an internal resolver option

Edit
`packages/backend/src/services/auth/workdayTokenResolver.ts`.

Add a small internal option; do not add a new service or endpoint:

```ts
interface ResolveWorkdayTokenOptions {
  forceRefresh?: boolean;
}

export async function resolveWorkdayToken(
  sessionId: string,
  options: ResolveWorkdayTokenOptions = {},
): Promise<string> {
  const session = await getSession(sessionId);
  if (!session) {
    throw new WorkdayTokenError('No session found for sessionId', 404);
  }

  if (options.forceRefresh && session.idpToken) {
    return refreshWorkdayToken(sessionId, session.idpToken);
  }

  if (session.workdayToken && Date.now() < session.workdayTokenExpiry - 60_000) {
    return session.workdayToken;
  }

  if (session.idpToken) {
    return refreshWorkdayToken(sessionId, session.idpToken);
  }

  if (session.workdayToken) {
    if (Date.now() >= session.workdayTokenExpiry) {
      throw new WorkdayTokenError(
        'Workday token expired and cannot be refreshed on the service path',
      );
    }
    return session.workdayToken;
  }

  throw new WorkdayTokenError('No Workday token available for this session');
}
```

Preserve the existing per-session single-flight `inFlightRefresh` map and
`coalesce` call. Concurrent refresh requests for one session must still collapse
onto one exchange and one Redis write.

Why `forceRefresh` is not an HTTP field:

- Callers must not choose resolver policy.
- The recovery route always has recovery semantics.
- The HTTP request remains `{ sessionId }` only.
- Normal backend context preparation calls `resolveWorkdayToken(sessionId)`
  without the option and therefore keeps the valid-token fast path.

### Use forced mode only at the recovery route

Edit `packages/backend/src/routes/resolveToken.ts`:

```ts
const workdayToken = await resolveWorkdayToken(sessionId, {
  forceRefresh: true,
});
```

Do not change `ResolveTokenSchema`. It must remain strict:

```ts
const ResolveTokenSchema = z.object({
  sessionId: z.string().uuid(),
  action: z.literal('resolve-token').optional(),
}).strict();
```

Do not add an employee selector. Keep the `internal-agents` client check and the
existing safe error mapping.

### Bound repeated forced recovery

The existing `/api/internal` limit is shared by an Apigee egress address. Add a
narrower limiter in `packages/backend/src/routes/resolveToken.ts`, after
`apiKeyValidator`, so the key is an authenticated request's `sessionId`:

```ts
import { createRateLimiter } from '../middleware/rateLimit';

const resolveTokenRateLimit = createRateLimiter({
  name: 'resolve-token',
  windowMs: 60_000,
  limit: 10,
  bodyIdentityField: 'sessionId',
  useBearerIdentity: false,
});

const requireInternalAgent: RequestHandler = (req, res, next): void => {
  if (req.clientId !== 'internal-agents') {
    res.status(401).json({ error: 'Unauthorized' });
    return;
  }
  next();
};

export const resolveToken = [
  apiKeyValidator({ allowInternal: true }) as RequestHandler,
  requireInternalAgent,
  resolveTokenRateLimit,
  handleResolveToken,
];
```

The order is important. `apiKeyValidator({ allowInternal: true })` accepts
multiple server tiers, so the exact `internal-agents` client check must also run
before the limiter. Otherwise another valid server client could consume a
session's budget before receiving 401. Putting the limiter at the
`/api/internal` mount would pool unrelated sessions. The existing rate
limiter uses Redis outside tests, so the budget is shared across App Service
instances and its keys expire with the rate-limit window. Operators may tune
`RATE_LIMIT_RESOLVE_TOKEN_MAX` and `RATE_LIMIT_RESOLVE_TOKEN_WINDOW_MS`, but the
defaults should remain safe for the one-recovery-per-transaction contract.

Declare both generated environment controls in
`packages/backend/.env.example`, beside the existing internal rate-limit values:

```dotenv
# RATE_LIMIT_RESOLVE_TOKEN_WINDOW_MS=60000
# RATE_LIMIT_RESOLVE_TOKEN_MAX=10
```

Also add `RESOLVE_TOKEN` to the comment that enumerates limiter buckets. AskHR's
environment verification intentionally fails when a generated limiter is not
represented in this file.

### Add regression tests

In
`packages/backend/src/services/auth/workdayTokenResolver.test.ts`, add a test
where:

- The stored Workday token has substantial remaining lifetime.
- The session has `idpToken`.
- The caller passes `{ forceRefresh: true }`.
- `exchangeForWorkdayToken` returns a different token.
- The result is the new token.
- `updateSession` stores the new token and expiry.

Example assertion:

```ts
expect(await resolveWorkdayToken('sess-1', { forceRefresh: true })).toBe('fresh');
expect(exchangeForWorkdayToken).toHaveBeenCalledWith('idp-token');
expect(updateSession).toHaveBeenCalledWith('sess-1', {
  workdayToken: 'fresh',
  workdayTokenExpiry: expect.any(Number),
});
```

In `packages/backend/src/routes/resolveToken.test.ts`, assert that a valid request
delegates with:

```ts
expect(resolveWorkdayToken).toHaveBeenCalledWith(VALID_SESSION_ID, {
  forceRefresh: true,
});
```

Also send 11 valid authenticated requests for a fresh test session and assert
that the first 10 return 200, the eleventh returns 429, and the resolver runs 10
times. Use a session UUID not shared by the other route tests because the test
rate limiter keeps its in-memory counter for the module lifetime.

Add a second ordering regression: send 10 requests for one session as another
accepted server-tier client (for example `employee-portal-server`) and assert
they return 401; then send one request as `internal-agents` and assert it still
returns 200. This proves unauthorized server clients do not consume the
session's recovery budget.

Retain the tests proving:

- Unauthorized client identity returns 401.
- Missing or malformed `sessionId` returns 400.
- Extra fields, including `employeeId`, return 400.
- Missing Redis session returns 404.
- Unrecoverable token state returns 422.
- Unexpected infrastructure failures return a generic 500.
- Concurrent refreshes remain single-flight.
- Sequential forced recovery is bounded per session after authentication.

## Phase 3 — align the registry shells with the WxO package

### EVL shell

Edit `config/agents/evl_agent.json` so the relevant fields are:

```json
{
  "toolNames": [
    "evl_tools:evl_tools",
    "askhr_platform_tools:stage_card",
    "askhr_platform_tools:report_action"
  ],
  "contextProfile": {
    "fields": [
      "employee_id",
      "first_name",
      "region",
      "country_iso",
      "response_language"
    ],
    "sensitive": [],
    "token": "in-context"
  }
}
```

The empty `sensitive` array is intentional. AskHR's current context builder uses
that list to prefix selected keys with `_`; the transferred EVL agent declares
and reads the exact `wd_access_token` key. The token is still treated as a token
by platform stripping and trace-redaction logic; this setting controls the
context key name, not whether the value is secret.

Keep the existing EVL country gate. Do not broaden countries as part of this
change.

### Work Offsite shell

Edit `config/agents/work_offsite.json` so `toolNames` is the imported toolkit
surface, including the shared control toolkit:

```json
"toolNames": [
  "work_offsite_toolkit:view_offsite_requests",
  "work_offsite_toolkit:list_offsite_requests_for_action",
  "work_offsite_toolkit:validate_offsite_request",
  "work_offsite_toolkit:submit_offsite_request",
  "work_offsite_toolkit:cancel_offsite_request",
  "work_offsite_toolkit:modify_offsite_request",
  "work_offsite_toolkit:get_offsite_reasons",
  "askhr_platform_tools:stage_card",
  "askhr_platform_tools:report_action"
],
"contextProfile": {
  "fields": [
    "employee_id",
    "current_date",
    "response_language"
  ],
  "sensitive": [],
  "token": "none"
}
```

Do not configure Work Offsite for the employee OBO token. Its transferred tools
use the existing FlexWork service credential contract, not EVL's
`wd_access_token` flow. `current_date` is not a connection value; AskHR computes
it per turn from the validated session time zone as specified below.

### Environment enablement

Keep both shipped shells at:

```json
"enabledEnvironments": []
```

until the matching WxO toolkits and agents have been imported, connections have
been associated, and a live read/card/write smoke test has passed in an approved
development or QA environment. This is a rollout hold, not the removed token
policy gate. Enable only the environment that has evidence; do not enable UAT or
production speculatively.

## Phase 4 — remove the stale console contract

The backend no longer emits a `token` discovery gate. Remove that value from the
console so operator diagnostics do not advertise a nonexistent check.

Edit `packages/console/src/api/admin.ts`:

```ts
export type DiscoveryGate =
  | 'environment'
  | 'status'
  | 'region'
  | 'audience'
  | 'country'
  | 'health'
  | 'dispatchability';
```

Edit
`packages/console/src/components/admin/agents/DiscoveryPanel.tsx` and remove the
`token: 'Workday token'` entry from `GATE_LABELS`.

Edit `packages/console/mock/mockData.ts` and remove the two mock gate objects
whose gate is `token`. Keep all remaining gates in backend order.

Run this search afterward; it should return no active token-gate contract:

```bash
rg -n "Workday token gate|blockedBy.*token|gate: 'token'|TOKEN_POLICY_BLOCKED" \
  packages config docs/handbook postman
```

## Phase 5 — update current documentation and the API collection

Update these current-state pages. Describe behavior, not the history of this
conversation:

```text
docs/handbook/engineering/01-system-overview.md
docs/handbook/engineering/04-security-and-auth.md
docs/handbook/engineering/07-routing-and-agents.md
docs/handbook/engineering/10-api-reference.md
docs/handbook/engineering/14-routing-end-to-end.md
docs/handbook/engineering/15-ai-inventory.md
docs/handbook/integrations/01-agent-builder-guide.md
docs/handbook/integrations/02-wxo-agents.md
docs/handbook/integrations/09-evl-config-management.md
docs/runbooks/evl-connection-provisioning.md
packages/backend/.env.example
postman/AskHR.postman_collection.json
```

The documentation must say all of the following consistently:

- In-context Workday token delivery is supported for WxO.
- Normal context preparation uses the expiry-aware cache path and fails closed
  before dispatch if it cannot produce a usable token.
- An agent uses the payload token first.
- Only a missing token or Workday 401/403 triggers one resolver call and retry.
- The explicit route requests a fresh backend OBO exchange when the session has
  an IDP assertion.
- If an older session's stored assertion is no longer exchangeable, the short
  transaction fails cleanly and asks the employee to refresh/reopen AskHR. Do
  not add a mid-session assertion upsert unless product scope changes.
- A service-minted session has no IDP assertion to exchange. It may return a
  still-live service-supplied token, but it cannot manufacture a replacement.
- The agent never performs OBO and never receives the IDP assertion.
- The resolver body remains `sessionId` only, with only the tolerated legacy
  `action: "resolve-token"` field.
- Recovery has a dedicated 10/min per-session limit after the exact
  `internal-agents` client check.
- Publication now has the existing platform, geography, and readiness checks;
  there is no separate token-policy gate.
- Discovery has six base gates, plus the separate fresh dispatchability result.
- Both agents use in-stream `askhr_platform_tools:stage_card` and
  `askhr_platform_tools:report_action` tool calls.

Update the Postman resolve-token request description with the same forced
backend-recovery semantics. The request body itself does not change.

Update `docs/runbooks/evl-connection-provisioning.md` as an operational contract,
not history: add the required `resolve_token_url` connection key, use the exact
`wd_access_token` context name, and add live acceptance checks for the direct
token fast path and the one resolver-assisted retry.

If `BUILD-STATUS.md` exists in the development checkout, add a fresh top entry
only after verification. A filtered work-PC checkout may intentionally omit it;
in that case put the base commit, verification results, and external live-smoke
gap in the pull request instead.

## Behavior examples

### Valid in-context token

```text
AskHR buildAgentContext
  -> resolveWorkdayToken(sessionId) uses cached live token
  -> WxO context contains wd_access_token
EVL
  -> calls Workday through the configured endpoint with that bearer
  -> Workday returns 200
  -> no resolve-token callback
```

This is the common fast path. It adds no resolver round trip.

### Workday rejects a token whose expiry still looks healthy

```text
EVL -> Workday: Authorization: Bearer old-token
Workday -> EVL: 401 or 403
EVL -> AskHR: POST /api/internal/resolve-token { sessionId }
AskHR -> Redis: load subject-bound session
AskHR -> Entra/Workday: backend-owned OBO exchange using stored idpToken
AskHR -> Redis: update encrypted Workday token and expiry
AskHR -> EVL: { workdayToken: "fresh-token" }
EVL -> Workday: one retry with fresh-token
```

The agent never sees the IDP token or SAML assertion.

If several EVL report reads reject the same token in parallel, the toolkit
shares one recovery call. AskHR independently collapses simultaneous refreshes
for the same session and applies the per-session recovery budget to sequential
requests.

### Workday returns a non-authentication failure

```text
EVL -> Workday: current payload token
Workday -> EVL: 429 or 500
EVL: return the bounded unavailable message
```

Do not call `resolve-token` for 429, 5xx, malformed JSON, or a network timeout.
Refreshing credentials cannot fix those conditions and only adds latency.

### Second authentication rejection

```text
Workday rejects payload token
AskHR returns replacement token
Workday rejects replacement token
EVL stops; no third Workday call; no second resolver call
```

The employee receives the refresh/reopen message. Do not loop.

### Service-minted session

A server-to-server session may have a Workday token but no `idpToken`. AskHR
cannot run a new employee OBO exchange for that session. It must continue to
fail closed after actual expiry. Do not move service credentials or exchange
logic into the agent to work around this boundary.

### Attempted subject override

This request must remain invalid:

```json
{
  "sessionId": "550e8400-e29b-41d4-a716-446655440000",
  "employeeId": "someone-else"
}
```

Expected result: HTTP 400 `Unexpected fields`, with no token lookup or exchange.

## Card and action contract

No new card framework is required in AskHR. IBM Python toolkit imports expose
tools as `toolkit_name:tool_name`, so update `wxoRunManager.ts` to recognize the
two exact namespaced controls. Do not retain bare names or match arbitrary names
by suffix:

```ts
const STAGE_CARD_TOOL_NAME = "askhr_platform_tools:stage_card";
const REPORT_ACTION_TOOL_NAME = "askhr_platform_tools:report_action";
```

Pass the dispatched registry entry's `toolNames` into `sendWxOMessage`. Build a
set once per run and check every streamed name before parsing its arguments or
performing any side effect. For a name that is not attached to that agent, add
only the fixed marker `unrecognized_tool` to telemetry and continue. Never put
the raw unknown name into telemetry, logs, reasoning SSE, or analytics: the WxO
stream is external input and the name could contain employee data or a token.
For an allowed event, continue recording the exact qualified name for useful
deployment telemetry.

Pass `[agent.name]` as a separate trusted display-name allowlist. Accept
`step_details[].agent_display_name` into telemetry only when it exactly matches
that configured value. Otherwise ignore it. Do not use a raw provider display
name in widget reasoning or as the persisted analytics `agentKey`; it is also
external text and can carry employee data or secrets. If a future supervisor
needs collaborator-level telemetry, add the expected collaborator names to the
registry contract explicitly rather than accepting arbitrary stream values.

Use exact equality with `STAGE_CARD_TOOL_NAME` and `REPORT_ACTION_TOOL_NAME` at
the existing interception points. This prevents an unrelated toolkit or a bare
legacy name from impersonating a control tool.

The run manager then intercepts a toolkit event such as:

```json
{
  "name": "askhr_platform_tools:stage_card",
  "args": {
    "card": {
      "kind": "confirm",
      "cardId": "evl-confirm",
      "title": "Confirm letter request",
      "facts": [],
      "confirmLabel": "Send",
      "cancelLabel": "Cancel"
    }
  }
}
```

AskHR validates `args.card` with `ChatBlockSchema`, stages it in Redis, and emits
the typed SSE card event after the WxO run completes. Do not add Adaptive Cards,
Bot Framework, `%%RENDER%%`, or another rendering protocol.

For actionable cards, AskHR also generates a per-emission `submissionId`, stores
a one-time token bound to the session, card, and staging agent, atomically
consumes it on submission, and owner-routes the confirmed turn. Missing,
expired, forged, and replayed submissions are rejected before agent dispatch.
Keep that existing boundary intact; it is why the tool-level `confirmed=false`
default is defense in depth rather than the sole confirmation control.

The transferred agents emit `askhr_platform_tools:report_action` only after a
tool result explicitly confirms success. The HTTP `report-action` callback remains a no-op
acknowledgement; actual transaction telemetry comes from the in-stream tool call.

## Verification

Use failing-first targeted tests while implementing, then run the complete gate.
Backend Supertest suites need permission to bind local ephemeral ports.

Targeted checks:

```bash
npm test -w packages/backend -- --run \
  src/services/auth/workdayTokenResolver.test.ts \
  src/routes/resolveToken.test.ts \
  src/services/agents/agentContext.test.ts \
  src/services/agents/agentRegistry.test.ts \
  src/routes/agentAdmin.test.ts

npm test -w packages/console -- --run \
  src/components/admin/agents/DiscoveryPanel.test.tsx

npm run config:validate -w packages/backend
```

If the named console test does not exist on the work-PC branch, use the package
type check and existing discovery/mock preview coverage; do not create a shallow
snapshot test solely to satisfy this line.

Full completion gate:

```bash
npm run verify
```

Final hygiene:

```bash
npm run verify:ai
npm run verify:forbidden
git diff --check
git status --short
```

Review the final diff manually. Generated build outputs, validation reports,
dependency folders, temporary cards, and synthetic inputs must not appear in
the change unless they are already tracked outputs intentionally refreshed by
the repository workflow.

## Response-language configuration

Do not add IBM agentic-workflow locale files for these native agents. IBM's
workflow multi-language feature translates user-activity labels, help text,
buttons, choices, and error messages; it does not translate dynamic variables,
tool outputs, workflow logic, or native-agent prose.

Because IBM does not translate native-agent tool output automatically, each
agent must translate the natural-language prose in employee-facing tool `text`
according to `response_language`. It must preserve the meaning and every
card payload, date, Workday value, ID, URL, action type, status, and other
factual or opaque value. If `response_language` is unexpectedly absent or
invalid, the tool text and all other conversational prose stay in English.

AskHR already owns the native-agent language boundary in `chatMessage.ts`:

1. The registry shell must include `response_language` in
   `contextProfile.fields` (both transferred shells do).
2. Global language support and multilingual mode must be enabled.
3. The current turn's resolved non-English locale must appear in the App Config
   value `AskHR:AgentResponseLanguages`.
4. The locale must also appear in AskHR's fixed IBM WxO transactional ceiling.
5. AskHR sends the approved non-English locale when all checks pass; otherwise
   it sends `response_language: "en"`. The agent never has to infer the fallback
   from a Polish or other unsupported-language message.

### Backend implementation specification

The broader knowledge-language taxonomy is not the WxO capability boundary.
Do not remove or narrow knowledge languages to implement this change. Instead,
edit `packages/backend/src/services/config/featureFlags.ts` and place one
immutable non-English ceiling beside `AskHR:AgentResponseLanguages`:

```ts
const WXO_TRANSACTIONAL_RESPONSE_LANGUAGES = new Set([
  'fr',
  'es',
  'de',
  'it',
  'ja',
  'ko',
  'zh-CN',
  'zh-TW',
  'pt-BR',
]);
```

English is deliberately absent from this set because it is not configurable;
AskHR sends it as the explicit fallback. Filter the already-canonicalized App
Configuration list against the set:

```ts
function parseLanguageList(raw: string): string[] {
  return canonicalizeLanguageList(
    raw.split(',').map(language => language.trim()).filter(Boolean),
  ).filter(language => WXO_TRANSACTIONAL_RESPONSE_LANGUAGES.has(language));
}
```

In `packages/backend/src/routes/chatMessage.ts`, initialize the effective
transactional language to English before checking the approved non-English
subset:

```ts
let responseLanguage = 'en';
if (agent.contextProfile?.fields.includes('response_language') &&
    (await isMultilingualEnabled())) {
  const resolved = turnCtx.language;
  const enabledLanguages = await getAgentResponseLanguages();
  if (resolved !== 'en' && enabledLanguages.includes(resolved)) {
    responseLanguage = resolved;
  }
}
```

Continue passing that value through the existing `buildAgentContext` call. The
context builder emits it only for agents whose profile selects
`response_language`. This makes an unsupported Polish transactional turn
unambiguous: routing still sees the Polish request, but WxO receives
`response_language: "en"` and follows the English instruction.

Reuse the existing parser, 30-second cache, coalesced App Configuration read,
and warm last-known-good behavior. Do not add another service, network call,
connection value, agent-local locale list, or environment-variable override.
The filtering happens only when the cached setting is refreshed; normal turns
perform a cached membership check, so this adds no material request-path work.

### App Configuration

Set these existing keys for each environment where localized transactional
agent output is approved:

```text
AskHR:LanguageSupportEnabled = true
AskHR:MultilingualEnabled = true
AskHR:AgentResponseLanguages = fr,es,de,it,ja,ko,zh-CN,zh-TW,pt-BR
```

`AskHR:AgentResponseLanguages` may contain a narrower rollout subset. It can
never widen the code ceiling: knowledge-only values such as `nl` or `pl`,
malformed tags, and English are discarded. Empty or unavailable cold
configuration means English-only transactional agents. Do not put this list in
`EVLConnection`, `FlexWork`, or a process environment variable; those are not
the authority for conversational behavior.

### Required behavior

| Current-turn locale | Knowledge support | Configured transactional subset | WxO context |
| --- | --- | --- | --- |
| `es` | supported | contains `es` | `response_language: "es"` |
| `pt_BR` | supported | contains `pt-BR` | canonicalized to `response_language: "pt-BR"` |
| `nl` | supported | even if mistakenly configured | `response_language: "en"` |
| `en` | supported | any value | `response_language: "en"` |
| malformed/unknown | irrelevant | any value | `response_language: "en"` |

Do not use another identity-context field as a fallback inside either agent;
that would bypass the current-turn resolution and the transactional ceiling.

Retain the existing backend tests proving that an approved locale such as `es`
is emitted, a configured regional locale such as `pt-BR` keeps its exact form,
English, unsupported, and unvetted locales produce explicit `en`. Add a
`featureFlags.test.ts` case proving that canonicalization and deduplication run
before the IBM ceiling, and that `nl`, `pl`, and `en` cannot enter the effective
non-English list even when an operator configures them. In the agent package,
retain the test that both YAML instructions use only
`response_language`, translate employee-facing tool prose when it is present,
and fail closed to English.

## Employee-local date context for Work Offsite

Work Offsite must not resolve “today,” “tomorrow,” weekdays, date-picker
minimums, or past dates from the WxO container clock. That clock can be a
different calendar day from a global employee. Add `current_date` to the Work
Offsite registry shell's `contextProfile.fields`; the transferred YAML and
Python toolkit already consume that exact key.

Likely current-worktree owners are the widget session bootstrap in
`packages/widget/src/hr-chatbot.ts` and its token/session service, the matching
authenticated session-creation route/service, the Redis `SessionContext`, and
`buildAgentContext`/`chatMessage.ts` with focused tests. Follow the current
work-PC symbols if names have moved; do not create a parallel session store.

Keep this a session/bootstrap concern, not a new per-turn service call:

1. During authenticated widget session initialization, collect the browser's
   IANA time-zone name from `Intl.DateTimeFormat().resolvedOptions().timeZone`.
2. Validate that string in the backend by constructing `Intl.DateTimeFormat`
   with it and rejecting unknown values, control characters, or an excessive
   length. Store only the validated zone in the existing Redis session. Do not
   infer a time zone from country or formatting locale; several countries span
   multiple zones.
3. At agent-context construction time, compute the current calendar date on
   the server with that session-bound zone and emit strict `YYYY-MM-DD` as
   `current_date`. Build it from `formatToParts`; do not depend on locale-formatted
   string ordering. The model never supplies this value as a tool argument.
4. If an older client supplies no valid zone, omit `current_date`. The toolkit
   then stops date-dependent reads or writes with a refresh message instead of
   guessing. Do not fall back to server UTC for a transaction.

The browser-reported zone is a UX reference, not authorization: the employee
can already choose explicit dates, confirmation remains mandatory, and Workday
remains the system of record. Still bind the validated zone to the authenticated
session so prompt text cannot change it. Add tests for UTC-midnight boundaries,
DST zones, malformed/unknown zones, missing context, and an exact ISO value
reaching WxO. This adds no external call and only one local date-format operation
on an agent turn.

Deploy backend/session support and the widget time-zone field before enabling
the updated Work Offsite shell. Existing sessions must refresh once to acquire
the new session value; that is the deliberate safe fallback, not a reason to use
server UTC.

## Live deployment evidence

Local verification cannot prove tenant wiring. Before enabling an environment:

1. Import the three transferred Python toolkits into that WxO tenant.
   Use the current WxO Python 3.12 tools runtime and confirm every exact package
   pin is present in the tenant allowlist.
2. Associate `EVLConnection` with `evl_tools` and `FlexWork` with
   `work_offsite_toolkit`. The shared AskHR platform toolkit has no connection.
3. Import both agent YAML files.
4. Confirm every named tool resolves on each agent.
5. Run an EVL read with a valid in-context token and prove no resolver request is
   made.
6. In a safe test session, simulate one Workday 401/403 and prove exactly one
   `{ sessionId }` resolver request and one Workday retry.
7. Prove the resolver exchange happens in AskHR logs, not WxO, without logging
   token values.
8. Render every selection, form, confirmation, choice, and table card in the
   AskHR widget.
9. Confirm cancel/modify uses the opaque `requestRef` returned by the selected
   row and cannot target a reordered request.
10. Confirm `askhr_platform_tools:report_action` is emitted only after an
    explicit successful write.
11. Sync the WxO agent identifiers into the matching AskHR environment, apply
    the registry configuration, run the AskHR agent connection test, and only
    then publish/enable that environment.

## Routing performance and 100-agent scale design

This section addresses the observed 12–19 second WxO-agent path, including slow
follow-ups. Knowledge routing is not the primary symptom. Do not tune a global
threshold or replace the architecture before measuring the full request path.
The inspected source contains several concrete fixed delays and one employee-
visible streaming defect that are sufficient to explain why the native WxO
widget can feel fast while the same agent feels stalled through AskHR.

### Root-cause evidence to confirm on the work PC

Trace the current implementations before editing. File names or line numbers can
have moved, but the behavior must be classified as present, already fixed,
partially fixed, or intentionally redesigned.

1. `packages/backend/src/services/chat/tieredRouter.ts` sends every agent winner
   from Tier 2 to the Azure OpenAI Tier-3 reranker. Existing tests explicitly
   pin this rule. Therefore an exact stored agent utterance still pays embedding,
   Atlas search, and an LLM call.
2. With multilingual Auto enabled, `chatMessage.ts` can call
   `analyzeKnowledgeQuery` before routing. This extra model call also runs on
   turns that ultimately enter an active agent flow.
3. Most typed follow-ups re-enter routing. A longer follow-up can pay an
   active-flow similarity embedding and then the normal route embedding, Atlas
   aggregation, and Tier-3 reranker.
4. `activeFlow` is currently persisted inside `setAgentThread`. The chat route
   calls that helper only when a provider `thread_id` was captured. A successful
   WxO turn without a thread ID therefore establishes no AskHR transaction
   owner, forcing the next turn through normal routing.
5. The agent path performs a confirmed-outage Mongo lookup and two sequential
   uncached registry reads before dispatch. Security requires one fresh read at
   the irreversible provider boundary, not two back-to-back reads.
6. Registry and IBM IAM refreshes do not currently coalesce concurrent expiry
   misses. A busy instance can stampede Mongo or IBM IAM when a cache expires.
7. `wxoRunManager.ts` appends text into `lineBuffer` and yields only complete
   newline-delimited lines or the final tail at `done`. A one-paragraph response
   can reach AskHR from IBM quickly but remain invisible to the employee until
   the whole run completes.
8. `serveAgentAnswer` remotely asserts the Redis turn lease for every provider
   event even though a renewal timer also runs. This may serialize a Redis round
   trip into every streamed event. Measure it before changing the fence.
9. The current synthetic 100-agent script calls the router directly, uses
   generic requests that do not target the generated agents, and does not assert
   destination correctness. It cannot prove full-path readiness.

These are source findings, not production timings. First reproduce the same
utterance through the direct WxO API and AskHR under the same model, tenant,
warm/cold state, and language mode. Record both first provider text and first
visible widget output.

### Target routing architecture

Use one router with three bounded lanes:

```text
employee turn
  -> active transaction owner, if one is live
  -> trusted server-owned route hint or unique approved exact row
  -> otherwise existing hybrid retrieval and bounded Tier-3 reranker
  -> exactly one fresh authorization read
  -> provider adapter
```

The catalog size does not make the reranker prompt linear: the current code
passes at most ten destination candidates. At 100 agents, the real risks are
overlapping utterances, retrieval recall, fixed external calls per turn, cache
expiry herds, provider/tool concurrency, and database/Redis pressure.

Do not add a second router, domain hierarchy, workflow engine, response cache,
or all-agent LLM prompt. Do not enable a generic semantic-score fast path: RRF
scores and margins are rankings rather than calibrated authorization
probabilities, and their distribution changes as agents are added.

### Exact routing without per-agent `exactTriggers`

Do not add an `exactTriggers` field. It would duplicate the existing
`route_utterances` corpus and create needless maintenance for every agent.

Derive a small, independently coalesced exact index from existing confirmed
route rows. In the first release, terminal agent authority is limited to
`source: "manual"` rows that an administrator deliberately approved. Generated
seed rows and traffic-promoted rows remain useful retrieval evidence but do not
bypass Tier 3. If the current work-PC schema has a stronger explicit approval
provenance, use it instead of inventing another boolean.

Do not change the existing `normalizeText` storage or hybrid-retrieval contract;
that would require a migration and re-embedding decision. Add a focused
`normalizeExactText` used only by manual-write collision validation, exact-index
construction from each row's original `text`, and exact request lookup:

```text
Unicode NFKC -> lowercase -> trim -> collapse internal whitespace
```

This requires no persisted-field backfill. Do not remove words or punctuation.

A cached exact decision is discovery evidence, not final authority. Store the
durable exact-corpus revision with each index snapshot. Every mutation that can
change exact ownership or create a knowledge collision must update that
revision atomically with its data change. If the current Mongo deployment cannot
provide that atomicity, leave exact routing in `off` or `shadow`; do not invent
an eventually consistent substitute.

Build each index from a consistent corpus view. Prefer one Mongo snapshot
transaction that reads the revision and all authoritative agent/knowledge rows.
If the current data layer cannot use a snapshot transaction, use the bounded
equivalent: read revision, read rows, read revision again, and cache only when
both revision values match. On mismatch, retry once; another mismatch or any
read failure leaves the exact lane unavailable and falls through to semantic
routing. Never read rows and then stamp them with a later revision.

Immediately before an exact dispatch, read the small durable revision document
from Mongo and require it to equal the snapshot revision. A mismatch, timeout,
or read failure falls through to the semantic router. This check is separate
from the final fresh `agent_registry` authorization read: registry freshness
cannot prove that route evidence was not deleted, reassigned, or newly blocked
by knowledge. Pub/sub remains the fast invalidation path; the revision read is
the missed-message safety boundary.

A request exact-routes only when:

- one normalized manual row matches exactly;
- exactly one currently eligible published agent owns it;
- no confirmed knowledge row owns the same normalized text;
- environment, region, country, role, health, context-profile, and token rules
  permit that agent;
- no other active transaction owns the session;
- a fresh durable exact-corpus revision check matches the cached snapshot; and
- the final fresh registry authorization succeeds.

Reject overlapping authoritative rows in overlapping scopes during the admin
write. Also check at runtime: any collision falls through to the semantic router
and never picks the first database row.

Examples suitable for manual exact approval include “view my work offsite
requests” and “request an employment verification letter.” Broad phrases such
as “work from home,” “letter,” “leave,” and “help with my request” remain
semantic. Existing route-example administration is the approval experience;
there is no separate trigger screen or refresh button.

Use one kill-switch mode and one centrally configured rollout allowlist, never
hardcoded agent keys:

```text
AskHR:FastExactAgentRouteMode=off | shadow | live
AskHR:FastExactAgentKeys=<comma-separated approved agent keys>
```

Shadow mode records the proposed destination and saved calls but preserves the
current decision. Live mode is inert for a destination not in the allowlist.
Populate that allowlist only after the complete corpus and load evaluation
passes, initially with one destination.

### Active transactional continuity

Provider continuity and AskHR routing ownership are different state:

```ts
interface ActiveFlow {
  agentKey: string;
  expiresAt: string;
  responseLanguage?: string;
}

interface AgentStateUpdate {
  activeFlow: ActiveFlow;
  providerThreadId?: string;
}
```

Do not activate ownership after every successful agent answer. Activate or
refresh it only after AskHR successfully delivers an objectively actionable
pending interaction from that agent: a form, confirm card, or selectable table.
A generic choice card is not enough because Work Offsite also uses one to show
an informational reason list. A text-only answer, generic choice, or
non-selectable read-only table does not capture later topics. Choice-card
submissions still route through their one-time owner-bound submission token. If
the provider contract later gains an explicit trusted transactional-intent
marker, it may activate a choice flow only after the same tests and rollout
controls pass.

Persist `activeFlow` independently of whether WxO supplied a `thread_id`.
Update `agentThreads[agentKey]` only when a validated provider thread ID exists.
Preserve the existing expected-prior-thread conflict check.

While ownership is live:

- valid card submissions continue to use their one-time, owner-bound token;
- ordinary follow-ups go directly to the owning agent without embedding,
  Atlas search, or reranking;
- authorization and health are revalidated before every provider dispatch;
- the trusted locale-neutral AskHR `leave_task` widget action clears ownership;
- English `start over` and `exit` remain convenience phrases, not the
  multilingual correctness boundary;
- a successful terminal `report_action`, explicit exit, or reset atomically
  writes `activeFlow: null`; this terminal state takes precedence over any card
  observed earlier in the same run;
- cancellation before a write clears ownership;
- revocation, suspension, or confirmed unavailability invalidates ownership;
- a transient provider/infrastructure failure does not create or refresh a new
  flow, but preserves a previously pending flow until expiry so the next turn
  is not silently reclassified; never auto-repeat an uncertain write; and
- `expiresAt` slides to ten minutes after each successfully delivered actionable
  interaction in the same flow, then expires.

Ten minutes is a fixed safety window with margin for the expected sub-five-
minute transaction. Do not add another setting until operations data justifies
one. The widget must identify the active task and render a localized “Leave
task” or “Start over” control whose submitted value is the stable
locale-neutral `leave_task` action. This is required in the initial live
contract for every enabled locale; do not maintain a fuzzy backend dictionary
of translated exit phrases. This prevents a new topic from being silently
interpreted inside the old transaction.

Use the existing authenticated `POST /api/session/reset` transport for this
control; do not send “leave task” as chat text and do not let it enter routing or
WxO. The widget maps its stable internal `leave_task` action to `resetSession`,
first aborting any local stream. Extend reset from “409 while a turn is active”
to a fenced reset/takeover operation: atomically invalidate the old turn owner
and clear `activeFlow`, `agentThreads`, and `turnHistory`. The running chat must
observe lease loss, abort its upstream request, and emit no later delta, card,
continuity update, or active-flow write. The reset generation/owner fence must
make same-run completion unable to resurrect state.

Expose only safe task presentation state to the widget: `{ active, label,
expiresAt }`, where `label` comes from the trusted registry entry rather than a
provider event. Emit it as a typed SSE `task_state` event when state changes and
include the same shape in authenticated session restoration so refreshes retain
the control. Do not expose provider thread IDs or treat the client copy as
routing authority.

Legacy session rows containing only `{ activeFlow: { agentKey } }` have no
expiry or objective activation evidence. Decode them safely but treat them as
expired/fallback-only; do not direct-route them.

Use one reversible mode and a centrally configured rollout allowlist:

```text
AskHR:ActiveFlowDirectMode=off | shadow | live
AskHR:ActiveFlowAgentKeys=<comma-separated approved agent keys>
```

Do not keep the current topic-similarity embedding in the live direct path; that
would preserve the latency and ambiguous capture this flow state is designed to
remove. In shadow, compare direct ownership with the current router and review
all disagreements before adding one transactional agent at a time to the
allowlist. The mode remains the global emergency kill switch.

Read both modes and both allowlists through AskHR's existing centrally refreshed,
coalesced App Configuration path—not process environment variables. Validate
the mode as the closed enum `off | shadow | live`; missing, malformed, or cold
configuration means `off`. Preserve the bounded cache and warm last-known-good
behavior, and prove an operator change to `off` reaches every App Service
instance within that documented refresh bound without a restart.

### Preserve response-language behavior

The earlier response-language specification remains authoritative.

- Card submissions use the language bound into the one-time submission token.
- A prompt-proven short continuation can reuse `activeFlow.responseLanguage`.
- A substantive Auto-mode free-text turn still resolves language so a genuine
  mid-conversation language switch works.
- Unsupported transactional locales resolve to explicit English before WxO.
- A user-pinned supported language remains authoritative.

Where independent, begin language resolution concurrently with cached registry
discovery or general routing, then await it before provider dispatch. Do not add
another locale list, agent connection, or environment variable.

## Registry caching and dispatch authorization

The inspected `agentRegistry.ts` already caches published/suspended discovery
rows in process for five minutes and allows a bounded ten-minute stale fallback
during a transient Mongo failure. Keep those defaults unless real measurements
justify a shorter discovery TTL. Do not cache authorization for 24 hours.

Registry rows contain publication, suspension, environment, geography,
audience, platform, context, and token-delivery policy. A missed notification
must not leave a revoked agent dispatchable for a day.

Use this explicit split:

```text
short in-process snapshot -> discovery and routing candidates only
fresh indexed Mongo read  -> authorization immediately before provider dispatch
```

At cache expiry, concurrent callers await one refresh promise. Reuse the
repository's existing coalescing primitive rather than adding a cache package:

```text
fresh snapshot                 -> return immediately
expired, no refresh            -> start one Mongo read
expired, refresh in progress   -> await that promise
refresh succeeds               -> atomically replace snapshot
refresh fails within max stale -> serve bounded stale discovery
refresh fails beyond max stale -> fail closed
```

Create one dedicated registry/routing invalidation publisher and one dedicated
Redis subscriber connection; a subscribed Redis connection must not be reused
for normal commands. Wire subscriber start and clean shutdown into the existing
application lifecycle. Centralize publication so these successful mutations
cannot forget it: agent create/sync/publish/update/suspend/resume/delete,
automatic suspension/recovery, manual utterance add/delete, seed replacement,
classifier promotion/deletion, and knowledge-corpus reseed. Every App Service
instance clears its local registry snapshot and the independently coalesced
exact index. Send only a version/invalidation signal—never registry or utterance
contents. The five-minute TTL is the missed-message backstop. A manual refresh
button is not a correctness mechanism and must not be required.

Immediately before sending employee text, identity, or token to a provider,
perform one bounded indexed read requiring the exact key, published state,
current environment, current eligibility, supported platform/context policy,
and expected registry revision. Mongo failure here fails closed and is breaker-
neutral because it is an AskHR control-plane failure. Remove the earlier
duplicate fresh read only after a race test proves that a revision or revocation
during context preparation is caught by this final read.

Verify a unique `agent_registry.agentKey` index exists. Capture the final
authorization query's `explain` plan in DEV/QA and require indexed equality
rather than a collection scan. Measure server execution, connection-pool wait,
and end-to-end p95/p99 at twice forecast peak; one query per turn is safe only
when the pool and database meet that load.

Add single-flight behavior to registry and IBM IAM refresh. Keep the existing
IAM expiry-minus-60-seconds behavior. Cache confirmed outage discovery per
region for only 15–30 seconds, coalesce misses, invalidate after outage
mutations, and permit only a bounded last-known-good fallback. A breaker-open or
registry-suspended state remains immediate.

## WxO streaming and lease safety

Change `sendWxOMessage` so each non-empty `message.delta` is yielded as soon as
it is decoded. Preserve the streaming `TextDecoder` for split UTF-8 sequences,
join `multiple_content` parts in order, enforce byte/event/character budgets,
retain whitespace needed to reconstruct the provider response, and do not add a
synthetic newline. Do not flush an already-yielded tail again at `done`.

Track separate monotonic spans:

- provider credential lookup/refresh;
- provider HTTP connection;
- first provider event;
- first nonblank provider text;
- first successful server SSE write; and
- provider and full-turn completion.

The old `firstTokenMs` is not enough: it can show a fast IBM token while the
employee waits behind `lineBuffer`. Name the server metric
`requestToFirstSseWriteMs`; an Express `res.write` is not proof of browser paint.
Measure true employee-visible time in the synthetic browser journey. Add a
PII-free widget render beacon only if operations truly needs production paint
telemetry; do not conflate it with the server metric.

Instrument remote turn-lease assertion count and cumulative/p95 time, but retain
the current per-event remote check in this delivery. A cadence-based check can
emit stale deltas after a forced reset or ownership change and does not preserve
the current absolute fence. Optimize elsewhere first. Any later lease change is
a separate security-sensitive design that must keep every mutation in the same
Lua/WATCH fence, abort upstream on loss, and prove zero post-loss deltas under a
forced takeover/reset race. If equivalence cannot be proven, keep the current
check.

## WxO card payload and transport

The card section above remains the source of truth. `stage_card` receives the
complete AskHR ChatBlock built by a business tool. A Work Offsite form looks like
this (dates vary at runtime):

```json
{
  "kind": "form",
  "cardId": "offsite-submit-form",
  "title": "Submit a work-offsite request",
  "subtitle": "Choose your dates and reason. For a single day, use the same date for both.",
  "submitLabel": "Review request",
  "cancelLabel": "Cancel",
  "fields": [
    { "type": "date", "id": "start_date", "label": "Start date", "required": true, "min": "2026-09-04" },
    { "type": "date", "id": "end_date", "label": "End date", "required": true, "min": "2026-09-04" },
    {
      "type": "select",
      "id": "reason",
      "label": "Reason",
      "required": true,
      "options": [
        { "value": "Business Reason", "label": "Business Reason" },
        { "value": "Other Reason", "label": "Other Reason" },
        { "value": "Remote Flexibility Benefit", "label": "Remote Flexibility Benefit" }
      ]
    }
  ]
}
```

Current kinds are:

- `form`: fields such as date, text, and select inputs;
- `choice`: title plus value/label/description options;
- `confirm`: title, non-sensitive facts, and confirm/cancel labels; and
- `table`: columns, string-valued rows, and optional single-selection controls.

The Python function remains intentionally small and connection-free:

```python
@tool
async def stage_card(card: dict) -> dict:
    card_id = card.get("cardId") if isinstance(card, dict) else None
    return {"ok": True, "cardId": card_id}
```

The side effect occurs in AskHR: it observes the exact qualified invocation,
validates `args.card` with `ChatBlockSchema`, stages it under the current turn
lease, resolves it only after successful WxO completion, creates a one-time
owner-bound submission token, and emits the typed card SSE event. The Python
container cannot mutate AskHR Redis without a network call and must not receive
direct Redis access.

Do not make the marker tool POST through Apigee. That would retain the model-
selected extra tool step and stream dependency while adding credentials,
DNS/TLS, gateway/backend availability, retry/idempotency work, and the current
shared `/api/internal` IP-keyed 120-per-minute bucket. Keep HTTP card staging for
the existing Custom HTTP contract.

Harden the stream adapter as specified earlier: exact qualified names, selected-
agent `toolNames` allowlist, trusted configured display name, object/string
argument parsing, ChatBlock validation, duplicate/conflict handling, existing
resource budgets, lease fencing, successful-terminal gating, no raw external
names or arguments in telemetry, and no backend reconstruction of business copy.

The adapter cannot reconstruct a business tool's `text` if the agent omits it;
the documented stream exposes model deltas and tool-call arguments, not every
earlier business-tool result. Fallback-text adherence belongs in each agent's
instructions and live evaluation. The backend validates and transports what it
receives; it must not invent fallback copy from card data.

Capture a sanitized real WxO SaaS event fixture and operate a harmless live card
canary. IBM documents the top-level event but leaves nested event `data`
generic, so this adapter is a monitored provider contract.

Measure `business tool complete -> stage_card observed -> WxO done -> widget
card delivered`. Keep this design if valid stage-to-widget reliability is at
least 99.9% and staging latency meets the agreed p95 target at twice forecast
peak. Only consider a deterministic same-business-tool callback if measured
evidence fails that gate. Such a future migration first requires production-
ready Apigee, callback capacity separated from the shared IP bucket, a bounded
turn-scoped card index, idempotency/conflict rules, and a temporary shadow
migration. Do not maintain both paths permanently.

Do not adopt IBM-native widgets as AskHR's cross-provider card schema. AskHR
must remain consistent across WxO, Custom HTTP, and Foundry; IBM widget state is
provider-specific and IBM currently documents widget limitations.

## Routing implementation phases

Implement these phases independently and keep their rollback boundaries small.

### Phase R0 — baseline and observability

Likely files:

- `packages/backend/src/types/agentTelemetry.ts`
- `packages/backend/src/types/tieredRouteResult.ts`
- `packages/backend/src/routes/chatMessage.ts`
- `packages/backend/src/services/agents/wxoRunManager.ts`
- `packages/backend/src/services/ops/analyticsLogger.ts`
- `packages/backend/src/services/ops/sessionTroubleshooter.ts`
- matching tests and current handbook pages

Add non-sensitive spans for lease acquisition, session load, config/registry
discovery, language analysis, route total/embedding/search/rerank, outage lookup,
context/token preparation, final authorization, provider auth/connect/first
event/first text/completion, first successful server SSE write, synthetic-browser
first render, card stages, continuity, and total request. Record route lane as `server_hint`, `approved_exact`,
`active_flow`, `hybrid_rerank`, or `knowledge`. Never store user text, card
bodies, tokens, tool arguments, or arbitrary provider strings.

### Phase R1 — visible-latency and control-plane fixes

Likely files:

- `packages/backend/src/services/agents/wxoRunManager.ts` and tests
- `packages/backend/src/routes/chatMessage.ts` and tests
- `packages/backend/src/services/agents/agentRegistry.ts` and tests
- `packages/backend/src/utils/iamTokenManager.ts` and tests
- a focused registry/routing invalidation publisher-subscriber module
- application startup/shutdown wiring
- every admin, sync, promotion, and automatic-suspension mutation owner
- confirmed-outage service/cache and tests

Implement immediate delta passthrough, exact qualified card/action names and
allowlisting, one final fresh authorization read, registry/IAM/outage
single-flight, short outage caching, and cross-instance invalidation. Instrument
turn-lease cost but retain the current per-event remote fence. Any lease
optimization requires its own later security design and zero-post-loss proof.
Do not change semantic routing thresholds in this phase.

### Phase R2 — derived exact lane

Likely files:

- `packages/backend/src/services/chat/routeUtteranceService.ts`
- `packages/backend/src/services/chat/tieredRouter.ts`
- `packages/backend/src/services/agents/agentRegistry.ts`
- `packages/backend/src/routes/agentAdmin.ts`
- console route-example types only if current manual approval needs a visible
  distinction
- router, admin, console, and normalization tests

Implement one independently coalesced derived index over authoritative existing
rows, centralized invalidation, an atomically maintained durable corpus
revision, the fresh pre-dispatch revision check, write-time and runtime
collision protection, shadow telemetry, and final authorization. Prove that a
missed pub/sub message, manual-row deletion or reassignment, and a newly added
knowledge collision all fall through instead of exact-dispatching. Keep
behavior in `off` or `shadow`; do not enable live dispatch before Phase R4
passes. Do not add a second trigger field or tune hybrid thresholds to imitate
exact routing.

### Phase R3 — active transaction owner

Likely files:

- `packages/backend/src/utils/redis.ts`
- `packages/backend/src/routes/sessionReset.ts` and tests
- `packages/backend/src/routes/chatMessage.ts`
- `packages/backend/src/services/chat/tieredRouter.ts`
- session/SSE types and matching tests
- `packages/widget/src/types/sse-events.ts`
- `packages/widget/src/services/chatStream.ts` and `sessionReset.ts`
- `packages/widget/src/components/chat-window.ts`, widget orchestration, styles,
  localization catalog, and matching tests

Implement atomic actionable-card activation, provider-independent flow
ownership, sliding fixed expiry, terminal/reset precedence, transient-failure
semantics, response-language state, safe task-state restoration/SSE, the
localized leave control, fenced reset takeover, and fresh authorization every
turn. Decode old sessions with only `activeFlow: { agentKey }` safely but treat
that state as expired. Keep behavior in `off` or `shadow` until Phase R4 passes.

### Phase R4 — quality and scale proof

Replace or extend `packages/backend/src/scripts/loadTestRouter.ts`, the routing
evaluation script, and golden datasets. Include Work Offsite and EVL before
enabling their fast paths. Test 1, 10, 50, and 100 agents using 20–30 realistic
examples per agent, near-neighbor agents, same-vocabulary knowledge negatives,
ordinary follow-ups, card submissions, exit/reset, languages, and dependency
failures. Assert the destination, not just duration.

Before running a gate, commit a PII-free load-model artifact that records the
forecast peak concurrent turns and requests per second, WxO/Foundry/custom
provider share, read/card/write journey mix, cold-versus-warm mix, test duration,
and minimum completed turns and card deliveries. Run the same named model at
expected peak and twice peak so results are reproducible across teams. Treat
the finite golden corpus as deterministic correctness evidence; do not claim a
statistical 99% production confidence interval from only 20–30 examples per
agent. Report corpus pass rates and load/SLO confidence separately.

Inventory the provider composition of all 100 agents. IBM currently documents a
Premium ceiling of five Python toolkits per tenant and a standard deployment of
five workers across two replicas, or ten concurrent calls per toolkit. This
repository already uses three toolkits. A 100-agent registry is feasible; 100
WxO agents each owning a separate Python toolkit is not feasible under that
quota. Verify the actual tenant entitlement with IBM, define intentional shared
toolkit ownership, and measure shared `askhr_platform_tools` queue p95/p99 at
twice peak. Do not shard preemptively, but do not approve scale without a
deployable toolkit inventory and concurrency plan.

Tune Atlas `numCandidates` only through a recall@10-versus-p95 sweep on the real
corpus. Do not copy a generic value without evidence. After all Phase R4 gates
pass, add one agent at a time to the exact and active-flow rollout allowlists.

### Phase R5 — handbook, runbook, and delivery evidence

Update the current equivalents of:

- `docs/handbook/engineering/14-routing-end-to-end.md`
- `docs/handbook/integrations/01-agent-builder-guide.md`
- `docs/handbook/integrations/02-wxo-agents.md`
- `docs/handbook/integrations/05-cards-reference.md`
- environment references and QA/rollback runbooks
- the top of `BUILD-STATUS.md` when that file exists in the checkout; otherwise
  record equivalent branch/PR evidence as required by the current repository

Document the exact authority boundary, cache-versus-authorization split,
active-flow lifecycle, qualified control names, card canary, feature modes, and
fresh evidence. Adapt file names to the current work-PC handbook; do not create
parallel stale documentation.

## Routing and card test specification

Add failing tests before each behavior change.

Streaming:

- non-newline text is delivered before `done`;
- partial deltas reconstruct exactly, including ordered multi-content parts and
  split UTF-8 sequences;
- no newline is inserted and no tail is duplicated;
- whitespace-only output does not satisfy renderable-output validation; and
- byte, event, response, tool, and card budgets remain enforced;
- a forced turn takeover/reset produces zero post-loss deltas; retain per-event
  remote ownership assertions unless that equivalence test remains green.

Registry and authorization:

- concurrent cold/expired requests produce one refresh;
- mutation invalidation reaches every subscribed instance;
- dedicated subscriber startup and clean shutdown work on every app instance;
- manual add/delete, seed replacement, classifier promotion/deletion, knowledge
  reseed, sync, and automatic suspension all invalidate the right caches;
- missed invalidation is bounded by the short TTL;
- exactly one fresh `agent_registry` authorization query occurs at dispatch;
  exact-lane turns additionally perform the required durable corpus-revision
  read;
- `agentKey` is uniquely indexed, the query plan uses it, and pool-wait plus
  query p95/p99 meet the twice-peak load gate;
- revocation or revision change after discovery blocks dispatch;
- final Mongo failure fails closed without penalizing the provider breaker; and
- no 24-hour stale dispatch authority exists.

Exact routing:

- one unique eligible manual row routes without embedding, Atlas, or reranker;
- generated/traffic evidence does not fast-route;
- the cached snapshot carries the durable corpus revision and a fresh revision
  check occurs before every exact dispatch;
- missed pub/sub plus manual deletion, reassignment, or a new knowledge
  collision all fall through safely;
- country, region, role, environment, health, and status gates still apply;
- knowledge or agent collision rejects at write time or falls through at runtime;
- exact-only normalization reads original row text, handles Unicode, case,
  spaces, and punctuation exactly, and requires no persisted-row migration; and
- a final fresh authorization still runs.

Active flow:

- an actionable card without `thread_id` sets ownership;
- an actionable card with `thread_id` updates ownership and provider continuity;
- text-only answers, generic choice cards, and read-only tables do not establish
  ownership;
- failure, truncation, cancellation, lease loss, or stopped delivery sets no
  new ownership;
- ordinary follow-up calls no embedding/search/reranker;
- valid card submissions remain owner-bound and one-time;
- terminal success/exit/reset wins over a card observed earlier in the run;
- transient failure preserves but does not refresh a prior pending flow and
  never automatically retries an uncertain write;
- expiry slides only on a successfully delivered actionable interaction;
- legacy no-expiry flow rows do not direct-route;
- a PTO or other unrelated turn after a read-only view/reason-advice response
  returns to normal routing;
- a reason-choice submission still owner-routes once through its card token
  without establishing session-wide ownership;
- the localized widget `leave_task` action clears ownership in every enabled
  locale and makes no routing/provider call;
- refresh restores only safe task label/expiry presentation state;
- concurrent streaming plus `leave_task` invalidates the prior lease, aborts
  upstream, emits zero post-leave deltas/cards, and cannot resurrect ownership;
  and
- unsupported language falls back to English while allowed language and
  mid-conversation switching still work.

Cards:

- only exact `askhr_platform_tools:stage_card` and
  `askhr_platform_tools:report_action` names attached to the selected agent are
  recognized;
- bare, suffix-matched, or unregistered qualified names have no side effect;
- object and JSON-string arguments work;
- malformed, missing, over-limit, duplicate, and conflicting cards fail safely;
- failure/truncation emits no card;
- choice, form, confirm, and table cards reach the widget;
- replay, stale turn, cross-session, and cross-owner submissions are rejected;
- external names/arguments cannot enter diagnostics or persistence; and
- WxO card staging calls no private card endpoint.

Full path:

- run at expected peak and twice expected peak for at least 30 minutes;
- cover cold process, cache/IAM expiry, multi-instance invalidation, Mongo,
  Redis, embedding, reranker, WxO, toolkit queue, and client-disconnect cases;
- record p50/p95/p99, throughput, errors, cache hits, external-call counts,
  resource growth, and destination correctness; and
- compare identical warm and cold scenarios against the direct WxO API.

## Routing acceptance gates and rollout

Targets, not current claims:

- server hint, approved exact, and active-flow pre-provider overhead in pinned
  language, card submissions, and prompt-proven short continuations: p95 at or
  below 250 ms and p99 at or below 500 ms at twice forecast peak;
- substantive Auto-language turns report language-analysis time separately and
  must meet the direct-provider comparison after that required analysis; do not
  claim the 250 ms fast-lane gate for a model-based language call before it is
  measured;
- general first-turn routing: p95 at or below 1.5 seconds and p99 at or below
  3 seconds;
- added AskHR latency versus direct WxO: median at or below 250 ms and p95 at
  or below 500 ms on the fast lanes;
- exact lane: 100% correct eligible destination and zero unauthorized/collision
  dispatches;
- general routing: at least 99% precision and 95% recall per agent, with 100%
  pass on knowledge/action, geography, role, and nearest-neighbor sentinels;
- active flow: 100% correct owner, exit, completion, expiry, failure, and
  revocation behavior;
- cards: at least 99.9% valid stage-to-widget delivery excluding declared
  provider/business outages, with 100% malformed/stale/cross-owner/replay
  rejection; and
- provider inventory fits the verified tenant toolkit quota, and shared toolkit
  queue p95/p99 meets the latency gate at twice peak; and
- soak: below 0.1% infrastructure errors, no unbounded resource growth, no
  cache-refresh herd, and no correctness regression from one to 100 agents.

If the provider prevents an absolute latency target, the direct-WxO comparison
remains mandatory because it isolates the overhead AskHR owns.

Roll out in this order:

1. baseline identical direct-WxO and AskHR journeys;
2. observability and immediate streaming;
3. qualified card controls plus live canary;
4. coalescing, invalidation, outage cache, and removal of one duplicate read;
5. exact and active-flow lanes in shadow only;
6. complete the real-corpus correctness suite, provider-toolkit inventory, and
   100-agent/twice-peak load soak; and
7. only after Phase R4 passes, add one agent at a time to each centrally managed
   live allowlist.

Emergency rollback sets both modes to `off`; the existing hybrid/Tier-3 path
remains the universal fallback. The card path adds no new network dependency.

## Routing/card definition of complete

Do not mark this portion complete until fresh evidence shows:

- request-to-first-SSE-write telemetry plus the synthetic-browser first-render
  trace identify every material phase without conflating server enqueue and paint;
- WxO text streams immediately and reconstructs exactly;
- AskHR meets the direct-provider overhead gate;
- discovery refresh is coalesced and invalidated across instances;
- exactly one fresh dispatch authorization remains;
- exact and active-flow modes pass shadow review before live enablement;
- Work Offsite and EVL are in the real routing golden set;
- the full-path 100-agent suite meets correctness, latency, reliability, and
  resource gates;
- the registered catalog has a deployable IBM toolkit inventory and proven
  shared-toolkit concurrency capacity;
- every current ChatBlock kind renders through the qualified control tool;
- a live harmless card canary detects IBM event/tool drift;
- no WxO card turn calls the private staging endpoint; and
- current handbook, runbook, environment, conditional `BUILD-STATUS.md` or
  equivalent branch evidence, `npm run verify`, and approved dev/QA
  `npm run eval:quality` evidence are complete. An external
  dependency that cannot run is `BLOCKED`, never silently counted as success.

## Current external references

- IBM [Runs streaming API](https://developer.watson-orchestrate.ibm.com/apis/orchestrate-agent/chat-with-orchestrate-assistant-as-stream)
  for request/thread semantics and the generic run-event envelope.
- IBM [Toolkit overview](https://developer.watson-orchestrate.ibm.com/tools/toolkits/overview)
  and [Python toolkits](https://developer.watson-orchestrate.ibm.com/tools/toolkits/python_toolkits)
  for qualified names, packaging, deployment, and concurrency considerations.
- IBM [Python tool authoring](https://developer.watson-orchestrate.ibm.com/tools/create_tool)
  for async `@tool`, typed inputs, connections, and run context.
- IBM [Native-agent context variables](https://developer.watson-orchestrate.ibm.com/agents/build_agent)
  for the scope of context exposed through the Runs API.
- IBM [ReAct Core migration](https://developer.watson-orchestrate.ibm.com/agents/agent_styles_migration)
  for the canonical style and GPT-OSS behavior.
- IBM [Agent performance](https://developer.watson-orchestrate.ibm.com/tutorials/performance/performance-guide-v2-agent)
  and [tool performance](https://developer.watson-orchestrate.ibm.com/tutorials/performance/performance-guide-v2-tools)
  for ReAct/tool-loop latency and environment-specific load measurement.
- IBM [Evaluation](https://developer.watson-orchestrate.ibm.com/evaluate/evaluate)
  for tool, routing, response-time, and journey-success evaluation.
- IBM [Widget integration](https://developer.watson-orchestrate.ibm.com/tools/widget_integration)
  and [known issues](https://developer.watson-orchestrate.ibm.com/release/knownissues)
  for the provider-specific widget boundary and current limitations.
- MongoDB [`$vectorSearch`](https://www.mongodb.com/docs/atlas/atlas-vector-search/vector-search-stage/)
  for evidence-based `numCandidates` recall/latency tuning.

The research did not call a live AskHR, MongoDB, Redis, Apigee, or WxO tenant.
Production timings, real SaaS event-shape adherence, toolkit queue behavior, and
peak concurrency are therefore verification requirements, not assumed facts.

## Final acceptance checklist

- [ ] No `agentTokenGate` module or import remains.
- [ ] No `TOKEN_POLICY_BLOCKED` branch remains.
- [ ] Backend and console `DiscoveryGate` unions match and contain no `token`.
- [ ] Enabled published WxO token consumers pass discovery and publication
      readiness when all other controls pass.
- [ ] `buildAgentContext` still uses default cache-aware resolution and never
      emits `idp_token`.
- [ ] `resolve-token` still accepts only the server-bound session identifier.
- [ ] The explicit route passes `{ forceRefresh: true }` internally.
- [ ] API-key validation and the exact `internal-agents` client check precede
      the 10/min per-session resolver limiter.
- [ ] Forced mode performs a backend exchange for sessions with `idpToken`.
- [ ] Single-flight refresh behavior remains intact.
- [ ] Service sessions still fail closed after actual token expiry.
- [ ] EVL's shell emits the exact `wd_access_token` context key.
- [ ] EVL uses the payload token first and requests recovery only after missing
      auth or Workday 401/403.
- [ ] The WxO tools contain no IDP/SAML/OBO exchange implementation.
- [ ] Both shells and YAML agents declare `askhr_platform_tools:stage_card` and
      `askhr_platform_tools:report_action`.
- [ ] Work Offsite's shell declares `current_date`; the backend computes it from
      the validated session IANA zone, and missing/invalid context fails safely.
- [ ] UTC-midnight and DST tests prove relative dates, date-picker minimums, and
      past-date checks use the employee-local date rather than server UTC.
- [ ] Cards validate against the existing AskHR ChatBlock schema.
- [ ] Current handbook and Postman descriptions match runtime behavior.
- [ ] `WXO_TRANSACTIONAL_RESPONSE_LANGUAGES` contains only `fr`, `es`, `de`,
      `it`, `ja`, `ko`, `zh-CN`, `zh-TW`, and `pt-BR`; English remains the
      explicit fallback outside the configurable set.
- [ ] `AskHR:AgentResponseLanguages` selects only a subset and cannot enable a
      knowledge-only locale for WxO.
- [ ] `nl` can remain available to knowledge while a WxO turn sends
      `response_language: "en"` and answers in English.
- [ ] Each enabled non-English transactional locale passes a DEV/QA live prose,
      tool-text, failure, and mid-conversation switch smoke test.
- [ ] EVL opening “I need an income letter” calls `get_options` first;
      `select_letter` is used only after options were shown.
- [ ] Targeted tests and the complete `npm run verify` gate pass freshly.
- [ ] Remaining tenant smoke tests are recorded explicitly; no untested agent is
      silently enabled or published.

## When to mark the work complete

Mark the **AskHR source implementation** complete only when every final
acceptance item and the routing/card definition of complete above are satisfied,
the full repository verification gate is green, and the work-PC branch contains
the backend language ceiling, regression tests, performance evidence, handbook
updates, and environment-specific App Configuration and rollout plan.

Mark an **environment deployment** complete only when all applicable App
Configuration values are applied: the language trio
`AskHR:LanguageSupportEnabled`, `AskHR:MultilingualEnabled`, and
`AskHR:AgentResponseLanguages`; `AskHR:FastExactAgentRouteMode` plus
`AskHR:FastExactAgentKeys`; and `AskHR:ActiveFlowDirectMode` plus
`AskHR:ActiveFlowAgentKeys`. The toolkits and agents must import under their
exact names, all connection/card/token/write checks must pass, and every enabled
non-English transactional locale must pass the live smoke tests. Fast routing
also requires its shadow review, toolkit-capacity proof, and full-path load
gate. A successful import by itself is not completion. Keep registry shells and
new fast-path allowlists disabled until this evidence is recorded.
