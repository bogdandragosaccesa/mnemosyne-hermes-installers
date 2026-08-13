# Troubleshooting

## Memories are stored but semantic recall never matches

The single most common failure, and it is silent. `hermes memory status` reports
`available`, storing works, keyword recall works — but nothing is ever found by meaning.

The cause is that the embedding stack cannot load inside Hermes' interpreter, so memories
are written without vectors. Since v1.0.0 the installer checks for this and prints:

```text
! Hermes cannot import numpy/onnxruntime from the Mnemosyne environment.
!   Memories will be stored without embeddings; semantic recall will not match.
```

### Confirming memories get embeddings

Ask the database directly. Every memory should have a `memory_embeddings` row:

```bash
~/.mnemosyne-venv/bin/python - <<'PY'
import sqlite3, os
db = os.path.expanduser('~/.hermes/mnemosyne/data/mnemosyne.db')
c = sqlite3.connect(db)
emb = {r[0] for r in c.execute('SELECT memory_id FROM memory_embeddings')}
for mid, content in c.execute('SELECT id, substr(content,1,50) FROM working_memory'):
    print('%-18s embedded=%-5s | %s' % (mid, mid in emb, content))
PY
```

`embedded=False` on memories written through Hermes means the embedding stack is not
loading. On Windows the database is at `%LOCALAPPDATA%\hermes\mnemosyne\data\`.

A recall result with `"dense_score": 0.0000` on every query is the same symptom seen from
the other side.

### Python version mismatch (Linux/macOS)

Hermes imports Mnemosyne in-process, so the two must run the same Python minor version —
see [How it works](how-it-works.md#why-the-python-version-has-to-match). Check:

```bash
~/.hermes/hermes-agent/venv/bin/python --version
~/.mnemosyne-venv/bin/python --version
```

If they differ, re-run the installer; it rebuilds the environment against Hermes'
version. Versions before 1.0.0 could produce this silently when `uv` was not on `PATH`.

### `onnxruntime` DLL load failed on Windows

```text
ImportError: DLL load failed while importing onnxruntime_pybind11_state:
A dynamic link library (DLL) initialization routine failed.
```

The Visual C++ runtime is too old. `onnxruntime` needs a recent x64 redistributable, and
some Windows Server images ship 14.29 (2019-era), which is not enough. Check and fix:

```powershell
(Get-Item "$env:WINDIR\System32\vcruntime140.dll").VersionInfo.FileVersion
winget install --id "Microsoft.VCRedist.2015+.x64" --exact --source winget
```

If winget reports `The server certificate did not match any of the expected values`, that
is the `msstore` source failing — `--source winget` avoids it.

## The installer hangs and never finishes

On Linux/macOS before v1.0.0, `hermes gateway restart` would fall back to running the
gateway in the foreground when no service was registered, and never return. Fixed by
registering the service first. If you hit it on an older copy:

```bash
hermes gateway install     # then re-run the installer
```

## `mnemosyne-hermes install` fails with "already exists"

```text
error: <HERMES_HOME>/plugins/mnemosyne already exists. Re-run with --force to replace it.
```

Fixed in v1.0.0 by always passing `--force`. On an older copy, remove the directory by
hand or pass `--force` yourself.

## Uninstall finished with leftovers

The uninstaller exits non-zero and lists what survived. Usually something still holds the
files open. Close every shell and editor using those paths and re-run. On Windows the
script already stops file-locking processes and retries; unlinking an open file always
succeeds on Unix, so leftovers there point at a permissions problem instead.

## `hermes` is not found after installing

The installer adds it to your shell rc file or User environment variables, neither of
which affects the shell you are already in. Open a new terminal, or:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## pip fails with "No such file or directory" on Windows

`onnxruntime` ships module paths around 120 characters deep. With `LongPathsEnabled=0` a
deeply nested virtual environment makes pip fail partway through with a misleading error.
The installer warns when the venv path is long. Set `MNEMOSYNE_VENV` to something shorter
and re-run:

```powershell
$env:MNEMOSYNE_VENV = 'C:\mnemosyne-venv'
.\install-mnemosyne-hermes-windows.ps1
```

## Config encoding warning on Windows

```text
Failed to inspect legacy provider defaults: 'utf-8' codec can't decode byte 0x97
```

Mnemosyne writes `config.yaml` in the ANSI codepage but reads it back as UTF-8, so the
template's em dash breaks every command. The installer re-encodes the file as UTF-8,
which preserves the content and silences the warning.

## `--no-embeddings` does nothing

`mnemosyne-hermes` 0.5.0 declares `mnemosyne-memory[embeddings]` as a hard dependency, so
pip installs fastembed / onnxruntime / sqlite-vec either way. The flag is kept for
parity between the two scripts; the Windows script warns when you pass it.

## Getting more detail

```bash
./uninstall-mnemosyne-hermes-unix.sh --dry-run   # exactly what would be removed
hermes memory status                             # registration state
~/.mnemosyne-venv/bin/mnemosyne stats            # database counts and path
~/.mnemosyne-venv/bin/mnemosyne doctor           # read-only health report
journalctl --user -u hermes-gateway -f           # gateway logs on Linux
```
