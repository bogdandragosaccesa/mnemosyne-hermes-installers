"""Tests for dev-team/scripts/env_pin_check.py (the .env pin-safety checker).

Stdlib unittest only. Run:
    python3 -m unittest discover -s dev-team/scripts/tests

Three layers:
  * str/bytes-level unit tests of the transcribed readers and the verdict;
  * subprocess tests that run the checker the way the installers do (value in
    DEV_TEAM_ENV_CHECK_VALUE, KEY in argv), under four locale set-ups;
  * a differential test against the REAL python-dotenv / hermes_cli.env_loader
    / mnemosyne readers, run in whatever venv python has them (found at test
    time, or named by env var); skipped cleanly when none is found.

No test touches a real Hermes home, profile or Mnemosyne bank: every file a
real reader sees is in a fresh temp dir, and the differential helper runs
with HOME / HERMES_HOME / MNEMOSYNE_HOME / MNEMOSYNE_DATA_DIR pointed there.
"""
from __future__ import annotations

import base64
import glob
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "env_pin_check.py"
spec = importlib.util.spec_from_file_location("env_pin_check", SCRIPT)
assert spec is not None and spec.loader is not None
epc = importlib.util.module_from_spec(spec)
sys.modules["env_pin_check"] = epc
spec.loader.exec_module(epc)

KEY = "MNEMOSYNE_HOME"
T = "/tmp/t"
NEWLINES = ("\n", "\r\n")  # Unix installer / Windows WriteAllLines

# --- the review's §2(b) table, plus the acceptance-criteria rows ------------
MID_PATH_BREAKERS = ["\u2028", "\u2029", "\u0085", "\x0b", "\x0c",
                     "\x1c", "\x1d", "\x1e"]
REVIEW_ROWS = (
    [f"{T}/a{c}b" for c in MID_PATH_BREAKERS]
    + [f"{T}/a\u00a0#b", f"{T}/bank\u00a0", f"{T}/bank\x1f",
       f"{T}/z\u2028y", f"{T}/a\nb", f"{T}/a\rb"]
)
ISSPACE = [chr(i) for i in range(0x110000) if chr(i).isspace()]
# The pre-existing refusals (kept as a subset).
LEGACY_ROWS = [f"'{T}/q", f'"{T}/q', f"{T}/q'", f'{T}/q"', f"{T}/a${{HOME}}b",
               f"{T}/a${{b", f"{T}/a #b", f"{T}/a\t#b"]


def _ordinary_sample() -> list[str]:
    """>= 1000 ordinary letters from the scripts the card names."""
    ranges = [
        (0x00C0, 0x00FF),  # Latin-1 Supplement letters
        (0x0100, 0x017F),  # Latin Extended-A (ă ą ...)
        (0x0180, 0x024F),  # Latin Extended-B (ș ț ...)
        (0x0370, 0x03FF),  # Greek
        (0x0400, 0x04FF),  # Cyrillic (х)
        (0x05D0, 0x05EA),  # Hebrew
        (0x0620, 0x064A),  # Arabic letters
        (0x0904, 0x0939),  # Devanagari letters
        (0x4E00, 0x4EFF),  # CJK
        (0xAC00, 0xACFF),  # Hangul
    ]
    out = [chr(c) for lo, hi in ranges for c in range(lo, hi + 1)
           if unicodedata.category(chr(c)).startswith("L")]
    for must in ("\u0105", "\u00c5", "\u0445", "\u0103", "\u0219", "\u021b"):
        assert must in out, must
    return out


ORDINARY = _ordinary_sample()
ACCEPTED_VALUES = [f"/tmp/bank-{c}-x" for c in ORDINARY] + [
    "/tmp/a#b", "/tmp/$HOME/x", "/tmp/a$b", "/tmp/a b", "/tmp/x=y",
    "C:\\Users\\Dr\u0103go\u0219\\bank", "/tmp/a'b", '/tmp/a"b', "/tmp/\U0001F600",
]
REFUSED_VALUES = (
    REVIEW_ROWS + LEGACY_ROWS
    + [f"{c}{T}/x" for c in ISSPACE] + [f"{T}/x{c}" for c in ISSPACE]
    + ["", f"{T}/a\x00b", f"{T}/\u0378"]
)


def _verdict(value, newline="\n", key=KEY):
    return epc.check_value(key, value, newline)


class ReviewRowsRefused(unittest.TestCase):
    def test_review_rows_refused_both_line_endings(self):
        for nl in NEWLINES:
            for v in REVIEW_ROWS:
                with self.subTest(v=ascii(v), nl=ascii(nl)):
                    self.assertIsNotNone(_verdict(v, nl))

    def test_review_rows_refused_by_the_reader_models_alone(self):
        # Semantics item 5: the transcriptions produce these naturally.
        for nl in NEWLINES:
            for v in REVIEW_ROWS:
                with self.subTest(v=ascii(v), nl=ascii(nl)):
                    self.assertIsNotNone(epc.readers_disagree(KEY, v, nl))

    def test_isspace_at_either_end_refused(self):
        self.assertGreater(len(ISSPACE), 20)
        for nl in NEWLINES:
            for c in ISSPACE:
                for v in (f"{c}{T}/x", f"{T}/x{c}"):
                    with self.subTest(c=f"U+{ord(c):04X}", v=ascii(v), nl=ascii(nl)):
                        self.assertIsNotNone(_verdict(v, nl))
                        self.assertIsNotNone(epc.readers_disagree(KEY, v, nl))

    def test_mid_path_breaker_reason_names_code_point_and_index(self):
        r = _verdict("/tmp/a\u2028b")
        self.assertEqual(
            r, "contains U+2028 LINE SEPARATOR at index 6; "
               "Mnemosyne's reader splits lines there")
        r = _verdict("/tmp/a\x1cb")
        self.assertIn("U+001C", r)
        self.assertIn("at index 6", r)

    def test_nbsp_before_hash_reason(self):
        r = _verdict(f"{T}/a\u00a0#b")
        self.assertIn("U+00A0 NO-BREAK SPACE at index 8", r)
        self.assertIn("'#'", r)

    def test_trailing_reasons(self):
        self.assertIn("ends with U+00A0 NO-BREAK SPACE at index 11",
                      _verdict(f"{T}/bank\u00a0"))
        self.assertIn("ends with U+001F", _verdict(f"{T}/bank\x1f"))

    def test_legacy_refusals_kept(self):
        for nl in NEWLINES:
            for v in LEGACY_ROWS:
                with self.subTest(v=v, nl=ascii(nl)):
                    self.assertIsNotNone(_verdict(v, nl))

    def test_legacy_refusals_mostly_natural(self):
        # Quotes, ${VAR}, whitespace+# and CR/LF come out of the reader
        # models; only the unclosed '${' needs the explicit rule.
        for v in LEGACY_ROWS:
            with self.subTest(v=v):
                natural = epc.readers_disagree(KEY, v, "\n")
                if v == f"{T}/a${{b":
                    self.assertIsNone(natural)
                    self.assertIn("${", _verdict(v))
                else:
                    self.assertIsNotNone(natural)

    def test_reasons_are_one_ascii_line(self):
        for nl in NEWLINES:
            for v in REFUSED_VALUES:
                r = _verdict(v, nl)
                with self.subTest(v=ascii(v)):
                    self.assertIsNotNone(r)
                    self.assertTrue(r.isascii(), r)
                    self.assertNotIn("\n", r)
                    self.assertNotIn("\r", r)


class EncodingRefused(unittest.TestCase):
    def test_invalid_utf8_posix(self):
        cases = {
            b"/tmp\xffx": "invalid UTF-8 byte 0xFF at index 4",
            b"/tmp/\xc3": "invalid UTF-8 byte 0xC3 at index 5",
            b"/t\xed\xa0\x80": "invalid UTF-8 byte 0xED at index 2",  # encoded surrogate
            b"/t\xc0\xaf": "invalid UTF-8 byte 0xC0 at index 2",  # overlong
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(epc.check_posix_bytes(KEY, raw), want)

    def test_lone_surrogate_windows_logic(self):
        for s in ("\ud800", "\udfff", "\udc85"):
            v = f"C:\\t\\a{s}b"
            r = epc.check_value(KEY, v, "\r\n")
            self.assertIsNotNone(r)
            self.assertIn(f"U+{ord(s):04X}", r)
            self.assertIn("at index 6", r)
            self.assertTrue(r.isascii())

    def test_valid_supplementary_char_windows_logic_accepted(self):
        self.assertIsNone(epc.check_value(KEY, "C:\\t\\\U0001F600", "\r\n"))

    def test_nul_refused(self):
        self.assertIn("U+0000", _verdict(f"{T}/a\x00b"))

    def test_unassigned_refused_fail_closed(self):
        self.assertIn("U+0378", _verdict(f"{T}/\u0378"))

    def test_empty_refused(self):
        self.assertEqual(_verdict(""), "is empty")


class OrdinaryAccepted(unittest.TestCase):
    def test_at_least_1000_ordinary_code_points(self):
        self.assertGreaterEqual(len(ORDINARY), 1000)

    def test_ordinary_accepted_mid_path_both_line_endings(self):
        for nl in NEWLINES:
            for v in ACCEPTED_VALUES:
                with self.subTest(v=ascii(v), nl=ascii(nl)):
                    self.assertIsNone(_verdict(v, nl))

    def test_ordinary_accepted_as_posix_bytes(self):
        for v in ACCEPTED_VALUES:
            with self.subTest(v=ascii(v)):
                self.assertIsNone(epc.check_posix_bytes(KEY, v.encode("utf-8")))

    def test_0x85_continuation_bytes_not_refused(self):
        # The reverted bash attempt refused these: UTF-8 has a 0x85 byte.
        for c in ("\u0105", "\u00c5", "\u0445"):
            self.assertIn(b"\x85", c.encode("utf-8"))
            self.assertIsNone(epc.check_posix_bytes(KEY, f"/tmp/b-{c}-x".encode()))

    def test_credential_key_strips_non_ascii(self):
        # Hermes _sanitize_loaded_credentials strips non-ASCII for *_KEY etc.
        self.assertIsNotNone(epc.check_value("FOO_TOKEN", "/tmp/\u0105", "\n"))
        self.assertIsNone(epc.check_value("FOO_TOKEN", "/tmp/a", "\n"))
        self.assertIsNone(epc.check_value(KEY, "/tmp/\u0105", "\n"))


class WriteModel(unittest.TestCase):
    def test_unix_and_windows_bytes(self):
        self.assertEqual(epc.write_env_file(["K=v\u0103"], "\n"),
                         "K=v\u0103\n".encode())
        self.assertEqual(epc.write_env_file(["K=v", "L=w"], "\r\n"),
                         b"K=v\r\nL=w\r\n")

    def test_windows_lone_surrogate_written_as_replacement(self):
        # UTF8Encoding($false) is non-throwing: a lone surrogate is U+FFFD.
        self.assertEqual(epc.write_env_file(["K=\ud800"], "\r\n"),
                         b"K=\xef\xbf\xbd\r\n")


# --- subprocess: exactly as the installers call it ---------------------------
LOCALES = {
    "env -i": {},
    "LC_ALL=C": {"LC_ALL": "C"},
    "LC_ALL=C.UTF-8": {"LC_ALL": "C.UTF-8"},
    "PYTHONUTF8=0 LC_ALL=C": {"LC_ALL": "C", "PYTHONUTF8": "0"},
}


def run_checker(argv, value_bytes=None, extra=None):
    env = {k.encode(): v.encode() for k, v in (extra or {}).items()}
    if value_bytes is not None:
        env[b"DEV_TEAM_ENV_CHECK_VALUE"] = value_bytes
    p = subprocess.run([sys.executable, str(SCRIPT), *argv], env=env,
                       capture_output=True, timeout=60)
    return p.returncode, p.stdout, p.stderr


@unittest.skipIf(os.name == "nt", "POSIX subprocess tests")
class Subprocess(unittest.TestCase):
    SUB_ACCEPT = [f"/tmp/bank-{c}-x" for c in
                  ("\u0105", "\u00c5", "\u0445", "\u0103", "\u0219", "\u021b",
                   "\u03a9", "\u05d0", "\u0628", "\u0915", "\u4e2d", "\uac00",
                   "\u00e9")] + ["/tmp/a#b", "/tmp/plain"]
    SUB_REFUSE = REVIEW_ROWS + LEGACY_ROWS + [f"{T}/x\u3000", f"\u2003{T}/x"]

    def test_exit_codes_and_output(self):
        rc, out, err = run_checker([KEY], b"/tmp/ok")
        self.assertEqual((rc, out), (0, b""))
        rc, out, _ = run_checker([KEY], "/tmp/a\u2028b".encode())
        self.assertEqual(rc, 1)
        self.assertEqual(out, b"contains U+2028 LINE SEPARATOR at index 6; "
                              b"Mnemosyne's reader splits lines there\n")
        rc, out, _ = run_checker([KEY], b"/tmp\xffx")
        self.assertEqual((rc, out), (1, b"invalid UTF-8 byte 0xFF at index 4\n"))

    def test_usage_errors_exit_2(self):
        for argv in ([], ["a", "b"], ["lower"], ["1ABC"], ["A-B"], ["\u0105"]):
            with self.subTest(argv=argv):
                self.assertEqual(run_checker(argv, b"/tmp/ok")[0], 2)
        self.assertEqual(run_checker([KEY], None)[0], 2)  # env var missing

    def test_locale_independent_verdicts(self):
        cases = [(v.encode("utf-8"), 0) for v in self.SUB_ACCEPT]
        cases += [(v.encode("utf-8"), 1) for v in self.SUB_REFUSE]
        cases += [(b"/tmp\xffx", 1), (b"/t\xed\xa0\x80", 1)]
        for raw, want in cases:
            seen = {}
            for name, extra in LOCALES.items():
                rc, out, err = run_checker([KEY], raw, extra)
                seen[name] = (rc, out)
                with self.subTest(raw=raw, locale=name):
                    self.assertEqual(rc, want, (out, err))
                    self.assertEqual(err, b"")
                    out.decode("ascii")
            self.assertEqual(len(set(seen.values())), 1, (raw, seen))

    def test_literal_env_i(self):
        env_bin = shutil.which("env")
        if not env_bin:
            self.skipTest("no env(1)")
        for raw, want in ((b"/tmp/bank-\xc4\x85-x", 0), ("/tmp/a\u2028b".encode(), 1)):
            p = subprocess.run(
                [env_bin, "-i", b"DEV_TEAM_ENV_CHECK_VALUE=" + raw,
                 sys.executable, str(SCRIPT), KEY],
                capture_output=True, timeout=60)
            self.assertEqual(p.returncode, want, p)


# --- differential against the real readers -----------------------------------
HELPER = r'''
import base64, json, os, sys, tempfile
from pathlib import Path
req = json.load(open(sys.argv[1], encoding="utf-8"))
tmp = Path(req["tmp"])
out = {"python": sys.executable}
cases = req["cases"]
if req.get("hermes_agent"):
    sys.path.insert(0, req["hermes_agent"])
    import dotenv
    from dotenv.parser import parse_stream
    from hermes_cli import env_loader
    out["dotenv_version"] = getattr(dotenv, "__version__", None) or __import__("dotenv.version").version.__version__
    out["dotenv_file"] = dotenv.__file__
    out["env_loader_file"] = env_loader.__file__
    res = []
    for i, c in enumerate(cases):
        p = tmp / f"h{i}.env"
        p.write_bytes(base64.b64decode(c))
        def read():
            a = env_loader._parse_dotenv_file(p)
            return {"assignments": a,
                    "resolved": env_loader._resolve_dotenv_assignments(a, {}, override=True)}
        before = read()
        env_loader._sanitize_env_file_if_needed(p)
        after_bytes = p.read_bytes()
        after = read()
        res.append({"orig": before, "sanitized_bytes": base64.b64encode(after_bytes).decode(),
                    "sanitized": after})
    out["hermes"] = res
if req.get("mnemosyne"):
    from mnemosyne import mcp_server
    import mnemosyne
    out["mnemosyne_file"] = mcp_server.__file__
    out["mnemosyne_version"] = getattr(mnemosyne, "__version__", None)
    res = []
    for i, c in enumerate(cases):
        p = tmp / f"m{i}.env"
        p.write_bytes(base64.b64decode(c))
        snap = dict(os.environ)
        for k in list(os.environ):
            if k not in ("HOME", "HERMES_HOME", "PATH"):
                del os.environ[k]
        base = dict(os.environ)
        loaded = mcp_server._load_dotenv(str(p))
        got = {k: v for k, v in os.environ.items() if k not in base}
        os.environ.clear(); os.environ.update(snap)
        res.append({"loaded": loaded is not None, "env": got})
    out["mnemosyne"] = res
json.dump(out, open(sys.argv[2], "w", encoding="utf-8"))
'''


def _find_hermes():
    agent = os.environ.get("DEV_TEAM_ENV_CHECK_HERMES_AGENT",
                           os.path.expanduser("~/.hermes/hermes-agent"))
    if not os.path.isfile(os.path.join(agent, "hermes_cli", "env_loader.py")):
        return None, None
    cands = []
    if os.environ.get("DEV_TEAM_ENV_CHECK_HERMES_PYTHON"):
        cands.append(os.environ["DEV_TEAM_ENV_CHECK_HERMES_PYTHON"])
    cands += sorted(glob.glob(os.path.expanduser(
        "~/.hermes/installs/*/environments/*/venv/bin/python")))
    for py in cands:
        if glob.glob(os.path.join(os.path.dirname(os.path.dirname(py)),
                                  "lib", "python3*", "site-packages", "dotenv")):
            return py, agent
    return None, None


def _find_mnemosyne():
    cands = []
    if os.environ.get("DEV_TEAM_ENV_CHECK_MNEMOSYNE_PYTHON"):
        cands.append(os.environ["DEV_TEAM_ENV_CHECK_MNEMOSYNE_PYTHON"])
    cands.append(os.path.expanduser("~/.mnemosyne-venv/bin/python"))
    for py in cands:
        if os.path.isfile(py) and glob.glob(os.path.join(
                os.path.dirname(os.path.dirname(py)), "lib", "python3*",
                "site-packages", "mnemosyne", "mcp_server.py")):
            return py
    return None


def _run_helper(py, values_and_nl, hermes_agent=None, mnemosyne=False):
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        for sub in ("home", "hermes", "mnemo", "mnemo/data", "files"):
            (tdp / sub).mkdir(parents=True, exist_ok=True)
        contents = [base64.b64encode(epc.build_content(KEY, v, nl)).decode()
                    for v, nl in values_and_nl]
        (tdp / "req.json").write_text(json.dumps({
            "tmp": str(tdp / "files"), "cases": contents,
            "hermes_agent": hermes_agent, "mnemosyne": mnemosyne}), encoding="utf-8")
        (tdp / "helper.py").write_text(HELPER, encoding="utf-8")
        env = {"HOME": str(tdp / "home"), "HERMES_HOME": str(tdp / "hermes"),
               "MNEMOSYNE_HOME": str(tdp / "mnemo"),
               "MNEMOSYNE_DATA_DIR": str(tdp / "mnemo" / "data"),
               "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C.UTF-8"}
        p = subprocess.run([py, str(tdp / "helper.py"), str(tdp / "req.json"),
                            str(tdp / "out.json")], env=env, capture_output=True,
                           timeout=600, cwd=str(tdp / "files"))
        if p.returncode != 0:
            raise AssertionError(f"helper failed under {py}: {p.stderr.decode(errors='replace')[-3000:]}")
        return json.loads((tdp / "out.json").read_text(encoding="utf-8"))


DIFF_SAMPLE = [(v, nl) for nl in NEWLINES
               for v in ACCEPTED_VALUES + [x for x in REFUSED_VALUES if "\x00" not in x]]


class Differential(unittest.TestCase):
    """Transcription vs REAL reader, value for value, on the full sample."""

    report: dict = {}

    @classmethod
    def tearDownClass(cls):
        if cls.report:
            sys.stderr.write("\nDIFFERENTIAL " + json.dumps(cls.report, sort_keys=True) + "\n")

    def test_hermes_dotenv_real_vs_transcription(self):
        py, agent = _find_hermes()
        if not py:
            self.skipTest("no Hermes venv with python-dotenv + hermes_cli found")
        out = _run_helper(py, DIFF_SAMPLE, hermes_agent=agent)
        mism, rewrites = [], 0
        for (v, nl), real in zip(DIFF_SAMPLE, out["hermes"]):
            content = epc.build_content(KEY, v, nl)
            rewrites += base64.b64decode(real["sanitized_bytes"]) != content
            # parse + ${} resolve, on the file as written
            m_orig = epc.hermes_parse_resolve(content, lookup={})
            if [list(x) for x in m_orig[0]] != real["orig"]["assignments"] \
                    or m_orig[1] != real["orig"]["resolved"]:
                mism.append(("orig", ascii(v), ascii(nl), m_orig, real["orig"]))
            # sanitize rewrite (real one ran on Linux -> os.linesep == "\n")
            m_san = epc.hermes_sanitize(content, rewrite_newline="\n")
            san_bytes = content if m_san is None else m_san
            if san_bytes != base64.b64decode(real["sanitized_bytes"]):
                mism.append(("sanitize", ascii(v), ascii(nl)))
            m_after = epc.hermes_parse_resolve(san_bytes, lookup={})
            if m_after[1] != real["sanitized"]["resolved"]:
                mism.append(("after", ascii(v), ascii(nl)))
        Differential.report["hermes"] = {
            "python": out["python"], "dotenv": out["dotenv_file"],
            "dotenv_version": out["dotenv_version"],
            "env_loader": out["env_loader_file"], "cases": len(DIFF_SAMPLE),
            "real_sanitizer_rewrites": rewrites, "mismatches": len(mism)}
        self.assertEqual(mism, [])
        # The real sanitiser must actually have run (it returns early when
        # hermes_cli.config cannot be imported): the trailing-whitespace
        # samples always make it rewrite.
        self.assertGreater(rewrites, 0)

    def test_mnemosyne_real_vs_transcription(self):
        py = _find_mnemosyne()
        if not py:
            self.skipTest("no venv with mnemosyne found")
        out = _run_helper(py, DIFF_SAMPLE, mnemosyne=True)
        mism = []
        for (v, nl), real in zip(DIFF_SAMPLE, out["mnemosyne"]):
            content = epc.build_content(KEY, v, nl)
            try:
                model = epc.mnemosyne_read(content)
                ok = True
            except epc.ReaderFailed:
                model, ok = {}, False
            if ok != real["loaded"] or (ok and model != real["env"]):
                mism.append((ascii(v), ascii(nl), model, real))
        Differential.report["mnemosyne"] = {
            "python": out["python"], "file": out["mnemosyne_file"],
            "version": out["mnemosyne_version"], "cases": len(DIFF_SAMPLE),
            "mismatches": len(mism)}
        self.assertEqual(mism, [])

    def test_verdicts_match_real_readers(self):
        """Accepted by the checker <=> both real readers return exactly the value."""
        hpy, agent = _find_hermes()
        mpy = _find_mnemosyne()
        if not (hpy and mpy):
            self.skipTest("need both real readers")
        sample = [(v, nl) for v, nl in DIFF_SAMPLE]
        h = _run_helper(hpy, sample, hermes_agent=agent)["hermes"]
        m = _run_helper(mpy, sample, mnemosyne=True)["mnemosyne"]
        disagree, counts = [], {"accepted": 0, "refused": 0}
        for (v, nl), hr, mr in zip(sample, h, m):
            want = {KEY: v, **epc.sentinels(KEY)}
            real_ok = (hr["orig"]["resolved"] == want
                       and hr["sanitized"]["resolved"] == want
                       and mr["loaded"] and mr["env"] == want)
            ours_ok = epc.check_value(KEY, v, nl) is None
            counts["accepted" if ours_ok else "refused"] += 1
            # Fail-closed rules (unassigned, unclosed '${', empty) may refuse
            # what today's real readers accept; never the reverse.
            if ours_ok and not real_ok:
                disagree.append(("checker accepts, real readers differ", ascii(v), ascii(nl)))
            if real_ok and not ours_ok and not epc.is_fail_closed_only(KEY, v, nl):
                disagree.append(("checker refuses, real readers agree", ascii(v), ascii(nl)))
        Differential.report["verdicts"] = {"cases": len(sample), **counts,
                                           "disagreements": len(disagree)}
        self.assertEqual(disagree, [])


if __name__ == "__main__":
    unittest.main()
