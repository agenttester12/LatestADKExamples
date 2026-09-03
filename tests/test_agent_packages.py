import asyncio
import importlib.util
import re
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _install_ibm_stubs() -> None:
    modules = {
        name: types.ModuleType(name)
        for name in (
            "ibm_watsonx_orchestrate",
            "ibm_watsonx_orchestrate.agent_builder",
            "ibm_watsonx_orchestrate.agent_builder.tools",
            "ibm_watsonx_orchestrate.agent_builder.connections",
            "ibm_watsonx_orchestrate.run",
            "ibm_watsonx_orchestrate.run.context",
        )
    }

    def tool(function=None, **_kwargs):
        return function if function is not None else lambda wrapped: wrapped

    class ConnectionType:
        KEY_VALUE = "key_value"

    class AgentRun:
        def __init__(self, request_context=None):
            self.request_context = request_context or {}

    modules["ibm_watsonx_orchestrate.agent_builder.tools"].tool = tool
    modules["ibm_watsonx_orchestrate.agent_builder.connections"].ConnectionType = ConnectionType
    modules["ibm_watsonx_orchestrate.run"].connections = types.SimpleNamespace(key_value=lambda _app_id: {})
    modules["ibm_watsonx_orchestrate.run.context"].AgentRun = AgentRun
    sys.modules.update(modules)


def _load_module(name: str, relative_path: str):
    _install_ibm_stubs()
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, status_code: int, body=None):
        self.status_code = status_code
        self._body = body
        self.text = ""

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class AgentPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evl = _load_module("evl_tools_under_test", "Tools/evl_tools/tools.py")
        cls.offsite = _load_module("offsite_tools_under_test", "Tools/work_offsite_toolkit/tools.py")

    def test_agent_specs_attach_platform_tools_and_use_supported_styles(self):
        expected_tools = {
            "evl_agent.yaml": [
                "evl_tools:evl_tools",
                "askhr_platform_tools:stage_card",
                "askhr_platform_tools:report_action",
            ],
            "work_offsite_agent.yaml": [
                "work_offsite_toolkit:view_offsite_requests",
                "work_offsite_toolkit:list_offsite_requests_for_action",
                "work_offsite_toolkit:validate_offsite_request",
                "work_offsite_toolkit:submit_offsite_request",
                "work_offsite_toolkit:cancel_offsite_request",
                "work_offsite_toolkit:modify_offsite_request",
                "work_offsite_toolkit:get_offsite_reasons",
                "askhr_platform_tools:stage_card",
                "askhr_platform_tools:report_action",
            ],
        }
        for filename, tool_names in expected_tools.items():
            spec = yaml.safe_load((ROOT / "Agents" / filename).read_text())
            self.assertIn(spec["style"], {"default", "react", "react_core"})
            self.assertTrue(spec["hide_reasoning"])
            self.assertEqual(spec["tools"], tool_names)

        evl_spec = yaml.safe_load((ROOT / "Agents/evl_agent.yaml").read_text())
        self.assertIn("session_id", evl_spec["context_variables"])
        self.assertTrue(
            all(guideline["tool"] == "evl_tools:evl_tools" for guideline in evl_spec["guidelines"])
        )

    def test_agent_language_contract_uses_only_vetted_context(self):
        for filename in ("evl_agent.yaml", "work_offsite_agent.yaml"):
            spec = yaml.safe_load((ROOT / "Agents" / filename).read_text())
            instructions = spec["instructions"]
            self.assertIn("operator-vetted non-English locale", instructions)
            self.assertIn(
                "If `response_language` is absent, empty, or malformed, write in English.",
                instructions,
            )
            self.assertIn("Do not infer the response language", instructions)
            self.assertIn("Tool `text` is part of the employee-facing reply.", instructions)
            self.assertIn("translate its natural-language prose faithfully", instructions)
            self.assertNotRegex(instructions, r"relay(?: the)?(?: returned| tool's)? text (?:exactly|as-is)")
            self.assertNotIn("Only your own prose is translated", instructions)
            self.assertNotIn("only if you can write", instructions.lower())
            self.assertNotIn('If `response_language` is "es"', instructions)

        offsite_spec = yaml.safe_load((ROOT / "Agents/work_offsite_agent.yaml").read_text())
        self.assertNotIn("preferred_language", offsite_spec["context_variables"])

    def test_requirements_use_exact_stable_versions(self):
        for path in (
            ROOT / "Tools/evl_tools/requirements.txt",
            ROOT / "Tools/work_offsite_toolkit/requirements.txt",
            ROOT / "Tools/askhr_platform_tools/requirements.txt",
        ):
            for line in path.read_text().splitlines():
                if line.strip():
                    self.assertRegex(line, r"^[A-Za-z0-9_.-]+==[0-9]+(?:\.[0-9]+)+(?:[A-Za-z0-9.-]+)?$")

    def test_platform_tools_are_packaged_once(self):
        platform_tools = _load_module("platform_tools_under_test", "Tools/askhr_platform_tools/tools.py")
        self.assertTrue(callable(platform_tools.stage_card))
        self.assertTrue(callable(platform_tools.report_action))
        card = {"kind": "choice", "cardId": "test-card", "title": "Choose", "options": []}
        self.assertEqual(asyncio.run(platform_tools.stage_card(card)), {"ok": True, "cardId": "test-card"})
        self.assertEqual(asyncio.run(platform_tools.report_action("test_action")), {"ok": True})

    def test_every_generated_card_has_the_askhr_contract_shape(self):
        entry = {
            "startDate": "2026-09-10", "endDate": "2026-09-11", "status": "In Progress",
            "subtype": "Business Reason", "subtypeID": "type-1", "workdayID": "wid-1",
            "flexComplete": "0",
        }
        cards = [
            self.evl.build_selection_card([{
                "label": "Employment Verification", "description": "Employment details",
            }]),
            self.evl.build_recipient_card(["en", "nl"], True),
            self.evl.build_confirm_card("Employment Verification", "en", "landlord@example.com"),
            self.offsite.build_submit_form("2026-09-02"),
            self.offsite.build_submit_confirm(
                "2026-09-10", "2026-09-11", "Business Reason",
            ),
            self.offsite.build_view_table([entry]),
            self.offsite.build_select_table([entry], "cancel"),
            self.offsite.build_cancel_confirm(entry),
            self.offsite.build_reasons_choice(),
        ]

        self.assertEqual(len(cards), 9)
        for card in cards:
            self.assertIn(card["kind"], {"form", "confirm", "table", "choice"})
            self.assertRegex(card["cardId"], r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
            if card["kind"] == "form":
                self.assertIsInstance(card["fields"], list)
            elif card["kind"] == "confirm":
                self.assertIsInstance(card["title"], str)
                self.assertTrue(all(set(fact) == {"label", "value"} for fact in card["facts"]))
            elif card["kind"] == "table":
                self.assertTrue(all(isinstance(value, str) for row in card["rows"] for value in row.values()))
            else:
                self.assertTrue(all({"value", "label"} <= set(option) for option in card["options"]))

    def test_evl_retries_workday_once_with_resolved_token_after_auth_rejection(self):
        context = types.SimpleNamespace(request_context={
            "employee_id": "1001",
            "session_id": "11111111-1111-4111-8111-111111111111",
            "wd_access_token": "stale-token",
        })
        self.evl._creds_cache = {
            "report_base_url": "https://workday.example",
            "employee_details_url": "https://workday.example/report",
            "resolve_token_url": "https://askhr.example/api/internal/resolve-token",
            "config_api_key": "internal-key",
        }
        responses = [
            FakeResponse(401, {}),
            FakeResponse(200, {"Report_Entry": [{"employeeID": "1001"}]}),
        ]

        async def fake_get(_url, _params, headers):
            expected = "Bearer stale-token" if len(responses) == 2 else "Bearer fresh-token"
            self.assertEqual(headers["Authorization"], expected)
            return responses.pop(0)

        async def fake_post(_url, _params, body, headers, timeout=None):
            self.assertEqual(body, {"sessionId": "11111111-1111-4111-8111-111111111111"})
            self.assertEqual(headers["x-api-key"], "internal-key")
            return FakeResponse(200, {"workdayToken": "fresh-token"})

        with patch.object(self.evl, "_get", side_effect=fake_get), patch.object(self.evl, "_post", side_effect=fake_post):
            details, error = asyncio.run(self.evl._fetch_employee_details(context, "1001"))

        self.assertIsNone(error)
        self.assertEqual(details["employeeID"], "1001")
        self.assertEqual(responses, [])

    def test_evl_does_not_resolve_token_for_non_auth_workday_failure(self):
        context = types.SimpleNamespace(request_context={
            "employee_id": "1001",
            "session_id": "11111111-1111-4111-8111-111111111111",
            "wd_access_token": "current-token",
        })
        self.evl._creds_cache = {
            "report_base_url": "https://workday.example",
            "employee_details_url": "https://workday.example/report",
            "resolve_token_url": "https://askhr.example/api/internal/resolve-token",
            "config_api_key": "internal-key",
        }
        with patch.object(self.evl, "_get", new=AsyncMock(return_value=FakeResponse(500, {}))), \
             patch.object(self.evl, "_resolve_workday_token", new=AsyncMock()) as resolve:
            _details, error = asyncio.run(self.evl._fetch_employee_details(context, "1001"))

        self.assertEqual(error, self.evl._WORKDAY_UNAVAILABLE)
        resolve.assert_not_awaited()

    def test_evl_resolves_when_context_token_is_missing(self):
        context = types.SimpleNamespace(request_context={
            "employee_id": "1001",
            "session_id": "11111111-1111-4111-8111-111111111111",
        })
        self.evl._creds_cache = {
            "report_base_url": "https://workday.example",
            "employee_details_url": "https://workday.example/report",
            "resolve_token_url": "https://askhr.example/api/internal/resolve-token",
            "config_api_key": "internal-key",
        }
        with patch.object(
            self.evl, "_resolve_workday_token", new=AsyncMock(return_value=("Bearer resolved-token", None))
        ) as resolve, patch.object(
            self.evl,
            "_get",
            new=AsyncMock(return_value=FakeResponse(200, {"Report_Entry": [{"employeeID": "1001"}]})),
        ) as get:
            details, error = asyncio.run(self.evl._fetch_employee_details(context, "1001"))

        self.assertIsNone(error)
        self.assertEqual(details["employeeID"], "1001")
        resolve.assert_awaited_once_with(context)
        self.assertEqual(get.await_args.args[2]["Authorization"], "Bearer resolved-token")

    def test_evl_stops_after_one_failed_resolver_retry(self):
        context = types.SimpleNamespace(request_context={
            "employee_id": "1001",
            "session_id": "11111111-1111-4111-8111-111111111111",
            "wd_access_token": "stale-token",
        })
        self.evl._creds_cache = {
            "report_base_url": "https://workday.example",
            "employee_details_url": "https://workday.example/report",
            "resolve_token_url": "https://askhr.example/api/internal/resolve-token",
            "config_api_key": "internal-key",
        }
        get = AsyncMock(side_effect=[FakeResponse(401, {}), FakeResponse(401, {})])
        resolve = AsyncMock(return_value=("Bearer refreshed-token", None))
        with patch.object(self.evl, "_get", new=get), patch.object(
            self.evl, "_resolve_workday_token", new=resolve
        ):
            details, error = asyncio.run(self.evl._fetch_employee_details(context, "1001"))

        self.assertIsNone(details)
        self.assertEqual(error, self.evl._WORKDAY_AUTH_MISSING)
        resolve.assert_awaited_once_with(context)
        self.assertEqual(get.await_count, 2)

    def test_evl_parallel_auth_rejections_share_one_recovery(self):
        context = types.SimpleNamespace(request_context={
            "employee_id": "1001",
            "session_id": "11111111-1111-4111-8111-111111111111",
            "wd_access_token": "stale-token",
        })
        self.evl._creds_cache = {
            "report_base_url": "https://workday.example",
            "employee_details_url": "https://workday.example/report",
            "resolve_token_url": "https://askhr.example/api/internal/resolve-token",
            "config_api_key": "internal-key",
        }

        async def fake_get(_url, _params, headers):
            if headers["Authorization"] == "Bearer stale-token":
                await asyncio.sleep(0)
                return FakeResponse(401, {})
            return FakeResponse(200, {"Report_Entry": [{"employeeID": "1001"}]})

        resolve = AsyncMock(return_value=("Bearer fresh-token", None))
        auth_state = {}

        async def run_parallel():
            return await asyncio.gather(
                self.evl._fetch_employee_details(
                    context, "1001", report_path="/region", auth_state=auth_state,
                ),
                self.evl._fetch_employee_details(
                    context, "1001", report_path="/country", auth_state=auth_state,
                ),
            )

        with patch.object(self.evl, "_get", side_effect=fake_get), patch.object(
            self.evl, "_resolve_workday_token", new=resolve
        ):
            results = asyncio.run(run_parallel())

        self.assertTrue(all(error is None for _, error in results))
        self.assertEqual([details["employeeID"] for details, _ in results], ["1001", "1001"])
        resolve.assert_awaited_once_with(context)

    def test_offsite_validation_returns_exact_write_arguments(self):
        submit = asyncio.run(self.offsite.validate_offsite_request(
            "2026-09-10", "2026-09-10", "Business Reason",
        ))
        modify = asyncio.run(self.offsite.validate_offsite_request(
            "2026-09-12", "2026-09-12", "Remote Flexibility Benefit", for_modify=True,
        ))

        self.assertEqual(submit["write_args"], {
            "start_date": "2026-09-10",
            "end_date": "2026-09-10",
            "reason": "Business Reason",
        })
        self.assertEqual(modify["write_args"], {
            "new_start_date": "2026-09-12",
            "new_end_date": "2026-09-12",
            "new_reason": "Remote Flexibility Benefit",
        })
        self.assertNotIn("normalized", submit)

    def test_offsite_does_not_surface_upstream_error_content(self):
        response = FakeResponse(400, {
            "workdayResponse": "Internal worker WID secret-wid and tenant details",
        })
        response.text = "sensitive upstream response"
        with patch.object(self.offsite.httpx, "AsyncClient") as client_type:
            client_type.return_value.__aenter__ = AsyncMock(return_value=client_type.return_value)
            client_type.return_value.__aexit__ = AsyncMock(return_value=None)
            client_type.return_value.post = AsyncMock(return_value=response)
            result, error = asyncio.run(self.offsite._api_submit(
                "token", "https://api.example/add", "1001",
                "2026-09-10", "2026-09-10", "type-1",
            ))

        self.assertEqual(result, {})
        self.assertEqual(error, self.offsite._WORKDAY_UNAVAILABLE)
        self.assertNotIn("secret-wid", error)

    def test_evl_parallel_report_overlay_keeps_specificity_and_pinned_identity(self):
        config = {
            "sets": {
                "region": {
                    "scope": {"regions": ["NA"]},
                    "templates": [{"id": "region", "label": "Region"}],
                    "report": {"path": "/region"},
                },
                "country": {
                    "scope": {"countries": ["USA"]},
                    "templates": [{"id": "country", "label": "Country"}],
                    "report": {"path": "/country"},
                },
            }
        }
        base = {"employeeID": "1001", "email": "employee@example.com", "company": "Base"}

        async def fetch(_context, _employee_id, report_path=None, employee_param=None, auth_state=None):
            values = {
                "/region": {"employeeID": "wrong", "email": "wrong@example.com", "company": "Region"},
                "/country": {"employeeID": "wrong", "email": "wrong@example.com", "company": "Country"},
            }
            return values[report_path], None

        with patch.object(self.evl, "_fetch_employee_details", side_effect=fetch):
            merged, error = asyncio.run(self.evl._overlay_set_reports(
                object(), "1001", base, config, "USA", "NA", {},
            ))

        self.assertIsNone(error)
        self.assertEqual(merged["company"], "Country")
        self.assertEqual(merged["employeeID"], "1001")
        self.assertEqual(merged["email"], "employee@example.com")

    def test_offsite_request_reference_survives_reordering(self):
        first = {
            "startDate": "2026-09-10", "endDate": "2026-09-10", "status": "In Progress",
            "subtype": "Business Reason", "subtypeID": "type-1", "workdayID": "wid-1", "flexComplete": "0",
        }
        second = {**first, "startDate": "2026-09-11", "endDate": "2026-09-11", "workdayID": "wid-2"}
        card = self.offsite.build_select_table([first, second], "cancel")
        request_ref = card["rows"][0]["requestRef"]
        self.assertRegex(request_ref, r"^[a-f0-9]{64}$")
        self.assertNotIn("requestRef", {column["key"] for column in card["columns"]})

        with patch.object(self.offsite, "_fetch_requests", new=AsyncMock(return_value=([second, first], None))):
            selected, error = asyncio.run(
                self.offsite._select_request({}, "1001", request_ref, "cancel")
            )

        self.assertIsNone(error)
        self.assertEqual(selected["workdayID"], "wid-1")

    def test_offsite_success_has_machine_readable_completion_marker(self):
        context = types.SimpleNamespace(request_context={"employee_id": "1001"})
        normalized = {
            "start": "2026-09-10", "end": "2026-09-10",
            "reason_label": "Business Reason", "subtype_id": "type-1",
        }
        with patch.object(self.offsite, "_normalize_write_request", return_value=(normalized, None)), \
             patch.object(self.offsite, "_get_creds", return_value={"add_flex_url": "https://api.example/add"}), \
             patch.object(self.offsite, "_acquire_bearer", new=AsyncMock(return_value=("token", None))), \
             patch.object(self.offsite, "_api_submit", new=AsyncMock(return_value=({"WID": "wid-1"}, None))):
            result = asyncio.run(self.offsite.submit_offsite_request(
                context, "2026-09-10", "2026-09-10", "Business Reason", confirmed=True,
            ))

        self.assertTrue(result["ok"])
        self.assertEqual(result["action_type"], "work_offsite_submit")

    def test_offsite_cancel_and_modify_success_markers_are_explicit(self):
        context = types.SimpleNamespace(request_context={"employee_id": "1001"})
        entry = {
            "startDate": "2026-09-10", "endDate": "2026-09-10", "status": "In Progress",
            "subtype": "Business Reason", "subtypeID": "type-1", "workdayID": "wid-1", "flexComplete": "0",
        }
        normalized = {
            "start": "2026-09-12", "end": "2026-09-12",
            "reason_label": "Business Reason", "subtype_id": "type-1",
        }
        creds = {"add_flex_url": "https://api.example/add"}
        with patch.object(self.offsite, "_get_creds", return_value=creds), \
             patch.object(self.offsite, "_select_request", new=AsyncMock(return_value=(entry, None))), \
             patch.object(self.offsite, "_acquire_bearer", new=AsyncMock(return_value=("token", None))), \
             patch.object(self.offsite, "_do_cancel_or_rescind", new=AsyncMock(return_value=(True, None))):
            cancel = asyncio.run(self.offsite.cancel_offsite_request(context, "a" * 64, confirmed=True))

        self.assertEqual(cancel["action_type"], "work_offsite_cancel")
        self.assertTrue(cancel["ok"])

        with patch.object(self.offsite, "_normalize_write_request", return_value=(normalized, None)), \
             patch.object(self.offsite, "_get_creds", return_value=creds), \
             patch.object(self.offsite, "_select_request", new=AsyncMock(return_value=(entry, None))), \
             patch.object(self.offsite, "_acquire_bearer", new=AsyncMock(return_value=("token", None))), \
             patch.object(self.offsite, "_do_cancel_or_rescind", new=AsyncMock(return_value=(True, None))), \
             patch.object(self.offsite, "_api_submit", new=AsyncMock(return_value=({"WID": "wid-2"}, None))):
            modify = asyncio.run(self.offsite.modify_offsite_request(
                context, "a" * 64, "2026-09-12", "2026-09-12", "Business Reason", confirmed=True,
            ))

        self.assertEqual(modify["action_type"], "work_offsite_modify")
        self.assertTrue(modify["ok"])

    def test_offsite_write_tools_refuse_unconfirmed_calls(self):
        context = types.SimpleNamespace(request_context={"employee_id": "1001"})
        submit = asyncio.run(self.offsite.submit_offsite_request(
            context, "2026-09-10", "2026-09-10", "Business Reason",
        ))
        modify = asyncio.run(self.offsite.modify_offsite_request(
            context, "a" * 64, "2026-09-12", "2026-09-12", "Business Reason",
        ))

        self.assertFalse(submit["ok"])
        self.assertFalse(modify["ok"])
        self.assertIn("confirm", submit["text"].lower())
        self.assertIn("confirm", modify["text"].lower())


if __name__ == "__main__":
    unittest.main()
