#!/usr/bin/env python3
"""Decide whether a Mnemosyne pin value is safe to write as an unquoted
``KEY=value`` line in a Hermes profile ``.env``.

Both installers (install-dev-team-unix.sh, install-dev-team-windows.ps1) call
this before they pin a value. "Safe" means: BOTH readers of that file --
Hermes's loader (python-dotenv + hermes_cli/env_loader.py post-processing)
and Mnemosyne's own ``_load_dotenv`` -- read back exactly ``{KEY: value}``.

Interface (fixed; the installers rely on it):
  * value: environment variable DEV_TEAM_ENV_CHECK_VALUE -- never argv/stdin.
      POSIX:   os.environb[b"DEV_TEAM_ENV_CHECK_VALUE"] (exact bytes, any locale)
      Windows: os.environ["DEV_TEAM_ENV_CHECK_VALUE"] (UTF-16 str, may hold
               lone surrogates)
  * argv:  env_pin_check.py <KEY>, KEY matching [A-Z_][A-Z0-9_]* (ASCII).
  * exit:  0 safe, no output.
           1 unsafe; ONE ASCII line on stdout naming the offending code point
             as U+XXXX (or byte as 0xXX) and its index. Indexes are code-point
             indexes into the value, or byte indexes for invalid UTF-8.
           2 usage or internal error (message on stderr).

Python 3 standard library only. Nothing below imports python-dotenv, Hermes
or Mnemosyne: the readers are TRANSCRIBED, function by function, each with a
``TRANSCRIPTION of <path>:<lines>`` citation. tests/test_env_pin_check.py
diffs every transcription against the real reader when one is installed.

Upstream versions transcribed:
  * python-dotenv 1.2.2 (dotenv/version.py:1), e.g. a venv's
    lib/python3.14/site-packages/dotenv/ -- any install of that exact
    release reads identically.
  * Hermes hermes-agent commit bf0203a794a07aac3ff764972ab9087baa4e4f4e
    (2026-10-22; pyproject version 0.0.0), ~/.hermes/hermes-agent/
  * mnemosyne: verified line-for-line identical against both
    mnemosyne_memory-4.0.0b6 and the installed mnemosyne_memory-4.0.0
    (__version__ "4.0.0"), mnemosyne/mcp_server.py.
  * Hermes agent/secret_scope.py:_parse_env_text (348-361, plus the
    _strip_inline_comment/_parse_env_value helpers it calls, 271-345) --
    a third Hermes .env reader (behind profile secret scopes), not
    transcribed below but confirmed by the differential fuzz (section 3
    of the PR #10 review) to agree with this checker on every accepted
    value.

Fail closed: where a real reader's result depends on something this checker
cannot know (the process environment for ``${VAR}``, the Unicode database of
another Python version), the value is refused.
"""
from __future__ import annotations

import codecs
import io
import os
import re
import sys
import unicodedata

ENV_VAR = "DEV_TEAM_ENV_CHECK_VALUE"
KEY_RE = re.compile(r"[A-Z_][A-Z0-9_]*", re.ASCII)


class ReaderFailed(Exception):
    """The real reader would raise / give up on this file."""


class EnvDependent(Exception):
    """The real reader's result depends on the process environment."""


# =============================================================================
# Write model: the exact bytes the installers put on disk.
# =============================================================================
def write_env_file(lines, newline):
    """Bytes of a .env written as ``lines``, each followed by ``newline``.

    Unix (newline="\\n"): install-dev-team-unix.sh:667-686 set_env_var,
      ``printf '%s\\n' "${out_lines[@]}"`` (:684) -- every line, then LF,
      bytes verbatim (the value was already strictly UTF-8-decoded, so
      re-encoding it reproduces those bytes).
    Windows (newline="\\r\\n"): install-dev-team-windows.ps1:1058-1071
      Set-EnvVarPin, ``[IO.File]::WriteAllLines($envFile, $lines,
      UTF8Encoding($false))``. WriteAllLines writes each line with
      TextWriter.WriteLine, i.e. line + Environment.NewLine (CRLF on
      Windows); UTF8Encoding($false) writes no BOM and, being the
      non-throwing variant, encodes a lone surrogate as U+FFFD (EF BF BD).
    """
    text = "".join(line + newline for line in lines)
    text = re.sub("[\ud800-\udfff]", "\ufffd", text)
    return text.encode("utf-8")


def sentinels(key):
    """Neighbour lines around the pin, so a value that leaks into (or
    swallows) the next/previous line is caught too."""
    before, after = "PINCHECK_BEFORE", "PINCHECK_AFTER"
    if key in (before, after):
        before, after = before + "_X", after + "_X"
    return {before: "1", after: "2"}


def build_content(key, value, newline):
    """``BEFORE=1``, ``KEY=value``, ``AFTER=2`` -- the pin amid other lines,
    as set_env_var / Set-EnvVarPin leave it in an existing file."""
    (b, bv), (a, av) = sentinels(key).items()
    return write_env_file(["%s=%s" % (b, bv), "%s=%s" % (key, value),
                           "%s=%s" % (a, av)], newline)


def build_single(key, value, newline):
    """``KEY=value`` alone -- the file the installers create when none exists."""
    return write_env_file(["%s=%s" % (key, value)], newline)


# =============================================================================
# Hermes reader, part 1: python-dotenv's parser.
# TRANSCRIPTION (not an import) of python-dotenv 1.2.2,
#   dotenv/parser.py:14-182 (venv path in the module docstring).
# =============================================================================
def _make_regex(string, extra_flags=0):  # parser.py:14-15
    return re.compile(string, re.UNICODE | extra_flags)


# parser.py:18-32 -- verbatim.
_newline = _make_regex(r"(\r\n|\n|\r)")
_multiline_whitespace = _make_regex(r"\s*", extra_flags=re.MULTILINE)
_whitespace = _make_regex(r"[^\S\r\n]*")
_export = _make_regex(r"(?:export[^\S\r\n]+)?")
_single_quoted_key = _make_regex(r"'([^']+)'")
_unquoted_key = _make_regex(r"([^=\#\s]+)")
_equal_sign = _make_regex(r"(=[^\S\r\n]*)")
_single_quoted_value = _make_regex(r"'((?:\\'|[^'])*)'")
_double_quoted_value = _make_regex(r'"((?:\\"|[^"])*)"')
_unquoted_value = _make_regex(r"([^\r\n]*)")
_comment = _make_regex(r"(?:[^\S\r\n]*#[^\r\n]*)?")
_end_of_line = _make_regex(r"[^\S\r\n]*(?:\r\n|\n|\r|$)")
_rest_of_line = _make_regex(r"[^\r\n]*(?:\r|\n|\r\n)?")
_double_quote_escapes = _make_regex(r"\\[\\'\"abfnrtv]")
_single_quote_escapes = _make_regex(r"\\[\\']")


class _ParseError(Exception):  # parser.py:65-66 (Error)
    pass


class _Position:  # parser.py:47-62
    def __init__(self, chars, line):
        self.chars = chars
        self.line = line

    @classmethod
    def start(cls):
        return cls(chars=0, line=1)

    def set(self, other):
        self.chars = other.chars
        self.line = other.line

    def advance(self, string):
        self.chars += len(string)
        self.line += len(re.findall(_newline, string))


class _Reader:  # parser.py:69-102; takes the str stream.read() would return
    def __init__(self, string):
        self.string = string
        self.position = _Position.start()
        self.mark = _Position.start()

    def has_next(self):
        return self.position.chars < len(self.string)

    def set_mark(self):
        self.mark.set(self.position)

    def get_marked(self):
        return self.string[self.mark.chars:self.position.chars]

    def peek(self, count):
        return self.string[self.position.chars:self.position.chars + count]

    def read(self, count):
        result = self.string[self.position.chars:self.position.chars + count]
        if len(result) < count:
            raise _ParseError("read: End of string")
        self.position.advance(result)
        return result

    def read_regex(self, regex):
        match = regex.match(self.string, self.position.chars)
        if match is None:
            raise _ParseError("read_regex: Pattern not found")
        self.position.advance(self.string[match.start():match.end()])
        return match.groups()


def _decode_escapes(regex, string):  # parser.py:105-109
    def decode_match(match):
        return codecs.decode(match.group(0), "unicode-escape")

    return regex.sub(decode_match, string)


def _parse_key(reader):  # parser.py:112-120
    char = reader.peek(1)
    if char == "#":
        return None
    elif char == "'":
        (key,) = reader.read_regex(_single_quoted_key)
    else:
        (key,) = reader.read_regex(_unquoted_key)
    return key


def _parse_unquoted_value(reader):  # parser.py:123-125
    (part,) = reader.read_regex(_unquoted_value)
    return re.sub(r"\s+#.*", "", part).rstrip()


def _parse_value(reader):  # parser.py:128-139
    char = reader.peek(1)
    if char == "'":
        (value,) = reader.read_regex(_single_quoted_value)
        return _decode_escapes(_single_quote_escapes, value)
    elif char == '"':
        (value,) = reader.read_regex(_double_quoted_value)
        return _decode_escapes(_double_quote_escapes, value)
    elif char in ("", "\n", "\r"):
        return ""
    else:
        return _parse_unquoted_value(reader)


def _parse_binding(reader):  # parser.py:142-176 -> (key, value, error)
    reader.set_mark()
    try:
        reader.read_regex(_multiline_whitespace)
        if not reader.has_next():
            return (None, None, False)
        reader.read_regex(_export)
        key = _parse_key(reader)
        reader.read_regex(_whitespace)
        if reader.peek(1) == "=":
            reader.read_regex(_equal_sign)
            value = _parse_value(reader)
        else:
            value = None
        reader.read_regex(_comment)
        reader.read_regex(_end_of_line)
        return (key, value, False)
    except _ParseError:
        reader.read_regex(_rest_of_line)
        return (None, None, True)


def dotenv_parse(text):
    """parser.py:179-182 parse_stream + dotenv/main.py:91-95 DotEnv.parse
    (yield (key, value) for every binding whose key is not None; main.py:32-39
    with_warn_for_invalid_lines only logs)."""
    reader = _Reader(text)
    while reader.has_next():
        key, value, _error = _parse_binding(reader)
        if key is not None:
            yield key, value


# =============================================================================
# Hermes reader, part 2: ${VAR} expansion.
# TRANSCRIPTION of python-dotenv 1.2.2 dotenv/variables.py:5-15 (_posix_variable),
#   :64-67 (Variable.resolve), :70-86 (parse_variables).
# =============================================================================
_posix_variable = re.compile(
    r"""
    \$\{
        (?P<name>[^\}:]*)
        (?::-
            (?P<default>[^\}]*)
        )?
    \}
    """,
    re.VERBOSE,
)


def _parse_variables(value):
    """Yields ("lit", text) / ("var", name, default)."""
    cursor = 0
    for match in _posix_variable.finditer(value):
        (start, end) = match.span()
        if start > cursor:
            yield ("lit", value[cursor:start])
        yield ("var", match["name"], match["default"])
        cursor = end
    if cursor < len(value):
        yield ("lit", value[cursor:len(value)])


def _resolve_atom(atom, env):
    if atom[0] == "lit":
        return atom[1]
    _, name, default = atom
    default = default if default is not None else ""
    result = env.get(name, default)
    return result if result is not None else ""


# =============================================================================
# Hermes reader, part 3: hermes_cli/env_loader.py (+ config.py) post-processing.
# TRANSCRIPTION of Hermes commit bf0203a794a07aac3ff764972ab9087baa4e4f4e,
#   ~/.hermes/hermes-agent/hermes_cli/env_loader.py and config.py.
# =============================================================================
# env_loader.py:24
_CREDENTIAL_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_KEY")
# env_loader.py:84 -- .env cannot override these when the parent set them
# (env_loader.py:419-420): environment-dependent, so refused (fail closed).
_SPAWN_CREDENTIAL_KEYS = frozenset({"HERMES_DASHBOARD_SESSION_TOKEN"})


def hermes_parse_resolve(content, lookup=None, override=True):
    """env_loader.py:328-342 _parse_dotenv_file, then :358-372
    _resolve_dotenv_assignments.

    Returns (assignments, resolved). ``lookup`` is the environment ${VAR}
    resolves against; None means "unknown" and raises EnvDependent on the
    first ${...} reference (fail closed). The user .env is loaded with
    override=True (env_loader.py:560).
    """
    # :330-337 -- utf-8-sig, latin-1 fallback (fallback unreachable for
    # values this checker accepts; transcribed for the differential test).
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raw = content[len(codecs.BOM_UTF8):] if content.startswith(codecs.BOM_UTF8) else content
        text = raw.decode("latin-1")
    # :342 -- DotEnv(stream=io.StringIO(text), interpolate=False).parse();
    # StringIO's default newline="\n" does no translation, so the parser
    # sees ``text`` as is.
    assignments = list(dotenv_parse(text))
    resolved = {}
    for name, value in assignments:  # :366-372
        if value is not None:
            atoms = list(_parse_variables(value))
            if lookup is None and any(a[0] == "var" for a in atoms):
                raise EnvDependent(name)
            env = lookup or {}
            env_view = {**env, **resolved} if override else {**resolved, **env}
            value = "".join(_resolve_atom(a, env_view) for a in atoms)
        resolved[name] = value
    return assignments, resolved


def _sanitize_env_lines(lines):
    """TRANSCRIPTION of hermes_cli/config.py:2556-2566 _sanitize_env_lines."""
    sanitized = []
    for line in lines:
        raw = line.rstrip("\r\n")
        stripped = raw.strip()
        sanitized.append((raw if not stripped or stripped.startswith("#") else stripped) + "\n")
    return sanitized


def hermes_sanitize(content, rewrite_newline):
    """env_loader.py:432-499 _sanitize_env_file_if_needed, run by
    load_hermes_dotenv (:554-555) BEFORE the parse. Returns the bytes Hermes
    rewrites the file to, or None when it leaves the file alone.

    ``rewrite_newline`` is what ``os.fdopen(fd, "w", encoding="utf-8")``
    (:487) turns "\\n" into: os.linesep, so "\\n" on POSIX, "\\r\\n" on Windows.
    """
    raw = content
    force_utf8_rewrite = False
    if raw.startswith(codecs.BOM_UTF32_LE) or raw.startswith(codecs.BOM_UTF32_BE):
        return None  # :450-458
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        try:  # :459-467
            with io.TextIOWrapper(io.BytesIO(raw), encoding="utf-16", newline=None) as f:
                original = f.readlines()
        except UnicodeDecodeError:
            return None
        force_utf8_rewrite = True
    else:
        # :471-478 -- open(path, encoding="utf-8-sig", errors="replace") is a
        # TextIOWrapper with universal newlines (newline=None): "\r\n" and a
        # lone "\r" become "\n", and readlines() splits on "\n" ONLY (not on
        # str.splitlines()'s extra boundaries).
        with io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8-sig",
                              errors="replace", newline=None) as f:
            original = f.readlines()
        if original and original[0].startswith("\ufffd"):
            return None
    stripped = [line.replace("\x00", "") for line in original]  # :482
    sanitized = _sanitize_env_lines(stripped)  # :483
    if sanitized != original or force_utf8_rewrite:  # :484-491
        buf = io.BytesIO()
        w = io.TextIOWrapper(buf, encoding="utf-8", newline=rewrite_newline)
        w.writelines(sanitized)
        w.flush()
        return buf.getvalue()
    return None


def _hermes_publish(resolved):
    """env_loader.py:413-429 _load_dotenv_with_fallback: what lands in
    os.environ, then :295-325 _sanitize_loaded_credentials."""
    out = {}
    for name, value in resolved.items():
        if value is None:  # :414
            continue
        if name in _SPAWN_CREDENTIAL_KEYS:  # :419-420, env-dependent
            raise EnvDependent(name)
        if "\x00" in value or "\x00" in name:  # os.environ[name] = value (:423)
            raise ReaderFailed("embedded null byte")
        if any(name.endswith(s) for s in _CREDENTIAL_SUFFIXES) and not _isascii(value):
            value = value.encode("ascii", errors="ignore").decode("ascii")  # :305-308
        out[name] = value
    return out


def hermes_read(content, rewrite_newline):
    """What Hermes ends up with in os.environ, for every file version it may
    parse: the file as the installer wrote it (when the sanitiser is a no-op,
    or unavailable -- env_loader.py:438-441 returns early on ImportError) and
    the sanitiser's rewrite (when it rewrites)."""
    versions = [content]
    rewritten = hermes_sanitize(content, rewrite_newline)
    if rewritten is not None:
        versions.append(rewritten)
    return [_hermes_publish(hermes_parse_resolve(c)[1]) for c in versions], versions


# =============================================================================
# Mnemosyne reader.
# TRANSCRIPTION of mnemosyne, verified line-for-line identical in both
# mnemosyne_memory-4.0.0b6 (as named by the PR #9 review) and the installed
# mnemosyne_memory-4.0.0, __version__ "4.0.0":
#   mnemosyne/mcp_server.py:793-842, _load_dotenv, line handling :820-841.
# =============================================================================
def mnemosyne_read(content):
    """Returns the {key: value} the reader puts in os.environ (keys not
    already set win first: ``k not in os.environ``, :833). Raises
    ReaderFailed where the real one hits its ``except Exception`` (:838) and
    moves on to the next candidate file."""
    try:
        # :823 -- Path.read_text(encoding="utf-8"): open() in text mode,
        # universal newlines, strict UTF-8, no BOM handling.
        with io.TextIOWrapper(io.BytesIO(content), encoding="utf-8", newline=None) as f:
            text = f.read()
        env = {}
        for line in text.splitlines():  # :823
            line = line.strip()  # :824
            if not line or line.startswith("#"):  # :825-826
                continue
            if line.startswith("export "):  # :827-828
                line = line[7:].strip()
            if "=" in line:  # :829-834
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k and k not in env:
                    if "\x00" in k or "\x00" in v:  # os.environ[k] = v raises
                        raise ValueError("embedded null byte")
                    env[k] = v
        return env
    except Exception as exc:  # :838-839
        raise ReaderFailed(str(exc))


# =============================================================================
# Verdict.
# =============================================================================
def _isascii(s):
    return all(ord(c) < 128 for c in s)


# unicodedata.name() has no name for C0/C1 controls; these are their Unicode
# name aliases.
_CONTROL_NAMES = {
    0x00: "NULL", 0x09: "CHARACTER TABULATION", 0x0A: "LINE FEED",
    0x0B: "LINE TABULATION", 0x0C: "FORM FEED", 0x0D: "CARRIAGE RETURN",
    0x1C: "INFORMATION SEPARATOR FOUR", 0x1D: "INFORMATION SEPARATOR THREE",
    0x1E: "INFORMATION SEPARATOR TWO", 0x1F: "INFORMATION SEPARATOR ONE",
    0x85: "NEXT LINE",
}


def _cp(c):
    o = ord(c)
    name = _CONTROL_NAMES.get(o) or unicodedata.name(c, "")
    return ("U+%04X %s" % (o, name)) if name else ("U+%04X" % o)


def _is_line_boundary(c):
    return len(("a" + c + "b").splitlines()) > 1


def _diagnose(key, value):
    """Explicit, precisely-worded rules. Every rule here is either implied by
    the reader models (and only gives a better message) or is a named
    fail-closed rule (see is_fail_closed_only / the test suite)."""
    if value == "":
        # Fail closed: a pin must name a directory. (On Windows an empty
        # environment variable cannot even be passed.)
        return "is empty"
    for i, c in enumerate(value):
        if 0xD800 <= ord(c) <= 0xDFFF:
            return ("contains lone surrogate U+%04X at index %d; it is not valid "
                    "UTF-8 (WriteAllLines would write U+FFFD instead)" % (ord(c), i))
    for i, c in enumerate(value):
        if c == "\x00":
            return "contains %s at index %d; an environment value cannot hold it" % (_cp(c), i)
        if c in "\r\n":
            return "contains %s at index %d; it ends the KEY=value line" % (_cp(c), i)
        if _is_line_boundary(c):
            return "contains %s at index %d; Mnemosyne's reader splits lines there" % (_cp(c), i)
    for i, c in enumerate(value):
        # Fail closed: a code point this Python's Unicode database does not
        # know may be whitespace / a line boundary to the Python the real
        # readers run on (Hermes and Mnemosyne run 3.14 here).
        if unicodedata.category(c) == "Cn":
            return ("contains U+%04X at index %d, unassigned in this Python's Unicode "
                    "%s; another Python's readers may treat it as whitespace or a line "
                    "break" % (ord(c), i, unicodedata.unidata_version))
    if value[0].isspace():
        return ("starts with %s at index 0; the .env readers strip leading "
                "whitespace" % _cp(value[0]))
    if value[-1].isspace():
        return ("ends with %s at index %d; the .env readers strip trailing "
                "whitespace" % (_cp(value[-1]), len(value) - 1))
    for i in range(len(value) - 1):
        if value[i].isspace() and value[i + 1] == "#":
            return ("contains %s at index %d followed by '#'; python-dotenv cuts the "
                    "value there as an inline comment" % (_cp(value[i]), i))
    if value[0] in "'\"":
        return ("starts with %s at index 0; python-dotenv parses it as a quoted value, "
                "Mnemosyne's reader strips it" % _cp(value[0]))
    if value[-1] in "'\"":
        return ("ends with %s at index %d; Mnemosyne's reader strips quote "
                "characters at the ends" % (_cp(value[-1]), len(value) - 1))
    i = value.find("${")
    if i >= 0:
        # Kept as an explicit rule: for an UNCLOSED '${' (no '}' after it)
        # python-dotenv's _posix_variable does not match, so the transcribed
        # readers agree and would accept it. The installers have always
        # refused any literal '${' (install-dev-team-unix.sh:481-488,
        # install-dev-team-windows.ps1:882), and one more character in a
        # later path would turn it into an expansion; refuse it outright.
        return ("contains a literal ${ at index %d; Hermes's loader expands ${VAR} "
                "from its environment, Mnemosyne's reader does not" % i)
    if key in _SPAWN_CREDENTIAL_KEYS:
        return ("KEY %s is a Hermes per-process credential; a .env value is ignored "
                "when the parent process set it" % key)
    for suffix in _CREDENTIAL_SUFFIXES:
        if key.endswith(suffix):
            for i, c in enumerate(value):
                if ord(c) > 127:
                    return ("contains non-ASCII %s at index %d; Hermes strips non-ASCII "
                            "from variables named *%s" % (_cp(c), i, suffix))
    return None


def _first_difference(want, got):
    if got is None:
        return "does not yield it at all"
    n = min(len(want), len(got))
    for i in range(n):
        if want[i] != got[i]:
            return "first difference at index %d (%s)" % (i, _cp(want[i]))
    return "first difference at index %d" % n


def readers_disagree(key, value, newline, rewrite_newline=None):
    """Reader models only (no explicit rule): None when Hermes's and
    Mnemosyne's transcribed readers both read back exactly the expected
    mapping, for the pin alone and amid other lines, on the file as written
    and as Hermes's sanitiser may rewrite it. Else an ASCII reason."""
    if rewrite_newline is None:
        rewrite_newline = newline
    for build, expected in ((build_single, {key: value}),
                            (build_content, dict(sentinels(key), **{key: value}))):
        content = build(key, value, newline)
        try:
            results, versions = hermes_read(content, rewrite_newline)
        except EnvDependent:
            return ("Hermes's loader would expand ${...} in it from the process "
                    "environment, which Mnemosyne's reader does not")
        except ReaderFailed:
            return "Hermes's loader cannot load it"
        for got in results:
            if got != expected:
                return "Hermes's loader (python-dotenv) reads it back differently; " + \
                    _first_difference(value, got.get(key))
        for c in versions:
            try:
                got = mnemosyne_read(c)
            except ReaderFailed:
                return "Mnemosyne's reader fails to load the file"
            if got != expected:
                return "Mnemosyne's reader reads it back differently; " + \
                    _first_difference(value, got.get(key))
    return None


def check_value(key, value, newline, rewrite_newline=None):
    """str-level verdict (the Windows path; also used by POSIX after the
    UTF-8 decode). None = safe, else a one-line ASCII reason."""
    reason = _diagnose(key, value) or readers_disagree(key, value, newline, rewrite_newline)
    if reason is not None and not _isascii(reason):  # never print non-ASCII
        reason = reason.encode("ascii", "backslashreplace").decode("ascii")
    return reason


def is_fail_closed_only(key, value, newline):
    """True when the value is refused only by a fail-closed rule while the
    reader models accept it (unassigned code point, unclosed '${', empty)."""
    return readers_disagree(key, value, newline) is None and check_value(key, value, newline) is not None


def check_posix_bytes(key, raw):
    """POSIX verdict on the exact environment bytes."""
    try:
        value = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as e:
        return "invalid UTF-8 byte 0x%02X at index %d" % (raw[e.start], e.start)
    return check_value(key, value, "\n")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or not KEY_RE.fullmatch(argv[0]):
        sys.stderr.write("usage: env_pin_check.py KEY  (KEY matches [A-Z_][A-Z0-9_]*; "
                         "the value is read from $%s)\n" % ENV_VAR)
        return 2
    key = argv[0]
    try:
        if os.name == "nt":
            value = os.environ.get(ENV_VAR)
            if value is None:
                sys.stderr.write("env_pin_check: %s is not set\n" % ENV_VAR)
                return 2
            reason = check_value(key, value, "\r\n")
        else:
            raw = os.environb.get(ENV_VAR.encode("ascii"))
            if raw is None:
                sys.stderr.write("env_pin_check: %s is not set\n" % ENV_VAR)
                return 2
            reason = check_posix_bytes(key, raw)
    except Exception as exc:  # never a silent pass: internal error -> 2
        sys.stderr.write("env_pin_check: internal error: %s\n" % ascii(exc))
        return 2
    if reason is None:
        return 0
    sys.stdout.write(reason + "\n")
    sys.stdout.flush()
    return 1


if __name__ == "__main__":
    sys.exit(main())
