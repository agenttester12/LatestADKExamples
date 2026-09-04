# AskHR WxO agents

Deployable watsonx Orchestrate definitions for EVL and Work Offsite. Both use
AskHR ChatBlock cards through the shared
`askhr_platform_tools:stage_card` control tool and report only confirmed
successful writes through `askhr_platform_tools:report_action`.

## Runtime contract

- Import all three Python directories as toolkits before importing the agents.
- Allowlist the exact package versions in each `requirements.txt` in the WxO
  tenant. Import into the current WxO Python 3.12 tools runtime.
- Associate `evl_tools` with the `EVLConnection` key-value connection and
  `work_offsite_toolkit` with `FlexWork`.
- `askhr_platform_tools` has no connection or secret. AskHR observes its tool
  calls in the WxO run stream; the returned acknowledgements contain no card
  body or employee data.
- Do not add AskHR config, token-resolution, HTTP, or connection settings to
  `askhr_platform_tools`. Work Offsite also does not use `config_url`,
  `config_api_key`, or `resolve_token_url`; those keys belong only to EVL's
  configuration and Workday-token recovery path.

`EVLConnection` keys:

- `report_base_url`, `report_path`, `dynamic_evl_url`
- `config_url`, `resolve_token_url`, optional `config_api_key`
- `evl_token_url`, `evl_username`, `evl_password`

`resolve_token_url` is the Apigee route for
`POST /api/internal/resolve-token`. The request body is only `{ "sessionId":
"<AskHR session UUID>" }`; the gateway must authenticate it as the
`internal-agents` client, either by injecting the credential or by using
`config_api_key`. AskHR treats this explicit recovery call as a request for a
fresh OBO exchange when the session has an IDP assertion.

`FlexWork` keys:

- `bearer_url`, `username`, `password`
- `view_flex_url`, `add_flex_url`, `cancel_flex_url`, `rescind_flex_url`
- `get_end_flex_url`, `workday_user`, `workday_pass`

## Authentication ownership

The agents do not perform OBO. EVL uses the subject-bound
`wd_access_token` that AskHR places in the run context. If that token is absent
or Workday rejects it with 401/403, EVL sends only the AskHR `session_id` to
`resolve_token_url`; AskHR performs any fresh OBO exchange and returns only the
replacement Workday token. The agent never receives an IDP token or SAML
assertion and has no OBO exchange code.

The `client_credentials` calls in these toolkits are separate service-to-service
authentication for the EVL document service and FlexWork APIs. They do not
impersonate the employee and are not the Workday OBO flow.

IBM defines YAML `context_variables` as context available to the agent runtime;
it is not a documented secret vault or guaranteed model-isolation boundary.
EVL must list `wd_access_token` and `session_id` so its Python tool can consume
the values from `AgentRun.request_context`, but neither value is interpolated
into instructions or accepted as a model-supplied tool argument. The prompt also
forbids disclosure. Treat this as deliberate bearer delivery to the trusted WxO
agent boundary, not as proof of tool-only visibility. If policy requires a
cryptographic model/tool separation, use resolver-only delivery or a future IBM
secret-injection feature that explicitly guarantees it; do not claim the current
context contract provides that guarantee.

Work Offsite also reads `current_date` from run context. AskHR computes this
ISO date from the validated, session-bound IANA time zone reported by the
employee's browser; the model never supplies it as a tool argument. This keeps
“today,” “tomorrow,” weekday resolution, date-picker minimums, and past-date
checks aligned for global employees. If the context value is absent or invalid,
date-dependent tools fail safely and ask the employee to refresh AskHR rather
than using the WxO container's calendar.

## Response-language contract

These are native agents, so IBM's agentic-workflow translation files do not
control their conversational responses. AskHR owns the runtime decision:

- `response_language` carries the effective transactional response language on
  every opted-in turn: an approved non-English locale within AskHR's fixed IBM
  WxO ceiling, or `en` for every other case.
- When it is present and well formed, both agents write all conversational
  prose in that locale, including a faithful translation of the human-readable
  `text` returned by their tools.
- If the field is unexpectedly absent, empty, or malformed, both agents still
  use English. They do not infer a different response language from the message
  or another context field.
- ChatBlock card labels and Workday values remain unchanged. EVL's separate
  `language` tool argument controls the requested document template, not the
  conversation language.

IBM's agentic-workflow translation bundles do not translate tool outputs and do
not apply to these native agents. The native-agent instructions therefore own
the tool-text translation rule. Cards and factual or opaque values stay
unchanged.

The non-English ceiling is `fr`, `es`, `de`, `it`, `ja`, `ko`, `zh-CN`,
`zh-TW`, and `pt-BR`; English is the explicit fallback. This is independent of
AskHR's broader knowledge-language taxonomy. Configure the desired subset in
`AskHR:AgentResponseLanguages`; an empty setting is the safe English-only
default.

## Agent reasoning contract

Both YAML definitions declare the current canonical `style: react_core` and hide
reasoning. IBM has deprecated `default` and `react` and documents that
GPT-OSS-120B already uses ReAct Core internally, so changing the old style label
does not change these agents' behavior. The explicit tool and safety
instructions remain the executable business contract. Do not change models
merely to alter prompting. First use `orchestrate models list`, deploy a draft
variant available in the target tenant, and compare latency, tool selection,
confirmations, and write safety with the evaluation cases below.
See IBM's [ReAct Core migration guide](https://developer.watson-orchestrate.ibm.com/agents/agent_styles_migration).

Work Offsite deliberately uses the minimum tool sequence for the employee's
goal. It recommends only a published reason that the employee's stated facts
support, asks one focused clarification when needed, and requires the employee
to choose or confirm the exact reason before validation or a write. The Python
validator accepts only canonical reason labels, numbers, and a small explicit
alias set; it never extracts a reason from narrative text.

## Import order

From this directory, replace `<tenant-tier>` with the dedicated Python-toolkit
tier enabled for the target tenant, then run:

```bash
orchestrate toolkits add --kind python --name evl_tools \
  --description "Employment verification letter tools" \
  --tier <tenant-tier> --package_root Tools/evl_tools --app-id EVLConnection

orchestrate toolkits add --kind python --name work_offsite_toolkit \
  --description "Work-offsite request tools" \
  --tier <tenant-tier> --package_root Tools/work_offsite_toolkit --app-id FlexWork

orchestrate toolkits add --kind python --name askhr_platform_tools \
  --description "AskHR card staging and action reporting controls" \
  --tier <tenant-tier> --package_root Tools/askhr_platform_tools

orchestrate agents import -f Agents/evl_agent.yaml
orchestrate agents import -f Agents/work_offsite_agent.yaml
```

IBM requires `--tier` for Python toolkits. Confirm the permitted tier with the
tenant administrator; do not guess or omit it.

Toolkit imports expose tools as `toolkit_name:tool_name`. Before importing the
agents, verify that `orchestrate tools list -v` includes these exact names:

```text
evl_tools:evl_tools
work_offsite_toolkit:view_offsite_requests
work_offsite_toolkit:list_offsite_requests_for_action
work_offsite_toolkit:validate_offsite_request
work_offsite_toolkit:submit_offsite_request
work_offsite_toolkit:cancel_offsite_request
work_offsite_toolkit:modify_offsite_request
work_offsite_toolkit:get_offsite_reasons
askhr_platform_tools:stage_card
askhr_platform_tools:report_action
```

The agent YAML files intentionally use those qualified names. Do not shorten
them to bare Python function names after a toolkit import.

After the WxO imports and connection tests pass, sync the agents into the
matching AskHR environment, apply the repository agent configuration, run the
card/write smoke tests, and publish. The repository shells intentionally remain
environment-disabled until that evidence exists.

## Local verification

```text
python3 -m unittest discover -s tests -v
python3 -m py_compile Tools/evl_tools/tools.py Tools/work_offsite_toolkit/tools.py Tools/askhr_platform_tools/tools.py
```

The Python suite constructs every one of the nine card builders and checks the
portable ChatBlock shape. Before deployment, also run the cross-repository card
check in `ASKHR-CODEBASE-CHANGE-GUIDE.md` against the target AskHR checkout's
actual `ChatBlockSchema`; that integration check is authoritative if the schema
has changed.

`evaluations/work_offsite_acceptance.csv` is the required source manifest for
the Work Offsite behavioral gate. After importing the draft agent, use IBM's
`orchestrate evaluations record` flow to capture each journey, review the
generated annotated JSON, and run it with `orchestrate evaluations evaluate`.
The recorded tool trajectory and final response must satisfy the corresponding
required/forbidden columns. Run each case more than once; do not publish from a
single lucky response. Store credentials only in the evaluation environment,
never in this repository.

The mandatory journeys include:

- “I want to work offsite” opens the blank request form without an unsolicited
  reason dump.
- “I want to work offsite while visiting my sister in Florida. Which reason?”
  explains that the published alternative-location description may be the
  closest match, asks the employee to confirm the exact reason, and does not
  invent a personal-travel policy or claim eligibility, remaining days, country
  safety, or approval.
- “I am caring for my ill sister. Which reason?” recommends Other Reason.
- “I am visiting a client. Which reason?” recommends Business Reason.
- A session near UTC midnight resolves “today” and “tomorrow” from its supplied
  `current_date`, while a missing or malformed value stops before validation or
  a write.
- “Business Reason or Other Reason” is not silently accepted as one choice.
- An end date without a start date remains a partial form rather than becoming
  a one-day request.
- Every confirmation writes the exact previously validated values, and a
  decline performs no write.

Also record the EVL opening utterance “I need an income letter.” Its first and
only business-tool call must be `evl_tools:evl_tools` with
`process="get_options"`; `select_letter` is valid only after the options have
already been shown in that flow.

IBM's evaluation framework compares the simulated journey step by step,
including tool-call order, and supports SaaS environments in current ADK
releases. See [Creating evaluation test cases](https://developer.watson-orchestrate.ibm.com/evaluate/create_data)
and [Evaluating agents and tools](https://developer.watson-orchestrate.ibm.com/evaluate/evaluate).
