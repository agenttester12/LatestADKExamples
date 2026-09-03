# AskHR codebase implementation guide

This is a one-time engineering handoff for applying the required AskHR
application changes in the real work-PC repository. It describes AskHR changes
only. The `Agents/` and `Tools/` directories beside this file are the separately
transferable watsonx Orchestrate package and must not be copied into the AskHR
application repository unless that repository already has an explicit location
for deployment artifacts.

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
8. EVL and Work Offsite declare the `stage_card` and `report_action` tools needed
   by the existing AskHR WxO run manager.
9. AskHR bounds forced recovery to 10 requests per minute per session after
   authenticating the internal-agent caller.

## Non-negotiable security boundary

OBO remains entirely backend-owned.

- The widget never receives or stores a Workday token.
- WxO never receives `idpToken`, a SAML assertion, the SAML application secret,
  or any input needed to perform OBO.
- The agent receives only the resulting Workday bearer when its registry profile
  explicitly selects `token: "in-context"`.
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
  "toolNames": ["evl_tools", "stage_card", "report_action"],
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

Edit `config/agents/work_offsite.json` and append:

```json
"stage_card",
"report_action"
```

Do not configure Work Offsite for the employee OBO token. Its transferred tools
use the existing FlexWork service credential contract, not EVL's
`wd_access_token` flow.

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
- Both agents use in-stream `stage_card` and `report_action` tool calls.

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

No new card framework is required in AskHR. The existing WxO run manager already
intercepts tool calls:

```json
{
  "name": "stage_card",
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

The transferred agents emit `report_action` only after a tool result explicitly
confirms success. The HTTP `report-action` callback remains a no-op
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
10. Confirm `report_action` is emitted only after an explicit successful write.
11. Sync the WxO agent identifiers into the matching AskHR environment, apply
    the registry configuration, run the AskHR agent connection test, and only
    then publish/enable that environment.

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
- [ ] Both shells and YAML agents declare `stage_card` and `report_action`.
- [ ] Cards validate against the existing AskHR ChatBlock schema.
- [ ] Current handbook and Postman descriptions match runtime behavior.
- [ ] Targeted tests and the complete `npm run verify` gate pass freshly.
- [ ] Remaining tenant smoke tests are recorded explicitly; no untested agent is
      silently enabled or published.
