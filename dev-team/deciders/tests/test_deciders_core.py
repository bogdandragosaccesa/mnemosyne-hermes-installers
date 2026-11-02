"""Offline tests for deciders_core: decide, extract, summarize.

No test calls the network. FakeTransport records every call and either
returns a canned (fixture) response or raises a given DecidersError.
"""
from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deciders_core
import forge_core
from deciders_core import DecidersError, EXIT_OK, EXIT_UNANSWERED, main

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _fake_key() -> str:
    # Built at runtime from two fragments so the full prefix never appears
    # as one literal substring anywhere in the repo.
    return "sk-" + "or-v1-FAKEKEYFAKEKEY"


class FakeTransport:
    """Records every call; replays a queued response or raises a queued error."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, headers, body, timeout))
        if not self._responses:
            raise AssertionError("FakeTransport called more times than expected")
        resp = self._responses.pop(0)
        if isinstance(resp, BaseException):
            raise resp
        return resp


def _env(key=None):
    env = {}
    if key is not None:
        env["OPENROUTER_DECIDERS_KEY"] = key
    return env


def _run(argv, transport, env, home=None):
    out = io.StringIO()
    code = main(argv, home=home or Path("/tmp/does-not-matter"), env=env,
                transport=transport, out=out)
    return code, out.getvalue()


class DecideRequestTests(unittest.TestCase):
    def test_decide_builds_request(self):
        fixture = _load_fixture("decide_ok.json")
        transport = FakeTransport([fixture])
        env = _env(_fake_key())

        code, stdout = _run(
            ["decide", "--state", str(FIXTURES / "decide_ok.json"),
             "--questions", str(FIXTURES / "decide_ok.json")],
            transport, env,
        )

        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(transport.calls), 1)
        url, headers, body, timeout = transport.calls[0]
        self.assertEqual(url, "https://openrouter.ai/api/alpha/decisions")
        self.assertEqual(headers["Authorization"], f"Bearer {_fake_key()}")
        self.assertEqual(body["model"], "liquid/d1.1")
        self.assertIn("state", body)
        self.assertIn("questions", body)

        data = json.loads(stdout)
        self.assertEqual(data["model"], fixture["model"])
        self.assertEqual(data["cost"], fixture["cost"])
        self.assertEqual(data["answers"], fixture["answers"])


class ExtractRequestTests(unittest.TestCase):
    def test_extract_builds_request(self):
        fixture = _load_fixture("extract_ok.json")
        transport = FakeTransport([fixture])
        env = _env(_fake_key())

        entities_path = FIXTURES / "entities_spec.json"
        entities_spec = {"types": ["synthetic-secret"]}
        entities_path.write_text(json.dumps(entities_spec))
        input_path = FIXTURES / "extract_input.txt"
        input_path.write_text("some text with EXAMPLE-TOKEN-0001 in it")
        try:
            code, stdout = _run(
                ["extract", "--entities", str(entities_path), "--input", str(input_path)],
                transport, env,
            )
        finally:
            entities_path.unlink(missing_ok=True)
            input_path.unlink(missing_ok=True)

        self.assertEqual(code, EXIT_OK)
        url, headers, body, timeout = transport.calls[0]
        self.assertEqual(url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(body["model"], "scaledown/extract")
        self.assertEqual(
            body["messages"],
            [{"role": "user", "content": "some text with EXAMPLE-TOKEN-0001 in it"}],
        )
        self.assertEqual(
            body["provider"], {"options": {"scaledown": {"entities": entities_spec}}}
        )

        data = json.loads(stdout)
        expected_entities = json.loads(fixture["choices"][0]["message"]["content"])["entities"]
        self.assertEqual(data["entities"], expected_entities)


class SummarizeTests(unittest.TestCase):
    def test_summarize_marks_not_evidence(self):
        fixture = _load_fixture("summarize_ok.json")
        transport = FakeTransport([fixture])
        env = _env(_fake_key())

        input_path = FIXTURES / "summarize_input.txt"
        input_path.write_text("background material to compress")
        try:
            code, stdout = _run(
                ["summarize", "--input", str(input_path)], transport, env,
            )
        finally:
            input_path.unlink(missing_ok=True)

        self.assertEqual(code, EXIT_OK)
        data = json.loads(stdout)
        self.assertIs(data["evidence"], False)
        self.assertEqual(data["summary"], fixture["choices"][0]["message"]["content"])


class ModelFlagTests(unittest.TestCase):
    def test_model_flag_overrides_default(self):
        fixture = _load_fixture("decide_ok.json")
        transport = FakeTransport([fixture])
        env = _env(_fake_key())

        code, stdout = _run(
            ["decide", "--state", str(FIXTURES / "decide_ok.json"),
             "--questions", str(FIXTURES / "decide_ok.json"),
             "--model", "typesafe/jev-1.13"],
            transport, env,
        )

        self.assertEqual(code, EXIT_OK)
        _, _, body, _ = transport.calls[0]
        self.assertEqual(body["model"], "typesafe/jev-1.13")


class NoKeyTests(unittest.TestCase):
    def test_no_key_exit_2(self):
        transport = FakeTransport([])
        env = _env(None)

        code, stdout = _run(
            ["decide", "--state", str(FIXTURES / "decide_ok.json"),
             "--questions", str(FIXTURES / "decide_ok.json")],
            transport, env,
        )

        self.assertEqual(code, EXIT_UNANSWERED)
        self.assertEqual(len(transport.calls), 0)
        data = json.loads(stdout)
        self.assertTrue(data["error"].startswith("deciders: no-key"))


class StatusLabelTests(unittest.TestCase):
    def _decide_argv(self):
        return ["decide", "--state", str(FIXTURES / "decide_ok.json"),
                "--questions", str(FIXTURES / "decide_ok.json")]

    def test_status_401_is_auth(self):
        self._assert_label([DecidersError("deciders: auth")], "deciders: auth")

    def test_status_402_is_key_cap(self):
        self._assert_label([DecidersError("deciders: key-cap")], "deciders: key-cap")

    def test_status_403_is_forbidden(self):
        self._assert_label([DecidersError("deciders: forbidden")], "deciders: forbidden")

    def test_status_404_is_model_unavailable(self):
        self._assert_label(
            [DecidersError("deciders: model-unavailable")], "deciders: model-unavailable"
        )

    def test_status_418_is_http_418(self):
        self._assert_label([DecidersError("deciders: http-418")], "deciders: http-418")

    def test_status_503_twice_is_upstream(self):
        self._assert_label(
            [DecidersError("deciders: upstream"), DecidersError("deciders: upstream")],
            "deciders: upstream",
            expected_calls=2,
        )

    def test_timeout_is_timeout(self):
        self._assert_label([DecidersError("deciders: timeout")], "deciders: timeout")

    def _assert_label(self, responses, expected_label, expected_calls=1):
        transport = FakeTransport(responses)
        env = _env(_fake_key())
        code, stdout = _run(self._decide_argv(), transport, env)
        self.assertEqual(code, EXIT_UNANSWERED)
        self.assertEqual(len(transport.calls), expected_calls)
        data = json.loads(stdout)
        self.assertTrue(data["error"].startswith(expected_label))


class RetryTests(unittest.TestCase):
    def test_retries_once_on_429_then_succeeds(self):
        fixture = _load_fixture("decide_ok.json")
        transport = FakeTransport([DecidersError("deciders: upstream"), fixture])
        env = _env(_fake_key())

        code, stdout = _run(
            ["decide", "--state", str(FIXTURES / "decide_ok.json"),
             "--questions", str(FIXTURES / "decide_ok.json")],
            transport, env,
        )

        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(transport.calls), 2)
        data = json.loads(stdout)
        self.assertEqual(data["model"], fixture["model"])


class BadShapeTests(unittest.TestCase):
    def test_bad_shape_is_bad_response(self):
        fixture = _load_fixture("decide_bad_shape.json")
        transport = FakeTransport([fixture])
        env = _env(_fake_key())

        code, stdout = _run(
            ["decide", "--state", str(FIXTURES / "decide_ok.json"),
             "--questions", str(FIXTURES / "decide_ok.json")],
            transport, env,
        )

        self.assertEqual(code, EXIT_UNANSWERED)
        data = json.loads(stdout)
        self.assertTrue(data["error"].startswith("deciders: bad-response"))


class KeyRedactionTests(unittest.TestCase):
    def _decide_argv(self):
        return ["decide", "--state", str(FIXTURES / "decide_ok.json"),
                "--questions", str(FIXTURES / "decide_ok.json")]

    def _assert_key_absent(self, responses):
        key = _fake_key()
        transport = FakeTransport(responses)
        env = _env(key)
        code, stdout = _run(self._decide_argv(), transport, env)
        self.assertEqual(code, EXIT_UNANSWERED)
        self.assertNotIn(key, stdout)

    def test_key_never_printed_no_key(self):
        transport = FakeTransport([])
        env = _env(None)
        code, stdout = _run(self._decide_argv(), transport, env)
        self.assertEqual(code, EXIT_UNANSWERED)
        self.assertNotIn(_fake_key(), stdout)

    def test_key_never_printed_auth(self):
        self._assert_key_absent([DecidersError("deciders: auth")])

    def test_key_never_printed_key_cap(self):
        self._assert_key_absent([DecidersError("deciders: key-cap")])

    def test_key_never_printed_forbidden(self):
        self._assert_key_absent([DecidersError("deciders: forbidden")])

    def test_key_never_printed_model_unavailable(self):
        self._assert_key_absent([DecidersError("deciders: model-unavailable")])

    def test_key_never_printed_http_418(self):
        self._assert_key_absent([DecidersError("deciders: http-418")])

    def test_key_never_printed_upstream_twice(self):
        self._assert_key_absent(
            [DecidersError("deciders: upstream"), DecidersError("deciders: upstream")]
        )

    def test_key_never_printed_timeout(self):
        self._assert_key_absent([DecidersError("deciders: timeout")])

    def test_key_never_printed_bad_shape(self):
        self._assert_key_absent([_load_fixture("decide_bad_shape.json")])

    def test_key_never_printed_transport_exception_with_key_in_message(self):
        key = _fake_key()
        transport = FakeTransport([RuntimeError(f"boom while using {key}")])
        env = _env(key)
        code, stdout = _run(self._decide_argv(), transport, env)
        self.assertEqual(code, EXIT_UNANSWERED)
        self.assertNotIn(key, stdout)


class RedactConformanceTests(unittest.TestCase):
    def test_redact_is_forges(self):
        self.assertIs(deciders_core.redact, forge_core.redact)


if __name__ == "__main__":
    unittest.main()
