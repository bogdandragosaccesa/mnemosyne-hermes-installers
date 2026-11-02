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
    """Records every call; replays a queued response or raises a queued error.

    I4: also records live request-shape violations recorded in PROBES.md,
    so a test that (re)introduces an array `questions` or a nested/list
    `entities` is caught here even if production code's own validation
    were ever weakened or bypassed.

    D4 (review): a shape violation is *recorded*, not raised. Raising
    `AssertionError` here used to work by accident only because nothing
    in `main()` swallowed it on the happy path -- but `main()`'s
    catch-all `except Exception` DOES catch `AssertionError`, turns it
    into a generic `{"error": ...}` + exit 2, and a test that only
    checks `code == EXIT_UNANSWERED` (exit 2 means "answered with an
    error", not specifically "the input was rejected before any call")
    would pass even though the real bug -- a bad shape reaching the
    transport at all -- went undetected. Recording into `self.violations`
    and asserting that list is empty in `_run` (used by every test that
    drives `main()`) makes the failure visible regardless of what exit
    code `main()` happens to produce."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.violations = []

    def __call__(self, url, headers, body, timeout):
        self.calls.append((url, headers, body, timeout))
        if url == "https://openrouter.ai/api/alpha/decisions" and "questions" in body:
            questions = body["questions"]
            if not isinstance(questions, dict):
                self.violations.append(
                    f"questions must be a record (dict), not {type(questions).__name__}")
            else:
                for qid, spec in questions.items():
                    if not (isinstance(spec, dict) and spec.get("type")):
                        self.violations.append(
                            f"questions[{qid!r}] must be an object with a 'type'")
        if url == "https://openrouter.ai/api/v1/chat/completions":
            entities = (body.get("provider") or {}).get("options", {}).get(
                "scaledown", {}).get("entities")
            if entities is not None:
                if not isinstance(entities, dict):
                    self.violations.append(
                        f"entities must be a flat dict, not {type(entities).__name__}")
                else:
                    for name, desc in entities.items():
                        if not (isinstance(name, str) and isinstance(desc, str)):
                            self.violations.append(
                                "entities must be a flat str -> str record")
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


def _assert_no_shape_violations(transport) -> None:
    """D4 (review): every test that drives `main()` through a FakeTransport
    must fail -- not silently pass with whatever exit code `main()`
    produced -- if the fake recorded a request-shape violation. Without
    this, `main()`'s catch-all `except Exception` turns the fake's
    detection into an ordinary-looking `{"error": ...}` / exit 2 that is
    indistinguishable from a legitimate rejection."""
    violations = getattr(transport, "violations", None)
    if violations:
        raise AssertionError(
            "FakeTransport recorded request-shape violation(s), but the "
            "test did not fail on them: " + "; ".join(violations)
        )


def _run(argv, transport, env, home=None):
    out = io.StringIO()
    if home is None:
        # Never fall back to a fixed real path: every test run would then
        # share (and keep appending to) one real ledger file outside any
        # test's sandbox, and would fail with exit 2 wherever that path
        # isn't writable.
        with tempfile.TemporaryDirectory() as tmp:
            code = main(argv, home=Path(tmp), env=env, transport=transport, out=out)
            _assert_no_shape_violations(transport)
            return code, out.getvalue()
    code = main(argv, home=home, env=env, transport=transport, out=out)
    _assert_no_shape_violations(transport)
    return code, out.getvalue()


class DecideRequestTests(unittest.TestCase):
    def test_decide_builds_request(self):
        fixture = _load_fixture("decide_ok.json")
        transport = FakeTransport([fixture])
        env = _env(_fake_key())

        code, stdout = _run(
            ["decide", "--state", str(FIXTURES / "decide_ok.json"),
             "--questions", str(FIXTURES / "decide_questions.json")],
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

        entities_spec = {"synthetic-secret": "a fake example credential pattern, for tests only"}
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
             "--questions", str(FIXTURES / "decide_questions.json"),
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
             "--questions", str(FIXTURES / "decide_questions.json")],
            transport, env,
        )

        self.assertEqual(code, EXIT_UNANSWERED)
        self.assertEqual(len(transport.calls), 0)
        data = json.loads(stdout)
        self.assertTrue(data["error"].startswith("deciders: no-key"))


class StatusLabelTests(unittest.TestCase):
    def _decide_argv(self):
        return ["decide", "--state", str(FIXTURES / "decide_ok.json"),
                "--questions", str(FIXTURES / "decide_questions.json")]

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
             "--questions", str(FIXTURES / "decide_questions.json")],
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
             "--questions", str(FIXTURES / "decide_questions.json")],
            transport, env,
        )

        self.assertEqual(code, EXIT_UNANSWERED)
        data = json.loads(stdout)
        self.assertTrue(data["error"].startswith("deciders: bad-response"))


class KeyRedactionTests(unittest.TestCase):
    def _decide_argv(self):
        return ["decide", "--state", str(FIXTURES / "decide_ok.json"),
                "--questions", str(FIXTURES / "decide_questions.json")]

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


class TaskIdValidationTests(unittest.TestCase):
    """D2 (review): `--task-id` is validated before the handler runs, so a
    bad value never spends money and never reaches the ledger. Every
    rejection is the same labelled `deciders: bad-input: task-id` --
    never the rejected value itself."""

    def _decide_argv(self, task_id):
        return [
            "decide", "--state", str(FIXTURES / "decide_ok.json"),
            "--questions", str(FIXTURES / "decide_questions.json"),
            "--task-id", task_id,
        ]

    def _assert_rejected(self, task_id):
        key = _fake_key()
        transport = FakeTransport([])  # never reached if rejection is pre-handler
        env = _env(key)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            code, stdout = _run(self._decide_argv(task_id), transport, env, home=home)
            self.assertEqual(code, EXIT_UNANSWERED)
            self.assertEqual(len(transport.calls), 0, "handler must not run on bad task-id")
            data = json.loads(stdout)
            self.assertTrue(data["error"].startswith("deciders: bad-input"))
            self.assertIn("task-id", data["error"])
            self.assertNotIn(key, stdout)
            if task_id != key:
                self.assertNotIn(task_id, stdout)
            ledger = deciders_core.ledger_path(home)
            self.assertFalse(ledger.exists(), "a rejected task-id must never reach the ledger")

    def test_key_as_task_id_is_rejected(self):
        self._assert_rejected(_fake_key())

    def test_bearer_key_as_task_id_is_rejected(self):
        self._assert_rejected(f"Bearer {_fake_key()}")

    def test_5000_char_task_id_is_rejected(self):
        self._assert_rejected("a" * 5000)

    def test_task_id_with_newline_is_rejected(self):
        self._assert_rejected("t_abc\ndef")

    def test_valid_task_id_is_accepted(self):
        fixture = _load_fixture("decide_ok.json")
        key = _fake_key()
        transport = FakeTransport([fixture])
        env = _env(key)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            code, stdout = _run(
                self._decide_argv("t_1a2b3c4d"), transport, env, home=home
            )
            self.assertEqual(code, EXIT_OK)
            ledger = deciders_core.ledger_path(home)
            record = json.loads(ledger.read_text().splitlines()[0])
            self.assertEqual(record["task_id"], "t_1a2b3c4d")


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
                "--questions", str(FIXTURES / "decide_questions.json")]

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
             "--questions", str(FIXTURES / "decide_questions.json")],
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

    def test_non_finite_or_negative_cost_is_bad_response(self):
        # I1: a NaN, infinite or negative cost must never reach the ledger
        # (it would poison the board's budget gate -- see kanban-budget.py).
        for cost in (float("nan"), float("inf"), float("-inf"), -50, -0.001):
            with self.subTest(cost=cost):
                response = dict(_load_fixture("decide_ok.json"), cost=cost)
                self._assert_bad_response(self._decide(response))


class EmptyInputTests(unittest.TestCase):
    """No --input (or an empty one) must not send an empty prompt to a paid model."""

    def test_extract_and_summarize_without_input_are_bad_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            entities_path = Path(tmp) / "entities.json"
            entities_path.write_text(json.dumps({"x": "a fake example pattern, for tests only"}))
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


class I4RequestShapeTests(unittest.TestCase):
    """I4: decide/extract validate the live request shapes before any
    network call -- an array `questions` or a nested/list `entities` is
    `bad-input`, never sent."""

    def _decide_with_questions(self, tmp: Path, questions) -> tuple[int, str]:
        questions_path = tmp / "questions.json"
        questions_path.write_text(json.dumps(questions))
        transport = FakeTransport([])
        code, stdout = _run(
            ["decide", "--state", str(FIXTURES / "decide_ok.json"),
             "--questions", str(questions_path)],
            transport, _env(_fake_key()),
        )
        self.assertEqual(len(transport.calls), 0, "network call made on bad input")
        return code, stdout

    def test_decide_rejects_an_array_of_questions(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, stdout = self._decide_with_questions(
                Path(tmp), [{"type": "choice"}])
        self.assertEqual(code, EXIT_UNANSWERED)
        self.assertTrue(json.loads(stdout)["error"].startswith("deciders: bad-input"))

    def test_decide_rejects_a_question_without_a_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, stdout = self._decide_with_questions(
                Path(tmp), {"q1": {"instructions": "no type here"}})
        self.assertEqual(code, EXIT_UNANSWERED)
        self.assertTrue(json.loads(stdout)["error"].startswith("deciders: bad-input"))

    def test_decide_rejects_empty_questions(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, stdout = self._decide_with_questions(Path(tmp), {})
        self.assertEqual(code, EXIT_UNANSWERED)
        self.assertTrue(json.loads(stdout)["error"].startswith("deciders: bad-input"))

    def _extract_with_entities(self, tmp: Path, entities) -> tuple[int, str]:
        entities_path = tmp / "entities.json"
        entities_path.write_text(json.dumps(entities))
        input_path = tmp / "in.txt"
        input_path.write_text("some text")
        transport = FakeTransport([])
        code, stdout = _run(
            ["extract", "--entities", str(entities_path), "--input", str(input_path)],
            transport, _env(_fake_key()),
        )
        self.assertEqual(len(transport.calls), 0, "network call made on bad input")
        return code, stdout

    def test_extract_rejects_the_nested_entities_shape(self):
        # PROBES.md step 2c: {"entities": [{"type": ..., "pattern": ...}]}
        # silently returned zero entities live.
        with tempfile.TemporaryDirectory() as tmp:
            code, stdout = self._extract_with_entities(
                Path(tmp), {"entities": [{"type": "x", "pattern": "y"}]})
        self.assertEqual(code, EXIT_UNANSWERED)
        self.assertTrue(json.loads(stdout)["error"].startswith("deciders: bad-input"))

    def test_extract_rejects_an_array_of_type_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, stdout = self._extract_with_entities(Path(tmp), ["credential"])
        self.assertEqual(code, EXIT_UNANSWERED)
        self.assertTrue(json.loads(stdout)["error"].startswith("deciders: bad-input"))

    def test_extract_rejects_the_old_types_list_shape(self):
        # The old documented shape, {"types": [...]}, was a silent false
        # negative live (exit 0, zero entities).
        with tempfile.TemporaryDirectory() as tmp:
            code, stdout = self._extract_with_entities(
                Path(tmp), {"types": ["synthetic-secret"]})
        self.assertEqual(code, EXIT_UNANSWERED)
        self.assertTrue(json.loads(stdout)["error"].startswith("deciders: bad-input"))

    def test_extract_accepts_the_flat_record_shape(self):
        fixture = _load_fixture("extract_ok.json")
        with tempfile.TemporaryDirectory() as tmp:
            entities_path = Path(tmp) / "entities.json"
            entities_path.write_text(
                json.dumps({"synthetic-secret": "a fake example pattern"}))
            input_path = Path(tmp) / "in.txt"
            input_path.write_text("some text")
            code, stdout = _run(
                ["extract", "--entities", str(entities_path), "--input", str(input_path)],
                FakeTransport([fixture]), _env(_fake_key()),
            )
        self.assertEqual(code, EXIT_OK)


class M2ErrorLabellingTests(unittest.TestCase):
    """M2: argparse errors (SystemExit) and a bare ValueError from
    http.client must come out labelled 'deciders: ...' on stdout, never an
    unlabelled message -- and the key must still be stripped."""

    def test_argparse_error_is_labelled_bad_input_not_a_crash(self):
        out = io.StringIO()
        transport = FakeTransport([])
        code = main(["decide", "--state"], home=Path("/nonexistent"), env={},
                     transport=transport, out=out)
        _assert_no_shape_violations(transport)
        self.assertEqual(code, EXIT_UNANSWERED)
        data = json.loads(out.getvalue())
        self.assertTrue(data["error"].startswith("deciders: bad-input"))

    def test_unknown_subcommand_is_labelled_bad_input_not_a_crash(self):
        out = io.StringIO()
        transport = FakeTransport([])
        code = main(["not-a-command"], home=Path("/nonexistent"), env={},
                     transport=transport, out=out)
        _assert_no_shape_violations(transport)
        self.assertEqual(code, EXIT_UNANSWERED)
        data = json.loads(out.getvalue())
        self.assertTrue(data["error"].startswith("deciders: bad-input"))

    def test_help_exits_cleanly_and_is_not_wrapped_as_bad_input(self):
        # Regression: SystemExit(0) from argparse's own --help handling
        # must propagate untouched -- not be caught by the generic
        # SystemExit-to-bad-input conversion added for M2. `deciders
        # --help` is how installers' smoke checks verify the CLI works.
        out = io.StringIO()
        with self.assertRaises(SystemExit) as ctx:
            main(["--help"], home=Path("/nonexistent"), env={},
                 transport=FakeTransport([]), out=out)
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(out.getvalue(), "")

    def test_value_error_from_transport_is_labelled_and_key_stripped(self):
        key = _fake_key()
        # http.client raises a bare ValueError for a malformed header value
        # (e.g. a key with an embedded newline, a common `secret` artifact).
        transport = FakeTransport([ValueError(f"Invalid header value b'Bearer {key}")])
        code, stdout = _run(
            ["decide", "--state", str(FIXTURES / "decide_ok.json"),
             "--questions", str(FIXTURES / "decide_questions.json")],
            transport, _env(key),
        )
        self.assertEqual(code, EXIT_UNANSWERED)
        data = json.loads(stdout)
        self.assertTrue(data["error"].startswith("deciders: bad-input"))
        self.assertNotIn(key, stdout)


class D4FakeShapeViolationIsNotSwallowedTests(unittest.TestCase):
    """Review D4 regression: if `_require_questions_record`'s own
    validation is ever bypassed, a bad shape reaching the transport must
    fail the test outright -- not quietly produce the ordinary-looking
    exit 2 that `main()`'s catch-all gives every other error.

    Proof: with the real validator patched into a no-op, a deliberately
    wrong `questions` shape (a list, not a record) now reaches
    FakeTransport. The fake records the violation; `_run`'s
    `_assert_no_shape_violations` call turns that into a hard
    `AssertionError`, which is what this test expects. Before the D4
    fix (FakeTransport raising a plain `AssertionError` inside
    `__call__`, with no post-hoc list to check), that same AssertionError
    was caught by `main()`'s `except Exception`, converted to
    `{"error": "questions must be a record ..."}`, and `main()` returned
    EXIT_UNANSWERED (2) -- a code indistinguishable from any other
    legitimate rejection, so a test checking only `code == EXIT_UNANSWERED`
    passed even though the real bug (a bad shape reaching the network
    call) went completely undetected.
    """

    def test_bypassed_validation_is_caught_not_swallowed_as_exit_2(self):
        fixture = _load_fixture("decide_ok.json")
        transport = FakeTransport([fixture])
        env = _env(_fake_key())
        with tempfile.TemporaryDirectory() as tmp:
            # Deliberately the wrong shape per PROBES.md: a list, not a
            # record keyed by question id. With real validation in place
            # this never reaches the transport at all (see
            # I4RequestShapeTests); here we bypass that validator to
            # reproduce exactly what D4 is about: a shape bug that
            # reaches the wire.
            questions_path = Path(tmp) / "bad_questions.json"
            questions_path.write_text(json.dumps([{"type": "choice"}]))
            with mock.patch(
                "deciders_core._require_questions_record", side_effect=lambda q: q
            ):
                with self.assertRaises(AssertionError) as ctx:
                    _run(
                        ["decide", "--state", str(FIXTURES / "decide_ok.json"),
                         "--questions", str(questions_path)],
                        transport, env,
                    )
        self.assertIn("questions must be a record", str(ctx.exception))
        # The fake really did get called with the bad shape -- this is
        # not a test tautology, the request was actually sent.
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(transport.violations), 1)


if __name__ == "__main__":
    unittest.main()
