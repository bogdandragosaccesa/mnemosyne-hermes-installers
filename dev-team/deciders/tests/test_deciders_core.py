"""Offline tests for deciders_core: decide, extract, summarize.

No test calls the network. FakeTransport records every call and either
returns a canned (fixture) response or raises a given DecidersError.
"""
from __future__ import annotations

import email.message
import io
import json
import socket
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

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
    # str.join is a runtime call, so CPython cannot constant-fold the two
    # fragments into one literal in the .pyc (a `+` of literals would be).
    return "".join(("sk-", "or-v1-FAKEKEYFAKEKEY"))


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

        entities_spec = {"types": ["synthetic-secret"]}
        with tempfile.TemporaryDirectory() as tmp:
            entities_path = Path(tmp) / "entities_spec.json"
            entities_path.write_text(json.dumps(entities_spec))
            input_path = Path(tmp) / "extract_input.txt"
            input_path.write_text("some text with EXAMPLE-TOKEN-0001 in it")
            code, stdout = _run(
                ["extract", "--entities", str(entities_path), "--input", str(input_path)],
                transport, env,
            )

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

        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "summarize_input.txt"
            input_path.write_text("background material to compress")
            code, stdout = _run(
                ["summarize", "--input", str(input_path)], transport, env,
            )

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


# --- Real classification: urllib_transport with urlopen patched -------------

_ERROR_BODY = "UPSTREAM-ERROR-BODY-MARKER"


def _http_error(code):
    return urllib.error.HTTPError(
        "https://example.invalid", code, "synthetic", email.message.Message(),
        io.BytesIO(_ERROR_BODY.encode("utf-8")),
    )


class _FakeResponse:
    """Minimal context-manager stand-in for the object urlopen returns."""

    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ok_response(obj):
    return _FakeResponse(json.dumps(obj).encode("utf-8"))


class UrllibTransportClassificationTests(unittest.TestCase):
    """Drive the real urllib_transport; only urlopen itself is faked."""

    def _classify(self, side_effect):
        with mock.patch("urllib.request.urlopen", side_effect=side_effect):
            with self.assertRaises(DecidersError) as ctx:
                deciders_core.urllib_transport(
                    "https://example.invalid", {}, {"model": "m"}, 1.0)
        return ctx.exception

    def test_http_status_labels(self):
        cases = {
            401: "deciders: auth",
            402: "deciders: key-cap",
            403: "deciders: forbidden",
            404: "deciders: model-unavailable",
            418: "deciders: http-418",
            429: "deciders: upstream",
            503: "deciders: upstream",
        }
        for code, label in cases.items():
            with self.subTest(code=code):
                exc = self._classify(_http_error(code))
                self.assertEqual(exc.label, label)
                self.assertNotIn(_ERROR_BODY, str(exc))

    def test_timeout_error_is_timeout(self):
        exc = self._classify(TimeoutError("timed out"))
        self.assertEqual(exc.label, "deciders: timeout")

    def test_urlerror_wrapping_socket_timeout_is_timeout(self):
        exc = self._classify(urllib.error.URLError(socket.timeout("timed out")))
        self.assertEqual(exc.label, "deciders: timeout")

    def test_connection_reset_is_upstream(self):
        exc = self._classify(ConnectionResetError("reset by peer"))
        self.assertEqual(exc.label, "deciders: upstream")

    def test_undecodable_body_is_bad_response(self):
        exc = self._classify([_FakeResponse(b"\xff\xfe\xfa")])
        self.assertEqual(exc.label, "deciders: bad-response")


class MainOverRealTransportTests(unittest.TestCase):
    def _decide_argv(self):
        return ["decide", "--state", str(FIXTURES / "decide_ok.json"),
                "--questions", str(FIXTURES / "decide_ok.json")]

    def test_main_retries_real_429_then_succeeds(self):
        fixture = _load_fixture("decide_ok.json")
        side_effect = [_http_error(429), _ok_response(fixture)]
        with mock.patch("urllib.request.urlopen", side_effect=side_effect) as urlopen:
            code, stdout = _run(self._decide_argv(), deciders_core.urllib_transport,
                                _env(_fake_key()))
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(json.loads(stdout)["answers"], fixture["answers"])

    def test_main_real_429_twice_is_upstream(self):
        side_effect = [_http_error(429), _http_error(429)]
        with mock.patch("urllib.request.urlopen", side_effect=side_effect) as urlopen:
            code, stdout = _run(self._decide_argv(), deciders_core.urllib_transport,
                                _env(_fake_key()))
        self.assertEqual(code, EXIT_UNANSWERED)
        self.assertEqual(urlopen.call_count, 2)
        error = json.loads(stdout)["error"]
        self.assertTrue(error.startswith("deciders: upstream"))
        self.assertNotIn(_ERROR_BODY, error)
        self.assertNotIn(_fake_key(), stdout)


# --- Response-shape validation ---------------------------------------------

class ResponseShapeTests(unittest.TestCase):
    def _decide(self, response):
        return _run(
            ["decide", "--state", str(FIXTURES / "decide_ok.json"),
             "--questions", str(FIXTURES / "decide_ok.json")],
            FakeTransport([response]), _env(_fake_key()),
        )

    def _summarize(self, response):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = Path(tmp) / "in.txt"
            input_path.write_text("background material")
            return _run(["summarize", "--input", str(input_path)],
                        FakeTransport([response]), _env(_fake_key()))

    def _assert_bad_response(self, result):
        code, stdout = result
        self.assertEqual(code, EXIT_UNANSWERED)
        self.assertTrue(json.loads(stdout)["error"].startswith("deciders: bad-response"))

    def test_non_dict_top_level_is_bad_response(self):
        for runner in (self._decide, self._summarize):
            for response in ([], ["answers"], "text", 3, None):
                with self.subTest(runner=runner.__name__, response=response):
                    self._assert_bad_response(runner(response))

    def test_non_numeric_cost_is_bad_response(self):
        for cost in ("abc", "0.1", None, True, [0.1]):
            with self.subTest(cost=cost):
                response = dict(_load_fixture("decide_ok.json"), cost=cost)
                self._assert_bad_response(self._decide(response))

    def test_missing_cost_is_bad_response(self):
        response = _load_fixture("decide_ok.json")
        del response["cost"]
        self._assert_bad_response(self._decide(response))

    def test_cost_read_from_usage_cost(self):
        response = _load_fixture("summarize_ok.json")
        del response["cost"]
        response["usage"] = {"cost": 0.0003}
        code, stdout = self._summarize(response)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(json.loads(stdout)["cost"], 0.0003)


class EmptyInputTests(unittest.TestCase):
    """No --input (or an empty one) must not send an empty prompt to a paid model."""

    def test_extract_and_summarize_without_input_are_bad_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            entities_path = Path(tmp) / "entities.json"
            entities_path.write_text(json.dumps({"types": ["x"]}))
            empty_path = Path(tmp) / "empty.txt"
            empty_path.write_text("  \n")
            argvs = [
                ["extract", "--entities", str(entities_path)],
                ["extract", "--entities", str(entities_path), "--input", str(empty_path)],
                ["summarize"],
                ["summarize", "--input", str(empty_path)],
            ]
            for argv in argvs:
                with self.subTest(argv=argv):
                    transport = FakeTransport([])
                    code, stdout = _run(argv, transport, _env(_fake_key()))
                    self.assertEqual(code, EXIT_UNANSWERED)
                    self.assertEqual(len(transport.calls), 0)
                    self.assertTrue(
                        json.loads(stdout)["error"].startswith("deciders: bad-input"))


class RedactConformanceTests(unittest.TestCase):
    def test_redact_is_forges(self):
        self.assertIs(deciders_core.redact, forge_core.redact)


if __name__ == "__main__":
    unittest.main()
