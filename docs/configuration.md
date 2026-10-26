# Configuration

Mnemosyne needs no configuration to work — every setting has a default. This page covers
what matters when you are running it *under Hermes*, and what these installers set for
you. The exhaustive list is upstream:
[docs.mnemosyne.site/getting-started/configuration](https://docs.mnemosyne.site/getting-started/configuration).

## Three layers, in precedence order

1. **`config.yaml`** — Mnemosyne's own file.
2. **Environment variables** — everything named `MNEMOSYNE_*`.
3. **Defaults** — compiled into `mnemosyne/core/config.py`.

Separately, Hermes keeps provider settings under `memory.mnemosyne` in **its own**
`config.yaml`. Two different files; do not confuse them.

| File | Location |
| --- | --- |
| Hermes config | `<HERMES_HOME>/config.yaml` |
| Mnemosyne config | `<MNEMOSYNE_DATA_DIR>/config.yaml`, else `<HERMES_HOME>/mnemosyne/config.yaml` |

## Hermes provider keys

These live under `memory.mnemosyne` in Hermes' `config.yaml`. The installers do not set
any of them — they only set `memory.provider: mnemosyne`.

| Key | Default | Description |
| --- | --- | --- |
| `auto_sleep` | `true` | Run `sleep()` automatically once working memory passes the threshold |
| `sleep_threshold` | `50` | Working-memory count that triggers auto-sleep |
| `vector_type` | `int8` | Vector storage type (`float32`, `int8`, `bit`) |
| `ignore_patterns` | `[]` | Regex patterns to keep out of memory |
| `profile_isolation` | `false` | Per-profile isolation via Mnemosyne banks |
| `shared_surface_path` | `data/shared/mnemosyne.db` | SQLite path for shared-surface memories |
| `shared_surface_read` | `false` | Merge shared-surface results into private recall |
| `skip_contexts` | `cron,flush,subagent,background,skill_loop` | Agent contexts that skip Mnemosyne init |
| `sync_roles` | `['user', 'assistant']` | Conversation roles autosaved by `sync_turn()` |

Set them with `hermes config set memory.mnemosyne.<key> <value>`.

Because these are *your* settings rather than something the installer wrote, the
uninstallers deliberately leave them alone. If you have tuned this block and then remove
Mnemosyne, the keys stay behind as dead config — harmless, but yours to clear.

## Storage locations

| Variable | Default | Notes |
| --- | --- | --- |
| `MNEMOSYNE_HOME` | `~/.hermes/mnemosyne` | Root for all Mnemosyne data |
| `MNEMOSYNE_DATA_DIR` | `~/.hermes/mnemosyne/data` | Database, logs, models, stats |
| `MNEMOSYNE_BLOB_DIR` | — | Blob storage from the content sanitizer |
| `MNEMOSYNE_SHARED_DB_PATH` | `data/shared/mnemosyne.db` | Shared-surface database |
| `MNEMOSYNE_AUTO_MIGRATE` | `1` | Auto-migrate the schema on startup |

The installers set `MNEMOSYNE_HOME`, and on Windows also `MNEMOSYNE_DATA_DIR` when a
non-default home is given, because `MNEMOSYNE_HOME` alone does not move the database
there. The uninstallers remove `MNEMOSYNE_BLOB_DIR` too when it points outside the data
directory — otherwise blobs would survive a full removal.

## Embeddings

Mnemosyne embeds locally with `BAAI/bge-small-en-v1.5` via fastembed (384 dimensions).
No API key is required.

| Variable | Default | Notes |
| --- | --- | --- |
| `MNEMOSYNE_NO_EMBEDDINGS` | `false` | Hard off — disables dense retrieval entirely |
| `MNEMOSYNE_EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | fastembed model |
| `MNEMOSYNE_EMBEDDING_DIM` | `384` | Override vector dimension |
| `MNEMOSYNE_VEC_TYPE` | `int8` | `int8`, `float32`, `float16`, `binary` |
| `MNEMOSYNE_EMBEDDINGS_VIA_API` | `false` | Force cloud embeddings |
| `MNEMOSYNE_EMBEDDING_API_URL` | `https://openrouter.ai/api/v1` | Cloud endpoint |
| `MNEMOSYNE_EMBEDDING_API_KEY` | — | Cloud API key |

`--no-embeddings` / `-NoEmbeddings` sets `MNEMOSYNE_NO_EMBEDDINGS=1`. It does **not**
avoid downloading the extras — `mnemosyne-hermes` depends on
`mnemosyne-memory[embeddings]` outright, so pip installs fastembed, onnxruntime and
sqlite-vec either way. What it does is stop dense retrieval at runtime: memories are
stored without vectors and recall falls back to keyword/FTS only.

The flag is declarative: **omitting it on a later install clears the variable again**, so
one flagged run does not disable dense retrieval forever.

That is the *deliberate* version of the failure described in
[Troubleshooting](troubleshooting.md#memories-are-stored-but-semantic-recall-never-matches).
If you did not ask for it and still see `dense_score: 0.0000`, something is broken rather
than configured.

## Ranking

Recall blends three signals; the weights are tunable per call or globally.

| Variable | Default |
| --- | --- |
| `MNEMOSYNE_VEC_WEIGHT` | `0.5` |
| `MNEMOSYNE_FTS_WEIGHT` | `0.3` |
| `MNEMOSYNE_IMPORTANCE_WEIGHT` | `0.2` |
| `MNEMOSYNE_RECENCY_HALFLIFE` | `168` hours |

The `mnemosyne_recall` tool accepts `vec_weight`, `fts_weight`, `importance_weight` and
`temporal_weight` per call, which is the quickest way to tell a vector failure from a
tuning problem — set `vec_weight: 1.0, fts_weight: 0.0` and see whether anything matches.

## Sleep consolidation

Working memories are promoted to episodic memory by a `sleep()` cycle that summarises
them with an LLM. It runs entirely locally by default (a GGUF model, then extractive
compression); leave `MNEMOSYNE_LLM_BASE_URL` unset and `MNEMOSYNE_HOST_LLM_ENABLED` off to
keep it that way. [`--recommended-config`](#recommended-configuration) turns the latter on.

| Variable | Default | Notes |
| --- | --- | --- |
| `MNEMOSYNE_LLM_ENABLED` | `true` | LLM summarisation during sleep |
| `MNEMOSYNE_LLM_BASE_URL` | — | Any OpenAI-compatible endpoint |
| `MNEMOSYNE_LLM_API_KEY` | — | Key for that endpoint |
| `MNEMOSYNE_LLM_MODEL` | — | Model identifier |
| `MNEMOSYNE_AUTO_SLEEP_ENABLED` | `true` | Automatic consolidation on a background thread |
| `MNEMOSYNE_WM_TTL_HOURS` | `168` | Age at which a working memory becomes eligible for consolidation |
| `MNEMOSYNE_WM_MAX_ITEMS` | `10000` | Eviction threshold |

This is why `mnemosyne stats` can report `Total memories: 0` next to a non-zero working
count: nothing has been consolidated yet. `mnemosyne sleep` is a no-op until entries are
old enough.

## Recommended configuration

`--recommended-config` / `-RecommendedConfig` writes the settings below and reads each one
back, failing the install if one does not stick. Without the flag the installers leave all
of them alone.

| Setting | Where | Written | Default | Why |
| --- | --- | --- | --- | --- |
| `cross_session` | Mnemosyne `config.yaml` | `true` | `false` | Recall searches every session of the bank, not just the current one. |
| `default_scope` | Mnemosyne `config.yaml` | `global` | `session` | New memories are stored as visible to all sessions. |
| `write_classifier` | Mnemosyne `config.yaml` | `strict` | `off` | Drops writes that contain a secret-shaped value (API keys, tokens, private keys, credentialed connection strings), built-in noise (shell output, stack traces, bare acknowledgements) and long low-structure dumps. |
| `MNEMOSYNE_HOST_LLM_ENABLED` | Hermes `.env` | `true` | `false` | Consolidation and fact extraction use Hermes' own authenticated model client, so Mnemosyne needs no credentials of its own. The text of your memories then goes to Hermes' configured model provider, and those calls cost tokens. |
| `MNEMOSYNE_LLM_ENABLED` | Hermes `.env` | `true` | `true` | LLM summarisation during sleep, pinned in case the environment turns it off. |

The two LLM switches are environment variables because Mnemosyne reads them only from the
environment, once, when it is imported; written to `config.yaml` they would have no effect.
Hermes loads its `.env` before it imports the provider. That is also why the flag cannot be
combined with `--skip-hermes-configuration`.

The `config.yaml` keys are written directly, not with `mnemosyne config set`: the first
`config set` copies every setting from the current environment into the file, and since
the file outranks `MNEMOSYNE_*` variables, that would freeze all of them. The rewrite keeps
the file's leading comment block but drops any other comments. Two of the keys,
`default_scope` and `write_classifier`, can also be set under `memory.mnemosyne` in Hermes'
`config.yaml`, which the provider reads first; the installer warns when one there disagrees.

With the defaults, every memory stays in the session that wrote it, so a new
conversation — a new day, a restarted gateway, another client — recalls nothing from
earlier ones. Hermes starts a new session far more often than most people expect.

> **Do not use it where one Hermes serves several people who must not see each other's
> memories.** In a group chat Hermes gives each participant their own session key; with
> these settings recall ignores that boundary for everything in the bank. Leave the flag
> off there, or give each audience its own Hermes home.

Re-running an installer with the flag rewrites these settings, so a later manual change to
one of them does not survive it. To undo one, set it back, for example
`mnemosyne config set cross_session false` or `hermes config unset MNEMOSYNE_HOST_LLM_ENABLED`.

Mnemosyne's `config.yaml` lives at `<MNEMOSYNE_DATA_DIR>/config.yaml`, else
`<HERMES_HOME>/mnemosyne/config.yaml`. With the default paths that is inside the data
directory, which the uninstallers delete unless you pass `--keep-data` / `-KeepData`. On
Linux/macOS with a custom `MNEMOSYNE_HOME` it is not, and it survives an uninstall, so
delete it yourself. The uninstallers remove the two `.env` lines while they still read
`true`, the value the flag wrote; any other value is left alone as yours.

## Windows: the config encoding trap

Mnemosyne writes `config.yaml` using Python's default text encoding — the ANSI codepage
on Windows — but always reads it back as UTF-8. The template contains an em dash, so
every later command prints:

```text
Failed to inspect legacy provider defaults: 'utf-8' codec can't decode byte 0x97
```

The installer re-encodes the file as UTF-8, **after** running `hermes memory status` and
`mnemosyne stats`, because those are what create it on a fresh install. Re-encoding
earlier silently does nothing. If you hit the warning, re-running the installer fixes it.

## Where these installers put things

| Variable | Set by | Persisted as |
| --- | --- | --- |
| `HERMES_HOME` | both installers | rc-file `export` / User env var |
| `MNEMOSYNE_HOME` | both installers | rc-file `export` / User env var |
| `MNEMOSYNE_DATA_DIR` | Windows, non-default home only | User env var |
| `MNEMOSYNE_NO_EMBEDDINGS` | `--no-embeddings` only | rc-file `export` / User env var |
| `MNEMOSYNE_LLM_ENABLED`, `MNEMOSYNE_HOST_LLM_ENABLED` | `--recommended-config` only | Hermes `.env` |

Everything else is left at its default for you to set. The uninstallers remove all of
these (the `.env` lines only while they still read `true`).
