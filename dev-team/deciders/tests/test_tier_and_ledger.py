"""Offline tests for the `tier` subcommand and the spend ledger.

No test calls the network. FakeTransport records every call and replays a
fixture. No key value appears in any fixture.
"""
from __future__ import annotations

import json
import multiprocessing
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import deciders_core
from deciders_core import DecidersError, EXIT_OK, EXIT_UNANSWERED, main

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _fake_key() -> str:
    # Built at runtime from two fragments so the full prefix never appears
    # as one literal substring anywhere in the repo.
    return "".join(("sk-", "or-v1-FAKEKEYFAKEKEY"))


class FakeTransport:
    """Records every call; replays a queued response or raises a queued error.

    I4: also records live request-shape violations recorded in PROBES.md
    (see test_deciders_core.py's FakeTransport for the same check).

    D4 (review): recorded into self.violations, not raised, so a test
    driving main() can't pass just because main()'s catch-all turned the
    violation into an ordinary-looking exit 2 -- see _assert_no_shape_violations
    below, used by every call through _run()."""

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
    """D4 (review): see test_deciders_core.py's identical helper -- every
    test driving main() through a FakeTransport must fail on a recorded
    shape violation, not just on whatever exit code main() produced."""
    violations = getattr(transport, "violations", None)
    if violations:
        raise AssertionError(
            "FakeTransport recorded request-shape violation(s), but the "
            "test did not fail on them: " + "; ".join(violations)
        )


def _run(argv, transport, env, home=None):
    import io
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


def _write_spec(dirpath: Path, text: str = "a routine implementation from a precise spec") -> Path:
    spec_path = dirpath / "spec.md"
    spec_path.write_text(text)
    return spec_path


# --- choose_tier: pure function tests ---------------------------------------

class ChooseTierTests(unittest.TestCase):
    def test_choose_tier_confident(self):
        tier = deciders_core.choose_tier({"fast": 0.1, "standard": 0.7, "deep": 0.2})
        self.assertEqual(tier, "standard")

    def test_choose_tier_split_takes_higher(self):
        tier = deciders_core.choose_tier({"fast": 0.02, "standard": 0.57, "deep": 0.41})
        self.assertEqual(tier, "deep")

    def test_choose_tier_split_low_pair(self):
        tier = deciders_core.choose_tier({"fast": 0.5, "standard": 0.45, "deep": 0.05})
        self.assertEqual(tier, "standard")

    def test_choose_tier_rejects_non_finite_even_when_sum_looks_ok(self):
        # json.loads parses NaN/Infinity; a NaN total makes the naive sum
        # check `abs(total - 1.0) > 0.02` evaluate to False. Must still
        # raise rather than silently returning a guessed tier.
        with self.assertRaises(DecidersError) as ctx:
            deciders_core.choose_tier({"fast": float("nan"), "standard": 0.7, "deep": 0.3})
        self.assertEqual(ctx.exception.label, "deciders: bad-response")

    def test_choose_tier_rejects_infinite_probability(self):
        with self.assertRaises(DecidersError) as ctx:
            deciders_core.choose_tier(
                {"fast": float("inf"), "standard": 0.7, "deep": 0.3}
            )
        self.assertEqual(ctx.exception.label, "deciders: bad-response")

    def test_choose_tier_rejects_negative_probability(self):
        # -0.5 + 0.8 + 0.7 == 1.0, so the sum check alone would let this
        # through; the per-value range check must catch it.
        with self.assertRaises(DecidersError) as ctx:
            deciders_core.choose_tier({"fast": -0.5, "standard": 0.8, "deep": 0.7})
        self.assertEqual(ctx.exception.label, "deciders: bad-response")

    def test_choose_tier_rejects_probability_above_one(self):
        with self.assertRaises(DecidersError) as ctx:
            deciders_core.choose_tier({"fast": 1.5, "standard": -0.3, "deep": -0.2})
        self.assertEqual(ctx.exception.label, "deciders: bad-response")

    def test_choose_tier_rejects_sum_not_close_to_one(self):
        with self.assertRaises(DecidersError) as ctx:
            deciders_core.choose_tier({"fast": 0.2, "standard": 0.2, "deep": 0.2})
        self.assertEqual(ctx.exception.label, "deciders: bad-response")


# --- tier subcommand over main ----------------------------------------------

class TierRejectsUnknownOptionTests(unittest.TestCase):
    def test_tier_rejects_unknown_option(self):
        fixture = _load_fixture("tier_unknown_option.json")
        transport = FakeTransport([fixture])
        env = _env(_fake_key())
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            spec_path = _write_spec(home)
            code, stdout = _run(
                ["tier", "--title", "Rename a variable", "--spec", str(spec_path)],
                transport, env, home=home,
            )
        self.assertEqual(code, EXIT_UNANSWERED)
        self.assertTrue(json.loads(stdout)["error"].startswith("deciders: bad-response"))

    def test_tier_rejects_probabilities_not_summing_to_one(self):
        # 0.2 + 0.2 + 0.2 == 0.6, well outside the +/-0.02 tolerance. Mutant
        # M1 (replace the sum check with `if False:`) must fail this test.
        fixture = _load_fixture("tier_bad_sum.json")
        transport = FakeTransport([fixture])
        env = _env(_fake_key())
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            spec_path = _write_spec(home)
            code, stdout = _run(
                ["tier", "--title", "Rename a variable", "--spec", str(spec_path)],
                transport, env, home=home,
            )
        self.assertEqual(code, EXIT_UNANSWERED)
        self.assertTrue(json.loads(stdout)["error"].startswith("deciders: bad-response"))


class TierQuestionShapeTests(unittest.TestCase):
    def test_tier_question_is_fixed(self):
        fixture = _load_fixture("tier_confident.json")
        transport = FakeTransport([fixture])
        env = _env(_fake_key())
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            spec_path = _write_spec(home)
            code, stdout = _run(
                ["tier", "--title", "Add a helper", "--spec", str(spec_path)],
                transport, env, home=home,
            )
        self.assertEqual(code, EXIT_OK)
        url, headers, body, timeout = transport.calls[0]
        self.assertEqual(url, "https://openrouter.ai/api/alpha/decisions")
        # The live Decisions API rejects an array ("expected record, received
        # array", http-400): questions is a record keyed by question id.
        questions = body["questions"]
        self.assertIsInstance(questions, dict)
        self.assertEqual(list(questions.keys()), ["tier"])
        self.assertEqual(questions["tier"]["type"], "choice")
        self.assertEqual(
            set(questions["tier"]["criteria"].keys()), {"fast", "standard", "deep"}
        )


class DecisionModelListTests(unittest.TestCase):
    def test_decisions_endpoint_defaults_are_listed_as_decision_models(self):
        # The installers refuse these as tier-table models: a decisions-only
        # model cannot run an agent worker.
        self.assertIn(deciders_core.DEFAULT_MODELS["decide"], deciders_core.DECISION_MODELS)
        self.assertIn(deciders_core.DEFAULT_MODELS["tier"], deciders_core.DECISION_MODELS)

    def test_decision_model_list_is_one_line_the_installers_can_read(self):
        src = Path(deciders_core.__file__).read_text()
        lines = [ln for ln in src.splitlines() if ln.startswith("DECISION_MODELS = (")]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].rstrip().endswith(")"))

    def test_I3_additional_decision_models_are_refused(self):
        # typesafe/jev-1.13 and upstage/solar-decide-flash were used as
        # decision models by the design's spike but were missing from the
        # denylist entirely.
        for entry in (
            {"provider": "typesafe", "model": "jev-1.13"},
            {"provider": "typesafe", "model": "jev-1.13-20261027"},  # dated variant
            {"provider": "upstage", "model": "solar-decide-flash"},
        ):
            with self.subTest(entry=entry):
                self.assertTrue(deciders_core._is_decision_model(entry))

    def test_I3_pplx_decider_family_is_refused_not_just_v1_2(self):
        for model in ("pplx-decider-v1.2", "pplx-decider-v2", "pplx-decider-v1.2-20261016"):
            with self.subTest(model=model):
                entry = {"provider": "perplexity", "model": f"perplexity/{model}"}
                self.assertTrue(deciders_core._is_decision_model(entry))

    def test_I3_variant_suffix_is_stripped_before_comparing(self):
        # "liquid/d1.1:nitro" must still be refused -- stripping only the
        # dated "-20261027" suffix style is not enough.
        entry = {"provider": "openrouter", "model": "liquid/d1.1:nitro"}
        self.assertTrue(deciders_core._is_decision_model(entry))

    def test_I3_unrelated_model_is_not_a_decision_model(self):
        entry = {"provider": "anthropic", "model": "claude-haiku-4-5"}
        self.assertFalse(deciders_core._is_decision_model(entry))


class TierOverrideTableTests(unittest.TestCase):
    def test_tier_reads_table(self):
        fixture = _load_fixture("tier_split.json")  # resolves to "deep"
        env = _env(_fake_key())
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            dev_team = home / "dev-team"
            dev_team.mkdir()
            (dev_team / "model-tiers.json").write_text(json.dumps(
                {"deep": {"provider": "anthropic", "model": "claude-opus-5"}}
            ))
            spec_path = _write_spec(home)
            code, stdout = _run(
                ["tier", "--title", "Harden auth", "--spec", str(spec_path)],
                FakeTransport([fixture]), env, home=home,
            )
        self.assertEqual(code, EXIT_OK)
        data = json.loads(stdout)
        self.assertEqual(data["tier"], "deep")
        self.assertEqual(
            data["override"], {"provider": "anthropic", "model": "claude-opus-5"}
        )
        self.assertIsNone(data["override_reason"])

    def test_tier_without_table(self):
        fixture = _load_fixture("tier_split.json")
        env = _env(_fake_key())
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            spec_path = _write_spec(home)

            code, stdout = _run(
                ["tier", "--title", "Harden auth", "--spec", str(spec_path)],
                FakeTransport([fixture]), env, home=home,
            )
            data = json.loads(stdout)
            self.assertEqual(code, EXIT_OK)
            self.assertIsNone(data["override"])
            self.assertEqual(data["override_reason"], "no tier table")

            dev_team = home / "dev-team"
            dev_team.mkdir(exist_ok=True)
            (dev_team / "model-tiers.json").write_text(json.dumps(
                {"fast": {"provider": "x", "model": "y"}}
            ))
            code, stdout = _run(
                ["tier", "--title", "Harden auth", "--spec", str(spec_path)],
                FakeTransport([fixture]), env, home=home,
            )
            data = json.loads(stdout)
            self.assertEqual(code, EXIT_OK)
            self.assertIsNone(data["override"])
            self.assertEqual(data["override_reason"], "no entry for deep")

    def test_tier_drops_decisions_only_model_from_table(self):
        # A hand-edited table can still name a decisions-only model; an
        # agent worker routed to one cannot run, so no override is given.
        fixture = _load_fixture("tier_split.json")  # resolves to "deep"
        env = _env(_fake_key())
        entries = [
            {"provider": "openrouter", "model": "liquid/d1.1"},
            {"provider": "openrouter", "model": "liquid/d1.1-20261027"},
            {"provider": "liquid", "model": "d1.1"},
            {"provider": "openrouter", "model": "perplexity/pplx-decider-v1.2"},
        ]
        for entry in entries:
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                dev_team = home / "dev-team"
                dev_team.mkdir()
                (dev_team / "model-tiers.json").write_text(json.dumps({"deep": entry}))
                spec_path = _write_spec(home)
                code, stdout = _run(
                    ["tier", "--title", "Harden auth", "--spec", str(spec_path)],
                    FakeTransport([fixture]), env, home=home,
                )
                data = json.loads(stdout)
                self.assertEqual(code, EXIT_OK)
                self.assertEqual(data["tier"], "deep")
                self.assertIsNone(data["override"])
                self.assertEqual(
                    data["override_reason"],
                    f"decisions-only model for deep: {entry['provider']}/{entry['model']}",
                )

    def test_tier_keeps_chat_model_whose_name_only_starts_like_one(self):
        fixture = _load_fixture("tier_split.json")
        env = _env(_fake_key())
        entry = {"provider": "openrouter", "model": "liquid/d1.10"}
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "dev-team").mkdir()
            (home / "dev-team" / "model-tiers.json").write_text(json.dumps({"deep": entry}))
            spec_path = _write_spec(home)
            code, stdout = _run(
                ["tier", "--title", "Harden auth", "--spec", str(spec_path)],
                FakeTransport([fixture]), env, home=home,
            )
        data = json.loads(stdout)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(data["override"], entry)
        self.assertIsNone(data["override_reason"])

    def test_M8_rejects_a_table_entry_with_an_empty_model(self):
        # Reproduction: the Unix installer used to accept `--tier-fast
        # anthropic:` (empty model), which produced exactly this entry.
        fixture = _load_fixture("tier_split.json")
        env = _env(_fake_key())
        entry = {"provider": "anthropic", "model": ""}
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "dev-team").mkdir()
            (home / "dev-team" / "model-tiers.json").write_text(json.dumps({"deep": entry}))
            spec_path = _write_spec(home)
            code, stdout = _run(
                ["tier", "--title", "Harden auth", "--spec", str(spec_path)],
                FakeTransport([fixture]), env, home=home,
            )
        data = json.loads(stdout)
        self.assertEqual(code, EXIT_OK)
        self.assertIsNone(data["override"])
        self.assertIn("invalid entry", data["override_reason"])

    def test_M8_rejects_a_table_entry_that_is_not_a_dict(self):
        fixture = _load_fixture("tier_split.json")
        env = _env(_fake_key())
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "dev-team").mkdir()
            (home / "dev-team" / "model-tiers.json").write_text(
                json.dumps({"deep": "anthropic/claude-opus-5"}))
            spec_path = _write_spec(home)
            code, stdout = _run(
                ["tier", "--title", "Harden auth", "--spec", str(spec_path)],
                FakeTransport([fixture]), env, home=home,
            )
        data = json.loads(stdout)
        self.assertEqual(code, EXIT_OK)
        self.assertIsNone(data["override"])
        self.assertIn("invalid entry", data["override_reason"])

    def test_M8_rejects_a_table_entry_with_an_empty_provider(self):
        fixture = _load_fixture("tier_split.json")
        env = _env(_fake_key())
        entry = {"provider": "", "model": "claude-opus-5"}
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "dev-team").mkdir()
            (home / "dev-team" / "model-tiers.json").write_text(json.dumps({"deep": entry}))
            spec_path = _write_spec(home)
            code, stdout = _run(
                ["tier", "--title", "Harden auth", "--spec", str(spec_path)],
                FakeTransport([fixture]), env, home=home,
            )
        data = json.loads(stdout)
        self.assertEqual(code, EXIT_OK)
        self.assertIsNone(data["override"])
        self.assertIn("invalid entry", data["override_reason"])


# --- ledger -------------------------------------------------------------

class LedgerWrittenOnSuccessTests(unittest.TestCase):
    def test_ledger_written_on_success_only(self):
        fixture = _load_fixture("decide_ok.json")
        env = _env(_fake_key())
        env["HERMES_KANBAN_BOARD"] = "myboard"
        env["HERMES_KANBAN_TASK"] = "t_123"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            code, stdout = _run(
                ["decide", "--state", str(FIXTURES / "decide_ok.json"),
                 "--questions", str(FIXTURES / "decide_questions.json")],
                FakeTransport([fixture]), env, home=home,
            )
            self.assertEqual(code, EXIT_OK)

            ledger = deciders_core.ledger_path(home)
            lines = ledger.read_text().splitlines()
            self.assertEqual(len(lines), 1)
            record = json.loads(lines[0])
            self.assertEqual(record["board"], "myboard")
            self.assertEqual(record["task_id"], "t_123")
            self.assertEqual(record["subcommand"], "decide")
            self.assertEqual(record["model"], fixture["model"])
            self.assertEqual(record["cost"], fixture["cost"])

            # An exit-2 call (no key) must never append.
            no_key_env = {"HERMES_KANBAN_BOARD": "myboard", "HERMES_KANBAN_TASK": "t_123"}
            code2, _ = _run(
                ["decide", "--state", str(FIXTURES / "decide_ok.json"),
                 "--questions", str(FIXTURES / "decide_questions.json")],
                FakeTransport([]), no_key_env, home=home,
            )
            self.assertEqual(code2, EXIT_UNANSWERED)
            lines_after = ledger.read_text().splitlines()
            self.assertEqual(len(lines_after), 1)


class DispatchedWorkerTaskIdTests(unittest.TestCase):
    """T3: proves the real root cause and its fix.

    A dispatched worker's *subprocess* environment (what `deciders` is
    actually invoked with, via a plain shell or `secret exec`) was probed
    directly on a live dispatched task: HERMES_KANBAN_BOARD,
    HERMES_KANBAN_WORKSPACE and sibling vars were present, but
    HERMES_KANBAN_TASK was not -- `printenv HERMES_KANBAN_TASK` failed
    even with no `secret exec` involved. So `env.get("HERMES_KANBAN_TASK")`
    alone can never populate the ledger's task_id from that process. This
    test models exactly that environment (board set, task env var absent)
    and proves the `--task-id` flag is what makes task_id non-null.
    """

    def test_task_id_env_var_absent_still_yields_non_null_task_id_via_flag(self):
        fixture = _load_fixture("decide_ok.json")
        # Models the probed dispatched-worker subprocess env exactly:
        # board present, task id genuinely absent (not just empty).
        env = _env(_fake_key())
        env["HERMES_KANBAN_BOARD"] = "dev-team-followups"
        self.assertNotIn("HERMES_KANBAN_TASK", env)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            code, stdout = _run(
                ["decide", "--state", str(FIXTURES / "decide_ok.json"),
                 "--questions", str(FIXTURES / "decide_questions.json"),
                 "--task-id", "t_a77c9659"],
                FakeTransport([fixture]), env, home=home,
            )
            self.assertEqual(code, EXIT_OK)
            ledger = deciders_core.ledger_path(home)
            record = json.loads(ledger.read_text().splitlines()[0])
            self.assertEqual(record["board"], "dev-team-followups")
            self.assertEqual(record["task_id"], "t_a77c9659")

    def test_task_id_flag_wins_over_env_var_when_both_present(self):
        fixture = _load_fixture("decide_ok.json")
        env = _env(_fake_key())
        env["HERMES_KANBAN_TASK"] = "t_stale_env_value"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            code, _ = _run(
                ["decide", "--state", str(FIXTURES / "decide_ok.json"),
                 "--questions", str(FIXTURES / "decide_questions.json"),
                 "--task-id", "t_explicit"],
                FakeTransport([fixture]), env, home=home,
            )
            self.assertEqual(code, EXIT_OK)
            ledger = deciders_core.ledger_path(home)
            record = json.loads(ledger.read_text().splitlines()[0])
            self.assertEqual(record["task_id"], "t_explicit")

    def test_no_task_id_flag_falls_back_to_env_var(self):
        # Backward compatible: callers that still rely on the env var
        # (e.g. a non-dispatched interactive session where it happens to
        # be set) keep working with no flag passed at all.
        fixture = _load_fixture("decide_ok.json")
        env = _env(_fake_key())
        env["HERMES_KANBAN_TASK"] = "t_from_env"
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            code, _ = _run(
                ["decide", "--state", str(FIXTURES / "decide_ok.json"),
                 "--questions", str(FIXTURES / "decide_questions.json")],
                FakeTransport([fixture]), env, home=home,
            )
            self.assertEqual(code, EXIT_OK)
            ledger = deciders_core.ledger_path(home)
            record = json.loads(ledger.read_text().splitlines()[0])
            self.assertEqual(record["task_id"], "t_from_env")

    def test_env_task_id_containing_key_is_recorded_as_null_not_failed(self):
        # D2 (review): the same validation applies to the env fallback,
        # but an invalid env value silently becomes null -- it never
        # fails the call, because this runs after a billed transport
        # call already succeeded.
        fixture = _load_fixture("decide_ok.json")
        key = _fake_key()
        env = _env(key)
        env["HERMES_KANBAN_TASK"] = key
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            code, stdout = _run(
                ["decide", "--state", str(FIXTURES / "decide_ok.json"),
                 "--questions", str(FIXTURES / "decide_questions.json")],
                FakeTransport([fixture]), env, home=home,
            )
            self.assertEqual(code, EXIT_OK)
            self.assertNotIn(key, stdout)
            ledger = deciders_core.ledger_path(home)
            record = json.loads(ledger.read_text().splitlines()[0])
            self.assertIsNone(record["task_id"])

    def test_env_task_id_too_long_is_recorded_as_null_not_failed(self):
        fixture = _load_fixture("decide_ok.json")
        env = _env(_fake_key())
        env["HERMES_KANBAN_TASK"] = "a" * 5000
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            code, _ = _run(
                ["decide", "--state", str(FIXTURES / "decide_ok.json"),
                 "--questions", str(FIXTURES / "decide_questions.json")],
                FakeTransport([fixture]), env, home=home,
            )
            self.assertEqual(code, EXIT_OK)
            ledger = deciders_core.ledger_path(home)
            record = json.loads(ledger.read_text().splitlines()[0])
            self.assertIsNone(record["task_id"])


class M1LedgerWrittenOnFailedValidationTests(unittest.TestCase):
    """M1: the transport returned (and was billed), but validation after
    that still fails -- a ledger line must be written anyway, with
    whatever model/cost could be read, cost 0.0 if it doesn't parse."""

    def test_tier_unknown_option_still_writes_a_ledger_line(self):
        # Reproduction from the review: `tier` with probabilities
        # {fast, standard, huge} used to exit 2 and write NO ledger file.
        fixture = _load_fixture("tier_unknown_option.json")
        env = _env(_fake_key())
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            spec_path = _write_spec(home)
            code, stdout = _run(
                ["tier", "--title", "Rename a variable", "--spec", str(spec_path)],
                FakeTransport([fixture]), env, home=home,
            )
            self.assertEqual(code, EXIT_UNANSWERED)
            self.assertTrue(json.loads(stdout)["error"].startswith("deciders: bad-response"))
            ledger = deciders_core.ledger_path(home)
            lines = ledger.read_text().splitlines()
            self.assertEqual(len(lines), 1)
            record = json.loads(lines[0])
            self.assertEqual(record["subcommand"], "tier")
            self.assertEqual(record["model"], fixture["model"])
            self.assertEqual(record["cost"], fixture["cost"])

    def test_bad_response_with_unparseable_cost_writes_cost_zero(self):
        # The response came back (billed) but its cost field itself does
        # not parse, and the shape also fails answers validation -- the
        # ledger line must still exist, with cost 0.0 (never guessed, never
        # dropped).
        fixture = {"answers": "not-a-dict", "model": "liquid/d1.1-20261027",
                   "cost": "not-a-number"}
        env = _env(_fake_key())
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            code, stdout = _run(
                ["decide", "--state", str(FIXTURES / "decide_ok.json"),
                 "--questions", str(FIXTURES / "decide_questions.json")],
                FakeTransport([fixture]), env, home=home,
            )
            self.assertEqual(code, EXIT_UNANSWERED)
            ledger = deciders_core.ledger_path(home)
            lines = ledger.read_text().splitlines()
            self.assertEqual(len(lines), 1)
            record = json.loads(lines[0])
            self.assertEqual(record["cost"], 0.0)
            self.assertEqual(record["model"], "liquid/d1.1-20261027")


def _ledger_worker(home_str: str, count: int) -> None:
    """Module-level so multiprocessing can pickle/fork it cleanly."""
    deciders_dir = str(Path(__file__).resolve().parent.parent)
    if deciders_dir not in sys.path:
        sys.path.insert(0, deciders_dir)
    import deciders_core as dc
    home = Path(home_str)
    for _ in range(count):
        dc.append_ledger(home, {}, "decide", "liquid/d1.1", 0.0001)


class LedgerConcurrencyTests(unittest.TestCase):
    def test_ledger_concurrent_appends_stay_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            procs = [
                multiprocessing.Process(target=_ledger_worker, args=(str(home), 200))
                for _ in range(2)
            ]
            for p in procs:
                p.start()
            for p in procs:
                p.join()
            for p in procs:
                self.assertEqual(p.exitcode, 0)

            lines = deciders_core.ledger_path(home).read_text().splitlines()
            self.assertEqual(len(lines), 400)
            for line in lines:
                record = json.loads(line)  # each line is whole, valid JSON
                self.assertEqual(record["subcommand"], "decide")



class LedgerWriteFailureTests(unittest.TestCase):
    """Review r2 D1: an unwritable ledger must still give the labelled
    {"error": ...} / exit 2 contract, never an escaping exception."""

    def test_unwritable_ledger_is_a_labelled_error_not_a_traceback(self):
        fixture = _load_fixture("tier_confident.json")
        env = _env(_fake_key())
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            spec_path = _write_spec(home)
            (home / "dev-team").write_text("a file where the team dir should be")
            code, stdout = _run(
                ["tier", "--title", "Rename a variable", "--spec", str(spec_path)],
                FakeTransport([fixture]), env, home=home,
            )
            self.assertEqual(code, EXIT_UNANSWERED)
            self.assertIn("error", json.loads(stdout))


class KeyWhitespaceTests(unittest.TestCase):
    """Review r2, item 9 remainder: a key read with a trailing newline (a
    common `secret` output artefact) is stripped before it is sent."""

    def test_trailing_newline_is_stripped_from_the_authorization_header(self):
        fixture = _load_fixture("tier_confident.json")
        transport = FakeTransport([fixture])
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            spec_path = _write_spec(home)
            code, _ = _run(
                ["tier", "--title", "Rename a variable", "--spec", str(spec_path)],
                transport, _env(_fake_key() + "\n"), home=home,
            )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(transport.calls[0][1]["Authorization"], "Bearer " + _fake_key())


if __name__ == "__main__":
    unittest.main()
