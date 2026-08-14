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
| `auto_sleep` | `false` | Run `sleep()` automatically once working memory passes the threshold |
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
compression); leave `MNEMOSYNE_LLM_BASE_URL` unset to keep it that way.

| Variable | Default | Notes |
| --- | --- | --- |
| `MNEMOSYNE_LLM_ENABLED` | `true` | LLM summarisation during sleep |
| `MNEMOSYNE_LLM_BASE_URL` | — | Any OpenAI-compatible endpoint |
| `MNEMOSYNE_LLM_API_KEY` | — | Key for that endpoint |
| `MNEMOSYNE_LLM_MODEL` | — | Model identifier |
| `MNEMOSYNE_AUTO_SLEEP_ENABLED` | `false` | Automatic consolidation |
| `MNEMOSYNE_WM_TTL_HOURS` | `24` | Working-memory expiry |
| `MNEMOSYNE_WM_MAX_ITEMS` | `10000` | Eviction threshold |

This is why `mnemosyne stats` can report `Total memories: 0` next to a non-zero working
count: nothing has been consolidated yet. `mnemosyne sleep` is a no-op until entries are
old enough.

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

Everything else is left at its default for you to set. All four are removed by the
uninstallers.
