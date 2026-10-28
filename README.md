# SmartFS

SmartFS is a Linux-native semantic file manager. It indexes opted-in directory trees,
extracts text from TXT/Markdown, PDF, and DOCX files, mirrors validated metadata into
Linux extended attributes (`user.smartfs.*`), and keeps a SQLite index for fast lookup.

**Current checkpoint: Phase 7 (final)** — indexing, recursive inotify watching, persistent
crash-recoverable jobs, local/Gemini analysis, optional local embeddings, FTS5 BM25
full-text search, semantic vector search, reciprocal-rank fusion with explanations, a
PySide6 desktop interface with non-blocking workers, organization tools (tag rename/merge,
duplicate groups, saved searches, related documents), and maintenance commands
(`doctor`, backups). The default configuration remains local-only and makes no network
calls.

See [`docs/PROJECT_REPORT.md`](docs/PROJECT_REPORT.md) for the full project report and
[`docs/VIVA.md`](docs/VIVA.md) for the viva architecture guide, live demo script, grading
checklist, design defenses, and likely questions.

## What works through Phase 7

- Recursive traversal with `os.scandir`, symlink avoidance, exclusions, partial-file
  filtering, and a size cap.
- Incremental SHA-256 indexing with chunked reads and extraction caching.
- TXT/MD with UTF-8 → UTF-8-SIG → Latin-1 fallback; PDF via PyMuPDF; DOCX via
  python-docx (paragraphs and tables).
- SQLite index with WAL mode, foreign keys, explicit transactions, schema versioning,
  automatic backup before migrations, FTS5 full-text search, and vector storage.
- xattr mirroring using `user.smartfs.category`, `.tags`, `.summary`, `.hash`, `.version`.
- xattr support probing with SQLite-only fallback; no crashes on unsupported filesystems.
- Reconciliation that repairs dropped/stale xattrs and detects renames by inode or hash.
- User tags with provenance (`user` vs `ai`) and tag normalization.
- Recursive `watchdog`/inotify integration for opted-in directories.
- Per-path debounce (1.5s default) and quiet-period stability checks, so files still being
  written are not indexed prematurely.
- Persistent SQLite job queue with atomic claims, active-job coalescing, rerun requests,
  bounded in-memory dispatch, retry with exponential backoff, cancellation, crash recovery,
  and graceful SIGINT/SIGTERM shutdown.
- Startup plus periodic reconciliation (10 minutes default) and immediate incremental
  scans when directories are created or moved.
- Self-write suppression so SmartFS xattr updates do not create an infinite re-index loop.
- Single-instance daemon lock per database.
- Deterministic local analysis: category, 3–8 tags, summary, topics, and confidence with
  no network access.
- Opt-in Gemini analysis through `google-genai`, gated by explicit consent, a key, and
  `local_only = false`; cloud errors automatically fall back to local analysis.
- API keys come from `GEMINI_API_KEY`, `GOOGLE_API_KEY`, or the system keyring. Keys are
  never written to the SmartFS config or logs.
- Versioned analysis caching by content hash/provider/model/prompt version.
- Optional `fastembed` embeddings (`BAAI/bge-small-en-v1.5` by default), stored as float32
  blobs in SQLite and disabled by default to avoid an unexpected model download.
- FTS5 full-text index over paths, titles, authors, categories, summaries, and full
  extracted text, ranked with BM25 and weighted toward filenames and titles.
- Hybrid search with local semantic cosine ranking and reciprocal-rank fusion (RRF).
  `--mode auto` uses fusion when embeddings exist, otherwise text search.
- Explainable results: BM25 score, cosine similarity, component ranks, fused RRF score,
  matching snippet, category, and tags.
- PySide6 desktop window with debounced search, tag/category filters, mode selection,
  result table, details/evidence pane, copy-path and double-click open, and category
  discovery.
- Non-blocking GUI workers: search and scans run on a bounded `QThreadPool` with their own
  SQLite connections; stale search results are discarded by request id.
- In-window incremental scan of watched directories with live progress; one scan at a time.
- Tag manager dialog with usage/provenance counts and background global rename or merge;
  SQLite is updated first, then `user.smartfs.tags` is re-synced.
- Duplicate-content view grouped by SHA-256, in the CLI and GUI, with copy/open actions.
  SmartFS reports duplicates but never deletes them.
- Saved-search collections (query, tags, category, mode) stored in SQLite, with a GUI
  manager and CLI create/list/show/delete commands.
- Related documents ranked by shared tags and local embedding cosine; computed in a
  background worker when a result is selected.
- `doctor` integrity checks for SQLite, FTS, vectors, tags, categories, jobs, and xattrs,
  with safe `--repair` for derived data and an automatic pre-repair backup.
- Consistent online backups with `backup create/list/prune`; backup integrity is verified
  before a copy is accepted.
- CLI: `init`, `watch add/remove/list`, `scan`, `daemon`, `gui`, `tags`,
  `tag add/remove/rename/merge`, `find`, `duplicates`, `related`, `collection`,
  `doctor`, `backup`, `status`, and
  `ai status/setup/enable/disable/set-key/forget-key/embeddings`.
- Tests for xattr round-trips, UTF-8 truncation, incremental scans, rename/atomic-replace
  persistence, missing files, exclusions, extraction, CLI behavior, queue semantics,
  retries, cancellation, shutdown recovery, debouncing, daemon end-to-end indexing,
  local/Gemini analysis, credential handling, embedding persistence, FTS5/BM25,
  semantic ranking, RRF fusion, filters, snippets, explanations, collections, duplicate
  detection, related scoring, doctor repairs, backup/prune behavior, GUI models, dialogs,
  workers, and the CLI GUI handoff.

## Fedora setup

```bash
sudo dnf install -y python3 python3-devel gcc redhat-rpm-config git attr
# attr provides getfattr/setfattr, used to verify the graded xattr requirement.

# Install uv if it is not already available:
curl -LsSf https://astral.sh/uv/install.sh | sh
# or, where packaged: sudo dnf install -y uv

cd smartfs
uv python install 3.12
uv sync --extra test --extra ai
```

`watchdog` and `keyring` are base dependencies, so no `watch` extra is required. The `ai`
extra installs `google-genai` and `fastembed`; local heuristic analysis works without it.
If you prefer not to use a pinned interpreter, `uv sync` uses the available Python (3.12+).

For large watched trees, raise the inotify limit:

```bash
sudo tee /etc/sysctl.d/99-smartfs.conf >/dev/null <<'EOF'
fs.inotify.max_user_watches = 524288
EOF
sudo sysctl --system
```

The desktop interface is optional:

```bash
uv sync --extra test --extra ai --extra gui
```

## Run the CLI

From the project directory:

```bash
uv run smartfs init
uv run smartfs watch add ~/Documents
uv run smartfs scan          # one-off full scan
uv run smartfs daemon        # foreground watcher + workers
uv run smartfs status
uv run smartfs ai status
uv run smartfs duplicates
uv run smartfs collection list
uv run smartfs doctor
uv run smartfs backup create
```

After activating `.venv`, the shorter form works:

```bash
source .venv/bin/activate
smartfs init
```

The installed console command is also available through `uv run smartfs ...`.

### AI setup and privacy

Local heuristic analysis is always available when analysis is enabled and requires no key
or network access. Gemini is opt-in:

```bash
uv run smartfs ai status
uv run smartfs ai setup         # consent prompt, optional key, optional embeddings
uv run smartfs ai set-key       # hidden prompt; or GEMINI_API_KEY/GOOGLE_API_KEY
uv run smartfs ai enable --embeddings  # cloud plus local embeddings
uv run smartfs ai embeddings enable    # local embeddings only; no key needed
uv run smartfs ai embeddings disable
uv run smartfs ai disable       # returns to local-only; keeps the stored key
uv run smartfs ai forget-key
```

`ai setup` asks before enabling cloud analysis. Non-interactive use requires an existing
key and `--yes`: `smartfs ai setup --yes --no-embeddings`. Key lookup prefers
`GEMINI_API_KEY`, then `GOOGLE_API_KEY`, then the system keyring (`smartfs` /
`gemini-api-key`).

When cloud analysis is enabled, SmartFS sends the configured excerpt (12,000 characters by
default), filename, MIME type, title, author, page count, and truncation flag to the
configured Gemini model (default `gemini-3.5-flash-lite`). It does not send the file itself. Set `analysis_max_chars` lower
to reduce the excerpt. If consent is off, `local_only` is true, no key exists, or Gemini
fails, SmartFS uses local analysis and continues indexing.

Embeddings are local-only and disabled by default. Enabling them downloads the configured
`fastembed` model on first use into the SmartFS cache directory. They are stored in SQLite
and are not mirrored to xattrs. `smartfs ai status` reports providers and counts without
revealing any secret.

### Desktop GUI

```bash
uv run smartfs gui          # launch with the existing index
uv run smartfs gui --scan   # also start an incremental scan after opening
```

The GUI provides the same FTS5/semantic/hybrid modes as `find`, with tag and category
filters. Typing is debounced, and search runs off the GUI thread. Search results show the
filename, category, tag union, contributing search sources, and score; selecting a result
shows its summary, matched snippet, BM25/cosine ranks, and RRF score. Double-click opens a
file with the desktop handler, and `Ctrl+F`, `Ctrl+R`, and `Ctrl+Shift+S` focus search,
refresh, and scan.

`Scan now` indexes configured watched directories in the background without requiring the
daemon. For live filesystem updates, keep `smartfs daemon` running as well; the GUI can
search the shared WAL database while the daemon indexes. The GUI never stores API keys or
sends queries to the network. Semantic queries are embedded locally when embeddings are
enabled. If PySide6 is absent, `smartfs gui` fails with an installation hint instead of
affecting the rest of the CLI.

The **Tools** menu adds:

- **Manage tags…** — usage and provenance counts, global rename, and merge into an
  existing or new tag. Operations run in a worker and re-sync xattrs after commit.
- **Duplicate content…** — exact SHA-256 groups as an expandable tree; open or copy a path.
- **Saved searches…** — save the current query/filters/mode, load a collection back into
  the window, or delete a collection.
- **Save current search…** — shortcut for naming the current filter state.

Selecting a result also fills the **Related documents** list from shared tags and local
vector similarity. Double-click a related entry to open it. Related results are advisory
and never modify metadata.

### Organization commands

```bash
# Duplicate content groups (report-only)
smartfs duplicates
smartfs duplicates --limit 20

# Global tag maintenance
smartfs tag rename physics natural-science
smartfs tag merge natural-science science

# Related documents for an indexed file
smartfs related ~/Documents/thermo.txt
smartfs related ~/Documents/thermo.txt --limit 5

# Saved searches
smartfs collection save "Physics Review" --tag physics   --category "lecture notes" --mode hybrid entropy notes
smartfs collection list
smartfs collection show "Physics Review"
smartfs collection delete "Physics Review"
```

Tag rename refuses to overwrite an existing target; use `tag merge` when both tags should
become one. Both operations preserve `user` and `ai` provenance, then mirror the new union
to xattrs where supported. Duplicate groups are computed from indexed content hashes, so
renamed copies still match and different files with similar text do not.

### Maintenance and backups

```bash
# Diagnose integrity, FTS coverage, vector blobs, orphan tags, categories, jobs, xattrs.
# Exit code is 1 when unresolved errors remain.
smartfs doctor

# Repair safely derived data; writes a pre-repair backup first.
smartfs doctor --repair
smartfs doctor --repair --vacuum

# Backup the live database consistently (SQLite online backup + integrity check).
smartfs backup create
smartfs backup create --keep 5
smartfs backup list
smartfs backup prune --keep 10
```

`doctor --repair` rebuilds FTS from cached full text, removes orphan FTS rows and tags,
deletes dimensionally invalid vectors, normalizes unknown categories to `other`, and
re-syncs xattrs. It never deletes user documents. A missing file remains `missing` until a
scan sees it again. `VACUUM` is opt-in because it takes an exclusive write lock.

### Daemon

`smartfs daemon` is intentionally foreground-only. Run it in a terminal or under your
process supervisor. It validates the lock, starts the worker pool and watcher, performs a
startup reconciliation, then reconciles every 10 minutes.

```bash
smartfs daemon --workers 2 --reconcile-interval 600
smartfs daemon --no-startup-reconcile
```

- `--workers N` overrides the configured worker count.
- `--reconcile-interval SECONDS` overrides the periodic scan interval (minimum 10s).
- `--no-startup-reconcile` skips the immediate full scan; watcher events still apply.
- `Ctrl+C` or `SIGTERM` stops new watches, flushes pending debounced paths, lets active
  jobs reach a cancellation boundary, requeues unfinished work, and exits cleanly.
- Jobs survive daemon restarts. On startup, any job left `running` is requeued.
- Finished jobs older than 7 days are purged during reconciliation.

Only one daemon may run against a given database. A second instance exits with a clear
lock error instead of duplicating work.

### Try it

In terminal 1:

```bash
mkdir -p ~/SmartFS-Demo
uv run smartfs init
uv run smartfs watch add ~/SmartFS-Demo
uv run smartfs daemon
```

In terminal 2:

```bash
printf 'Thermodynamics lecture notes\nEntropy always increases in an isolated system.\n' \
  > ~/SmartFS-Demo/thermo.txt
sleep 4

uv run smartfs tag add ~/SmartFS-Demo/thermo.txt physics
uv run smartfs tags ~/SmartFS-Demo/thermo.txt
uv run smartfs status
uv run smartfs ai status

# Standard Linux tool verification, exactly as the examiner can run it:
getfattr -d -m user.smartfs ~/SmartFS-Demo/thermo.txt

# Query by user or locally generated AI tag:
uv run smartfs find --tag physics --explain
uv run smartfs find --tag thermodynamics --explain

# Full-text and hybrid discovery:
uv run smartfs find "entropy isolated system" --explain
uv run smartfs find "thermodynamics" --mode text --explain
```

Expected `getfattr` output includes the hash, category, summary, version, and a sorted tag
union containing both the user tag and AI-generated tags, for example:

```text
user.smartfs.category="lecture notes"
user.smartfs.hash="..."
user.smartfs.tags="entropy, lecture, notes, physics, thermodynamics"
user.smartfs.version="3"
```

To demonstrate cloud enrichment, run `smartfs ai setup` first, then force a rescan with
`smartfs scan --force`. The cache avoids duplicate Gemini calls for unchanged content.

To demonstrate semantic fusion, enable local embeddings and rescan. No Gemini key is
required for local vectors:

```bash
uv run smartfs ai embeddings enable
uv run smartfs scan --force
uv run smartfs find "energy and isolated systems" --mode hybrid --explain
```

The first embedding run downloads the model into the SmartFS cache. If embeddings are
disabled or missing, `--mode auto` falls back to FTS5 text search.

## Storage locations

Default XDG paths (shown by `smartfs init` / `smartfs status`):

| Data | Path |
| --- | --- |
| Configuration | `~/.config/smartfs/config.toml` |
| SQLite index and job queue | `~/.local/share/smartfs/smartfs.db` |
| Backups | `~/.local/share/smartfs/backups/` |
| Embedding model cache | `~/.cache/smartfs/fastembed/` |
| Daemon lock | `~/.local/share/smartfs/daemon.lock` |
| Logs (JSON lines) | `~/.local/state/smartfs/log/smartfs.log` |

`$SMARTFS_HOME` overrides all locations with:

```text
$SMARTFS_HOME/config/config.toml
$SMARTFS_HOME/data/smartfs.db
$SMARTFS_HOME/data/backups/
$SMARTFS_HOME/cache/fastembed/
$SMARTFS_HOME/logs/smartfs.log
```

This is useful for demos and tests. Document contents are never written to logs. Logs
record paths, hashes, sizes, error types, job IDs, and command events.

## Architecture through Phase 7

```text
CLI (argparse)
  ├── scan ────────────────► scanner.py ──► index_file()
  ├── ai ──────────────────► credentials.py + config.py
  ├── find ────────────────► search.py ──► files_fts + embeddings + RRF
  ├── status ──────────────► db.py (files, tags, jobs, analysis, embeddings, FTS)
  ├── gui ─────────────────► gui.py + gui_dialogs.py
  │     ├── SearchTask (QThreadPool) ──► search.py + read-only SQLite
  │     ├── IndexTask (QThreadPool) ───► scan_directory() + own SQLite connection
  │     ├── RelatedTask (QThreadPool) ─► related.py + read-only SQLite
  │     ├── TagChangeTask ─────────────► tags.rename/merge + xattr sync
  │     └── SmartFSWindow ─────────────► table, filters, details, related, dialogs
  ├── doctor ──────────────► doctor.py ──► integrity + safe repair
  ├── backup ──────────────► db.py (online backup, list, prune)
  └── daemon ──────────────► daemon.py
        ├── WatcherService (watchdog/inotify)
        │     └── debounce per path ──► directory discover / file job
        ├── SmartFSDaemon reconciliation thread
        │     └── discover_changes() at startup and every 10 minutes
        └── WorkerPool
              ├── dispatcher: atomic claim from SQLite jobs
              ├── bounded queue (100 jobs default)
              ├── N workers (2 default), one SQLite connection per thread
              └── PipelineProcessor
                    ├── wait_until_stable()
                    ├── index_file()
                    │     ├── hash + extract
                    │     ├── AnalysisService (Gemini or local fallback)
                    │     ├── EmbeddingService (optional, local fastembed)
                    │     ├── DB commit + AI tags + FTS5 document
                    │     └── xattr mirror
                    └── delete job → mark missing, preserve metadata

analysis.py: local heuristics, Gemini JSON validation, rate limit, cache, fallback
credentials.py: environment → keyring resolution, never returns secrets in status
embeddings.py: lazy fastembed provider, float32 packing, failure isolation
search.py: safe FTS5 queries, filters, cosine ranking, RRF fusion, explanations
related.py: shared-tag and local-vector related-document ranking
doctor.py: integrity checks, FTS rebuild, derived-data repair, maintenance counters
gui.py: PySide6 window, results model, debounced search, background workers
gui_dialogs.py: tag manager, duplicate tree, saved-search collections
db.py: WAL, foreign_keys=ON, BEGIN IMMEDIATE, user_version migrations, backups, FTS
jobs.py: enqueue, claim, retry, cancel, requeue, crash recovery, job stats
watcher.py: watchdog filtering, debounce, directory reconciliation
tags.py: normalization, provenance, union mirror, rename/merge helpers
suppression.py: thread-safe self-write suppression
```

SQLite is authoritative. The xattr values are a user-visible mirror and are written only
after the corresponding database transaction commits. Re-scanning re-applies tags when a
tool such as `cp`, an editor's atomic save, or an archiver drops xattrs.

The current schema version is 5. The `jobs` table (version 2) uses a partial unique index
to prevent duplicate active jobs for the same `(path, type)`. If a change arrives while
that job is running, `rerun_requested` is set and the job is rescheduled after completion
instead of losing the event. Version 3 adds the `embeddings` table keyed by
`(sha256, model)`. Version 4 adds the standalone FTS5 `files_fts` table, keyed by file id,
and backfills it from existing metadata. `INDEX_VERSION` 3 forces a one-time reindex so
the FTS body contains full extracted text. Version 5 adds the `collections` table for
named saved searches. Migrations are backed up automatically.

## Query examples

```bash
smartfs find --tag physics
smartfs find --tag physics --tag exam
smartfs find --category "lecture notes"
smartfs find --tag report "quarterly revenue"
smartfs find "entropy" --explain
smartfs find "machine learning" --mode text --limit 20
smartfs find "energy in isolated systems" --mode semantic --explain
smartfs find "thermodynamics exam" --mode hybrid --explain
```

`--tag` may be repeated; all requested tags must match. `--category` is an exact match.
Text mode uses FTS5 BM25 over full extracted content. Semantic mode requires enabled local
embeddings and stored vectors. Hybrid mode fuses both rankings with RRF; auto mode chooses
hybrid when semantic data is available and otherwise uses text.

Text queries first require all meaningful terms, then fall back to any-term matching when
an all-term query has no results. FTS input is tokenized and quoted before use, so query
operators and punctuation cannot inject FTS syntax. A query with no tokenizable terms
falls back to literal substring filtering.

### Search explanations

With `--explain`, SmartFS prints the mode and candidate counts, the safe FTS query, then
per-file evidence:

```text
Search mode: hybrid (text=1, semantic=2)
FTS query: "entropy"
/path/thermo.txt (lecture notes) [entropy, lecture, notes, physics]
  why: matched tag(s): 'physics'; query: 'entropy'; full-text rank #1, BM25 1.234;
       semantic rank #2, cosine 0.412; RRF score 0.03252; matched text: ...
```

Raw rankings are reciprocal-rank fused with `k=60`: each source contributes
`1 / (60 + rank)`. This makes hybrid ranking robust to incomparable BM25 and cosine score
scales while still exposing both signals for the viva.

## xattr support and fallback

SmartFS only uses the `user.*` namespace. It probes support by writing, reading, and
removing a temporary probe attribute, and degrades gracefully if the filesystem returns
`ENOTSUP`, `EOPNOTSUPP`, `EPERM`, `EACCES`, `ENOSPC`, `E2BIG`, or `EROFS`.

- ext4, XFS, and Btrfs normally support user xattrs.
- Some FAT/exFAT, network, overlay, or tmpfs configurations do not.
- On unsupported filesystems, tags stay in SQLite and `status` reports DB-only mode.

Writing an xattr updates ctime but not mtime and never modifies file contents. xattr writes
are recorded in a thread-safe suppression set so the watcher ignores its own metadata
events and avoids an infinite re-index loop. Reconciliation also verifies existing xattrs
and repairs them if another tool dropped them.

## Privacy and security

- Only explicitly watched directories, or explicit `smartfs scan <dir>` paths, are read.
- Default exclusions cover `.git`, `node_modules`, `.cache`, keys, `.env`, password
  databases, and similar sensitive files.
- Files over 25 MiB are not extracted. PDF extraction has a page cap.
- The default configuration is local-only. Local heuristic analysis makes no network
  requests and needs no API key.
- Gemini is used only after explicit consent, with `local_only = false`, and with a key.
  Cloud requests can be disabled at any time with `smartfs ai disable`.
- Keys are read from the environment or system keyring and are never written to config or
  logs. `ai status` reports only the source.
- Cloud analysis sends a bounded document excerpt and metadata, not the original file.
- Embeddings are local-only, disabled by default, and stored in SQLite, not xattrs.
- Document text is not logged.
- SmartFS never deletes, renames, or moves user files. Removed files are marked `missing`
  in SQLite, preserving their metadata and tags.
- Queue and retry metadata contain paths and error summaries, never document contents.

## Troubleshooting

- **`Database not found`** — run `smartfs init`.
- **`File is not indexed`** — the daemon may still be waiting for the debounce/stability
  window. Check `smartfs status`, or run `smartfs scan <parent-directory>`.
- **No watcher events** — confirm the daemon is running, the directory is listed by
  `smartfs watch list`, and the directory still exists. Check the JSON log.
- **`Another SmartFS daemon is already running`** — stop the existing instance; the lock
  is released automatically on exit.
- **A job keeps retrying** — files still being written or temporarily locked are retried
  with exponential backoff (3 attempts default). Inspect recent failures in
  `smartfs status` and the daemon log.
- **xattrs DB-only** — the filesystem does not support `user.*` xattrs. The index still
  works; use ext4/XFS/Btrfs for the visible mirror.
- **PDF/DOCX skipped** — install the base dependencies with `uv sync --extra test`.
- **Cloud consent enabled but no key** — run `smartfs ai status`; set `GEMINI_API_KEY`,
  `GOOGLE_API_KEY`, or `smartfs ai set-key`. SmartFS falls back to local analysis meanwhile.
- **Keyring unavailable** — headless systems may have no Secret Service backend. Use
  `GEMINI_API_KEY` for the session, or install/configure a keyring backend.
- **Embeddings unavailable** — install the `ai` extra with `uv sync --extra ai`; the first
  enabled run downloads the model. Indexing continues without vectors on failure.
- **Large directory watch limits** — raise `fs.inotify.max_user_watches` as shown above.

## Development

```bash
uv sync --all-extras
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```

The final checkpoint passes the full suite, including FTS5, semantic, RRF, CLI search,
collections, duplicates, related documents, doctor repair, backup/prune, GUI
table/details/dialogs, background workers, and CLI handoff tests. Qt tests use the
offscreen platform plugin. Gemini calls use injected fakes; no network access or real API
key is required for tests.

## Phase roadmap

1. **Phase 1 (complete):** DB, traversal, extraction, xattr store, user tags, CLI.
2. **Phase 2 (complete):** watchdog/inotify, debounce, stability checks, persistent
   queue, retries, crash recovery, reconciliation.
3. **Phase 3 (complete):** local and consented Gemini analysis, keyring, caching,
   fallback, optional local embeddings.
4. **Phase 4 (complete):** FTS5 BM25, semantic search, RRF fusion, snippets,
   filters, and explainable results.
5. **Phase 5 (complete):** PySide6 GUI with debounced hybrid search, details,
   filters, and non-blocking index/search workers.
6. **Phase 6 (complete):** tag rename/merge UI, duplicate content groups, saved searches,
   and related documents.
7. **Phase 7 (this checkpoint):** doctor/repair, backup CLI, full test suite, and final
   viva documentation in [`docs/VIVA.md`](docs/VIVA.md).

OCR is not implemented; a hook is reserved for a later OCR phase. Exact-hash duplicate
detection is report-only. Collections store search criteria, not copied file sets.
