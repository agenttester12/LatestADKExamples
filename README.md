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

## Response-language contract

These are native agents, so IBM's agentic-workflow translation files do not
control their conversational responses. AskHR owns the runtime decision:

- `response_language` is included only for a non-English locale currently
  approved in `AskHR:AgentResponseLanguages`.
- When it is present and well formed, both agents write all conversational
  prose in that locale, including a faithful translation of the human-readable
  `text` returned by their tools.
- When it is absent, empty, or malformed, both agents use English. They do not
  infer a different response language from the message or another context field.
- ChatBlock card labels and Workday values remain unchanged. EVL's separate
  `language` tool argument controls the requested document template, not the
  conversation language.

IBM's agentic-workflow translation bundles do not translate tool outputs and do
not apply to these native agents. The native-agent instructions therefore own
the tool-text translation rule. Cards and factual or opaque values stay
unchanged.

Keep `AskHR:AgentResponseLanguages` limited to locales proven acceptable with
the deployed model. An empty setting is the safe English-only default.

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
