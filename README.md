# SmartFS

SmartFS is a Linux-native semantic file manager. It indexes opted-in directory trees,
extracts text from TXT/Markdown, PDF, and DOCX files, mirrors validated metadata into Linux
extended attributes (`user.smartfs.*`), and maintains a SQLite index for fast, explainable
search.

**Current version: 0.7.0 (final phase)** — recursive indexing, inotify watching, persistent
crash-recoverable jobs, local/Gemini analysis, optional local embeddings, FTS5 BM25
full-text search, semantic vector search, reciprocal-rank fusion, a PySide6 desktop
interface, organization tools, diagnostics, and backups. The default configuration is
local-only and makes no network calls.

## Documentation

| Document | Contents |
| --- | --- |
| [Linux setup](setup/linux.md) | Full installation, Gemini key, xattrs, inotify, GUI, and verification. |
| [Windows setup](setup/windows.md) | Complete setup through WSL2; native Windows limitations. |
| [macOS setup](setup/macos.md) | CLI/GUI setup and the Linux-feature limitations of macOS. |
| [Project report](docs/PROJECT_REPORT.md) | Full academic report: design, implementation, testing, evaluation, and appendices. |
| [Viva guide](docs/VIVA.md) | Architecture walkthrough, demo script, grading notes, and likely questions. |

Each setup guide explains how to create and store your **own** Gemini API key. Never commit a
key to Git or include it in a report.

## Overview

Personal document collections are difficult to search when filenames are generic and
directory structure does not reflect content. SmartFS combines four ideas:

1. **A persistent local index** instead of repeatedly scanning the filesystem.
2. **Linux-visible metadata** through `user.smartfs.*` extended attributes.
3. **Hybrid retrieval** that fuses full-text BM25 ranking with local semantic similarity.
4. **Optional AI enrichment** that is consent-gated, validated, and never required for core
   operation.

SQLite is authoritative. Xattrs are a mirror for interoperability with standard Linux tools.
SmartFS never deletes, renames, or moves user files; duplicates are reported, and missing
files are marked rather than removed from the index.

## Feature Summary

### Indexing and metadata

- Recursive traversal with symlink avoidance, exclusions, partial-file filtering, and a
  configurable size cap.
- Incremental SHA-256 indexing with chunked reads and extraction caching.
- TXT/Markdown extraction with UTF-8, UTF-8-SIG, and Latin-1 fallback.
- PDF extraction through PyMuPDF and DOCX extraction through python-docx.
- SQLite WAL index with foreign keys, explicit transactions, schema migrations, and
  automatic pre-migration backups.
- Rename detection through inode/device and content hash; missing files are marked without
  deleting metadata or tags.
- `user.smartfs.category`, `.tags`, `.summary`, `.hash`, and `.version` mirroring, with
  support probing and SQLite-only fallback on unsupported filesystems.
- Self-write suppression so xattr updates do not trigger an indexing loop.

### Analysis and search

- Deterministic local analysis: category, 3–8 tags, summary, topics, and confidence.
- Opt-in Gemini analysis with consent, a non-local-only setting, and a key; cloud failures
  fall back to local analysis.
- Versioned analysis cache keyed by content hash, provider, model, and prompt version.
- Optional local `fastembed` embeddings using `BAAI/bge-small-en-v1.5`, stored as float32
  blobs in SQLite.
- FTS5 full-text search over path, title, author, category, summary, and full body, ranked
  with field-weighted BM25.
- Semantic search with cosine similarity and optional local query embeddings.
- Hybrid search with reciprocal-rank fusion (`k = 60`) and graceful fallback when vectors
  are unavailable.
- Explainable results: text rank, BM25, snippet, semantic rank, cosine, and RRF score.
- Tag and category filters applied consistently across text and semantic candidates.

### Organization and desktop interface

- User and AI tag provenance with normalization and global rename/merge.
- Exact SHA-256 duplicate groups, report-only.
- Related documents ranked by shared tags and local cosine similarity.
- Saved-search collections storing query, tags, category, and mode.
- PySide6 window with debounced search, filters, mode selection, result table,
  details/evidence pane, related documents, and tools for tags, duplicates, and collections.
- Background `QThreadPool` workers for search, indexing, related lookup, and tag changes;
  stale responses are discarded by request ID.
- CLI covering indexing, search, tags, duplicates, related documents, collections, daemon,
  GUI, AI configuration, diagnostics, backups, and status.

### Reliability and maintenance

- Persistent SQLite job queue with atomic claims, active-job coalescing, rerun requests,
  retries with backoff, cancellation, and crash recovery.
- `watchdog`/inotify watcher with per-path debounce and file-stability checks.
- Startup and periodic reconciliation, plus immediate incremental scans for new directories.
- Single-instance daemon lock and graceful SIGINT/SIGTERM shutdown.
- `doctor` checks for SQLite integrity, schema, FTS coverage, vectors, orphan tags,
  categories, missing files, jobs, and xattr support.
- Safe `doctor --repair` for derived data with an automatic pre-repair backup.
- Online SQLite backups with integrity verification, listing, retention, and pruning.

## Architecture

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
        │     └── discover_changes() at startup and periodically
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
```

Key modules:

| Module | Responsibility |
| --- | --- |
| `config.py` | XDG paths, validated TOML settings, logging, secret redaction. |
| `db.py` | Connections, migrations, CRUD, jobs, FTS, embeddings, collections, backups. |
| `extractor.py` | TXT/Markdown/PDF/DOCX extraction and excerpts. |
| `scanner.py` | Discovery, exclusions, hashing, stability, incremental indexing. |
| `analysis.py` | Local heuristics, Gemini JSON analysis, rate limit, cache, fallback. |
| `credentials.py` | Environment/keyring key lookup without exposing secrets. |
| `embeddings.py` | Lazy fastembed provider, float32 packing, persistence. |
| `search.py` | FTS5/BM25, cosine ranking, RRF, filters, snippets, explanations. |
| `related.py` | Shared-tag and vector-based related-document ranking. |
| `tags.py` | Tag normalization, provenance, mutation, xattr synchronization. |
| `xattr_store.py` | Safe probe/read/write/remove for `user.smartfs.*`. |
| `jobs.py` | Durable queue, claims, retries, cancellation, worker pipeline. |
| `watcher.py` | Inotify events, debounce, filtering, self-write suppression. |
| `daemon.py` | Watcher, worker pool, reconciliation, shutdown handling. |
| `doctor.py` | Integrity checks and safe repair of derived data. |
| `gui.py` | Main PySide6 window, table model, workers, details, related panel. |
| `gui_dialogs.py` | Tag manager, duplicate viewer, saved-search collections. |
| `cli.py` | Command-line interface and output formatting. |

## Indexing Lifecycle

```text
discover path
  → exclusion / supported-extension check
  → wait for stable size and mtime
  → SHA-256 content hash
  → reuse cached extraction / analysis / embedding where possible
  → extract text and document metadata
  → local or consented Gemini analysis
  → optional local embedding
  → SQLite transaction: file row + tags + FTS row + vector
  → commit
  → mirror selected metadata to user.smartfs.* xattrs
  → mark job done
```

The database commit always precedes the xattr write. If xattr mirroring fails, the index is
still correct and the failure is reported. The reverse order could expose metadata that was
never committed.

## Data Model

Schema version 5 contains:

| Table | Purpose |
| --- | --- |
| `files` | Path, inode/device, size, mtime, hash, status, category, summary, analysis provider. |
| `tags` | Normalized unique tag names. |
| `file_tags` | Many-to-many links with `user`/`ai` provenance and optional confidence. |
| `content_cache` | Extracted text and metadata keyed by hash and extraction version. |
| `analysis_cache` | Validated results keyed by hash, provider, model, and prompt version. |
| `embeddings` | Float32 vectors keyed by hash and model. |
| `files_fts` | FTS5 index keyed by file ID. |
| `jobs` | Durable queue with state, attempts, retry time, and cancellation flags. |
| `collections` | Named saved searches with query, tags, category, and mode. |

`INDEX_VERSION = 3` forces a one-time reindex when stored metadata semantics change.
`EXTRACTION_VERSION = 1` versions extraction cache entries. Migrations run in order inside
transactions and are backed up before an existing database is modified.

## Search Design

### Full text

FTS5 indexes paths, titles, authors, categories, summaries, and full extracted text. BM25
weights are `path=8`, `title=5`, `author=1`, `category=2`, `summary=2`, `body=1`. Queries are
tokenized and quoted before use. SmartFS first requires all meaningful terms, then falls back
to any-term matching if there are no results.

### Semantic

When embeddings are enabled, documents are embedded locally and stored by content hash.
Queries are embedded at search time. SmartFS retains positive cosine similarities and
excludes zero-length or dimensionally inconsistent vectors.

### Hybrid

Reciprocal-rank fusion combines text and semantic rankings:

```text
RRF(d) = Σ 1 / (60 + rank_i(d))
```

This avoids comparing incompatible BM25 and cosine scales. Results are ordered by fused
score, then modification time, then path. `auto` mode uses fusion when vectors are available
and otherwise returns text results with a note. Explicit `semantic` mode reports a clear error
when embeddings or vectors are missing.

## AI Analysis and Privacy

Local heuristic analysis is always available and requires no network or key. Gemini is
optional and gated by all of the following:

1. analysis is enabled;
2. consent is true;
3. `local_only` is false; and
4. a key is available.

Keys are read from `GEMINI_API_KEY`, `GOOGLE_API_KEY`, or the system keyring
(`smartfs` / `gemini-api-key`). They are never written to the SmartFS configuration or logs.
`ai status` reports only the source. API-key-shaped strings and active environment values are
redacted from logged errors.

Cloud requests contain a bounded text excerpt (12,000 characters by default), filename, MIME
type, title, author, page count, and truncation flag — not the original file. The default
model is `gemini-3.5-flash-lite`. Output is parsed as JSON and validated with Pydantic; any
failure falls back to local analysis. A sliding-window limiter defaults to 30 requests per
minute.

Embeddings are local-only, disabled by default, and stored in SQLite. They are never sent to
Gemini and are not mirrored to xattrs.

## Linux Extended Attributes

SmartFS mirrors metadata using the `user.*` namespace:

| Attribute | Value |
| --- | --- |
| `user.smartfs.category` | Document category. |
| `user.smartfs.tags` | Sorted union of user and AI tags. |
| `user.smartfs.summary` | One-line summary, bounded to 512 UTF-8 bytes. |
| `user.smartfs.hash` | SHA-256 content identity. |
| `user.smartfs.version` | Index metadata version. |

Support is probed by writing, reading, and removing a temporary probe attribute. Unsupported
filesystems (`ENOTSUP`, `EOPNOTSUPP`, `EPERM`, `EACCES`, `ENOSPC`, `E2BIG`, `EROFS`) cause a
graceful SQLite-only fallback. ext4, XFS, and Btrfs normally support user xattrs. Writing an
xattr changes ctime but not mtime and never modifies file contents.

## Organization Tools

- **Tags:** normalized, case-folded, bounded to 64 characters, with `user` and `ai`
  provenance. Rename refuses to overwrite an existing target; merge preserves the union.
- **Duplicates:** exact SHA-256 groups. The view is report-only and never deletes files.
- **Related documents:** shared tags plus local cosine similarity:
  `score = shared_tag_count + 2 × cosine`.
- **Collections:** named saved searches storing criteria, not copied file sets.

## Storage Locations

Default Linux XDG paths:

| Data | Path |
| --- | --- |
| Configuration | `~/.config/smartfs/config.toml` |
| SQLite index and job queue | `~/.local/share/smartfs/smartfs.db` |
| Backups | `~/.local/share/smartfs/backups/` |
| Embedding model cache | `~/.cache/smartfs/fastembed/` |
| Daemon lock | `~/.local/share/smartfs/daemon.lock` |
| Logs | `~/.local/state/smartfs/log/smartfs.log` |

`$SMARTFS_HOME` maps all locations under one root:

```text
$SMARTFS_HOME/config/config.toml
$SMARTFS_HOME/data/smartfs.db
$SMARTFS_HOME/data/backups/
$SMARTFS_HOME/cache/fastembed/
$SMARTFS_HOME/logs/smartfs.log
```

Document contents are never written to logs. Logs contain paths, hashes, sizes, error types,
job IDs, and command events.

## Platform Support

| Platform | Status |
| --- | --- |
| Linux | Fully supported. Uses inotify and Linux user xattrs. |
| Windows via WSL2 | Fully supported inside the Linux environment. Native Windows is not supported because SmartFS imports Linux-specific `fcntl` and xattr APIs. |
| macOS | CLI and GUI can run, but Linux xattrs, inotify, and `getfattr` are unavailable. Use Linux or WSL2 for the graded demonstration. |

See the [setup guides](setup/README.md) for platform-specific instructions.

## Quality and Testing

The final checkpoint has **111 passing tests** covering extraction, configuration,
credentials, database migrations, FTS5/BM25, semantic ranking, RRF, scanner behavior,
watcher debounce, jobs, xattr round-trips, CLI workflows, GUI models/dialogs/workers,
related documents, collections, doctor repairs, and backups. Ruff checks and formatting
pass, and the package builds as a wheel and source distribution for 0.7.0.

Gemini tests use injected fake clients, so the automated suite needs no network or real key.
Live validation was also performed with `gemini-3.5-flash-lite`; it confirmed structured
analysis, cache reuse, and zero analysis failures. Real local embedding inference and an
offscreen GUI smoke test were also verified. A formal large-scale performance benchmark was
not conducted.

## Limitations

- No OCR for scanned PDFs or images.
- Exact-hash duplicate detection only; no near-duplicate clustering.
- Collections store search criteria, not materialised result sets.
- The daemon is foreground-only; no systemd user unit is included.
- Single-user desktop design; no server or multi-user mode.
- Semantic scoring currently loads stored vectors and compares them in Python.
- The macOS and native Windows paths are not equivalent to the Linux feature set.

## Phase Roadmap

1. **Phase 1 (complete):** database, traversal, extraction, xattr store, user tags, CLI.
2. **Phase 2 (complete):** watchdog/inotify, debounce, stability checks, persistent queue,
   retries, crash recovery, reconciliation.
3. **Phase 3 (complete):** local and consented Gemini analysis, keyring, caching, fallback,
   optional local embeddings.
4. **Phase 4 (complete):** FTS5 BM25, semantic search, RRF fusion, snippets, filters,
   explainable results.
5. **Phase 5 (complete):** PySide6 GUI with debounced hybrid search, details, filters, and
   non-blocking workers.
6. **Phase 6 (complete):** tag rename/merge UI, duplicate groups, saved searches, related
   documents.
7. **Phase 7 (complete):** doctor/repair, backup commands, full test suite, project report,
   viva guide, and platform setup documentation.

## License

The project metadata declares the MIT license. Add a `LICENSE` file before publishing if the
repository does not already include one.
