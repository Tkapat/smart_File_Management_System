# SmartFS — Viva and Demonstration Guide

This document is the short technical companion for demonstrating SmartFS. It covers the
architecture, the data model, the graded Linux features, a complete live demo, and likely
questions.

## 1. What SmartFS is

SmartFS is a Linux-native semantic file manager. It indexes directories that the user
explicitly opts into, extracts searchable text, produces local metadata and optional Gemini
metadata, mirrors selected fields into Linux extended attributes, and exposes the index
through a CLI and a PySide6 desktop application.

It never deletes, renames, or moves user files. SQLite is authoritative; xattrs are a
visible mirror for other Linux tools.

## 2. Technology choices

| Concern | Choice | Why |
| --- | --- | --- |
| Language | Python 3.12+ | Readable for the viva; strong extraction libraries. |
| Local metadata | `user.smartfs.*` xattrs | Linux-native, visible through `getfattr`. |
| Index | SQLite with WAL | Zero-server, transactional, FTS5 available, easy backups. |
| Full-text | SQLite FTS5 + BM25 | Standard ranked search without a service. |
| Semantics | `fastembed` + `BAAI/bge-small-en-v1.5` | Local, small, offline embeddings. |
| Cloud AI | Optional Google Gemini | Structured JSON tags/summaries only after consent. |
| Watching | `watchdog`/inotify + debounce | Live updates without polling the whole tree. |
| Work queue | SQLite jobs + worker threads | Crash-recoverable and bounded. |
| GUI | PySide6 + `QThreadPool` | Native Qt UI; search/scan/index never block the event loop. |

## 3. Architecture

```text
                         ┌──────────────────────────────┐
 mkdir/edit/rm ─────────►│ watchdog + debounce/stability│
                         └──────────────┬───────────────┘
                                        │ jobs
                         ┌──────────────▼───────────────┐
                         │ SQLite queue (WAL)           │
                         │ atomic claims / retries      │
                         └──────────────┬───────────────┘
                                        │
                    ┌───────────────────▼────────────────────┐
                    │ WorkerPool / PipelineProcessor          │
                    │  hash → extract → analyze → embed       │
                    │  → DB commit → FTS row → xattr mirror   │
                    └───────────────────┬────────────────────┘
                                        │
        ┌───────────────────────────────┼───────────────────────────────┐
        │                               │                               │
┌───────▼────────┐            ┌─────────▼─────────┐          ┌──────────▼─────────┐
│ CLI (argparse) │            │ PySide6 GUI       │          │ SQLite index       │
│ scan/find/tags │            │ search/scan/tools │          │ files/tags/FTS/vec │
└───────┬────────┘            └─────────┬─────────┘          └──────────┬─────────┘
        │                               │                               │
        └───────────────► search.py / related.py ◄──────────────────────┘
                         BM25 + cosine + RRF
```

### Main modules

| File | Responsibility |
| --- | --- |
| `config.py` | XDG/`SMARTFS_HOME` paths, validated settings, logging. |
| `db.py` | Connections, migrations, backups, CRUD, jobs, FTS, embeddings, collections. |
| `extractor.py` | TXT/Markdown/PDF/DOCX text extraction and excerpting. |
| `scanner.py` | Recursive discovery, hashing, incremental indexing, rename/missing handling. |
| `analysis.py` | Local heuristics and consented Gemini JSON analysis with fallback/cache. |
| `embeddings.py` | Lazy local fastembed provider and float32 SQLite storage. |
| `search.py` | FTS5 text search, semantic cosine, RRF fusion, filters, explanations. |
| `related.py` | Related documents from shared tags and local vectors. |
| `tags.py` | Normalization, provenance, rename/merge, xattr synchronization. |
| `jobs.py` | Persistent queue, claims, retries, cancellation, worker pipeline. |
| `daemon.py` / `watcher.py` | Foreground daemon and inotify reconciliation. |
| `gui.py` / `gui_dialogs.py` | Non-blocking desktop UI, tag manager, duplicates, collections. |
| `doctor.py` | Integrity checks and safe repair for derived data. |
| `xattr_store.py` | Support probe, read/write, bounded UTF-8 values, suppression. |

## 4. Data model

Schema version 5:

- `files` — path, inode/device, size, mtime, SHA-256, status, category, summary, title,
  author, excerpt, analysis provider/model, first/last seen.
- `tags` and `file_tags` — normalized tag names plus per-file provenance (`user` or `ai`).
- `content_cache` — extracted text keyed by `(sha256, extraction_version)`.
- `analysis_cache` — validated structured results keyed by hash/provider/model/prompt version.
- `embeddings` — float32 vector blob keyed by `(sha256, model)`.
- `files_fts` — standalone FTS5 table keyed by file id: path, title, author, category,
  summary, full body.
- `jobs` — durable queue with active-job uniqueness, retries, rerun and cancellation flags.
- `collections` — named saved searches (query, tags JSON, category, mode).

Migrations use `PRAGMA user_version`, run in order, and create a timestamped backup before
modifying an existing database. The current `INDEX_VERSION` is 3; older rows are reindexed
once so FTS bodies contain full extracted text.

## 5. Graded Linux features

### 5.1 Extended attributes

SmartFS writes these `user.*` attributes after the corresponding SQLite commit:

```text
user.smartfs.category
user.smartfs.tags
user.smartfs.summary
user.smartfs.hash
user.smartfs.version
```

Values are sanitized and size-bounded. Support is probed by writing, reading, and removing a
temporary attribute. On unsupported filesystems, SmartFS falls back to SQLite-only mode and
reports it; it does not crash or lose metadata. A suppression set prevents the watcher from
reacting to SmartFS's own xattr writes.

Verification:

```bash
getfattr -d -m user.smartfs ~/SmartFS-Demo/thermo.txt
```

### 5.2 AI-generated metadata

Local analysis always works without network access: deterministic category, tags, summary,
topics, and confidence. Gemini is opt-in only when all of these are true:

1. `analysis_enabled = true`
2. `consent = true`
3. `local_only = false`
4. A key exists in `GEMINI_API_KEY`, `GOOGLE_API_KEY`, or the keyring.

Only the configured text excerpt (12,000 characters by default), filename, MIME type,
title, author, page count, and truncation flag are sent. The default model is
`gemini-3.5-flash-lite`; it can be changed in `config.toml`. Keys are never written to config
or logs. Cloud failures fall back to local analysis.

### 5.3 Search

- **Text:** FTS5 BM25 with field weights favoring path/title. All terms are required first,
  then any-term matching is used as a fallback.
- **Semantic:** local query embedding compared by cosine to stored document vectors.
- **Hybrid:** reciprocal-rank fusion, `1 / (60 + rank)` per source, so BM25 and cosine
  scales do not need to be comparable.
- **Explanations:** mode, safe FTS query, source ranks, BM25, cosine, RRF score, and snippet.

Queries are tokenized and quoted before entering FTS5, so input cannot inject FTS syntax.
Queries with no tokenizable characters fall back to literal filtering.

## 6. Live demonstration (about 8 minutes)

### Setup

```bash
cd smartfs
uv sync --all-extras

mkdir -p ~/SmartFS-Demo
uv run smartfs init
uv run smartfs watch add ~/SmartFS-Demo
```

Create three documents:

```bash
cat > ~/SmartFS-Demo/thermo.txt <<'EOF'
Thermodynamics lecture notes.
Entropy always increases in an isolated system.
The second law governs energy transfer and heat engines.
EOF

cat > ~/SmartFS-Demo/bread.txt <<'EOF'
Sourdough bread recipe. Mix flour, water, salt, and starter.
Bake in a hot oven until the crust is dark.
EOF

cat > ~/SmartFS-Demo/network.txt <<'EOF'
Networking lecture notes on TCP sockets, packets, DNS, and protocol layers.
EOF
```

### Index and xattrs

```bash
uv run smartfs scan ~/SmartFS-Demo
uv run smartfs tag add ~/SmartFS-Demo/thermo.txt physics
uv run smartfs tags ~/SmartFS-Demo/thermo.txt
getfattr -d -m user.smartfs ~/SmartFS-Demo/thermo.txt
```

Point out the user tag, locally generated AI tags, category, summary, content hash, and
index metadata version in the xattr output.

### Full-text and explanations

```bash
uv run smartfs find "entropy isolated system" --mode text --explain
uv run smartfs find --tag physics --explain
```

### Local semantic and hybrid (no API key)

```bash
uv run smartfs ai embeddings enable
uv run smartfs scan --force ~/SmartFS-Demo
uv run smartfs find "energy in closed systems" --mode semantic --explain
uv run smartfs find "thermodynamics energy" --mode hybrid --explain
```

The first embedding run downloads the model into the SmartFS cache. Inspect the
`semantic rank`, `cosine`, and fused `RRF` lines in the explanation.

### Organization tools

```bash
cp ~/SmartFS-Demo/thermo.txt ~/SmartFS-Demo/thermo-copy.txt
uv run smartfs scan ~/SmartFS-Demo
uv run smartfs duplicates

uv run smartfs tag rename physics natural-science
uv run smartfs tag merge natural-science science
uv run smartfs related ~/SmartFS-Demo/thermo.txt

uv run smartfs collection save "Physics Review" \
  --tag science --category "lecture notes" --mode hybrid entropy energy
uv run smartfs collection list
```

### Daemon and GUI

Terminal 1:

```bash
uv run smartfs daemon
```

Terminal 2:

```bash
uv run smartfs gui
```

Show:

1. Text/hybrid mode and tag filters.
2. Selecting a result: evidence plus related documents.
3. **Tools → Manage tags…**: rename/merge and provenance counts.
4. **Tools → Duplicate content…**: report-only duplicate groups.
5. **Tools → Saved searches…**: load the collection saved earlier.
6. **Scan now** while the UI remains responsive.

### Maintenance and backup

```bash
uv run smartfs doctor
uv run smartfs backup create
uv run smartfs backup list
uv run smartfs backup prune --keep 5
uv run smartfs status
```

If time permits, delete FTS rows in SQLite and show `doctor` detecting them, then
`doctor --repair` rebuilding FTS and creating a pre-repair backup.

## 7. Design decisions to defend

- **SQLite is authoritative.** Xattrs are a mirror; a filesystem without xattr support
  still gets full tags and search.
- **Opt-in scanning.** Only configured directories are touched; `.git`, virtualenvs,
  secrets, keys, and partial downloads are excluded by default.
- **No destructive behavior.** Missing files are marked, not deleted; duplicates are
  reported, not removed; tag changes never edit file contents.
- **Secrets stay out of config.** Environment/keyring only, with redacted errors.
- **Local-first AI.** Heuristics plus local embeddings work offline; cloud calls require
  explicit consent and gracefully fall back.
- **Idempotent indexing.** SHA-256 + extraction/analysis caches avoid repeated reads and
  Gemini calls; inode/hash reconciliation preserves metadata across renames.
- **Crash recovery.** Jobs and file state are durable; active-job uniqueness avoids
  duplicate work; xattrs are written only after the DB commit.
- **UI responsiveness.** Search, indexing, related lookup, and tag maintenance run in
  workers with independent SQLite connections; stale search responses are discarded.
- **Explainable search.** The demo can show exactly which ranks and scores produced a
  result instead of treating search as a black box.

## 8. Limitations

- OCR for scanned PDFs and images is not implemented.
- Duplicate detection is exact-hash only; similarity clustering is not included.
- Collections save search criteria, not copied file sets.
- Related ranking is heuristic (tags + local vectors); it is not a knowledge graph.
- Gemini output is validated but still model-generated; users can disable cloud analysis.
- The GUI scan and daemon can both index concurrently; SQLite serializes writes, but for
  heavy trees the daemon alone is preferable.
- The daemon is foreground-only by design for transparent demonstrations.

## 9. Verification

```bash
uv sync --all-extras
uv run pytest -q          # 111 tests at the final checkpoint
uv run ruff check src tests
uv run ruff format --check src tests
```

Qt tests run with `QT_QPA_PLATFORM=offscreen`. Gemini tests use injected fakes, so the
suite needs no network or real key. The manual demo verifies real fastembed inference and
`getfattr` output on a filesystem with user-xattr support.

## 10. Likely questions

**Why not just `find` or `grep`?**
They scan content repeatedly, lack ranked metadata/search, cannot combine tags with
semantic similarity, and do not maintain a durable index or xattr mirror.

**Why keep both SQLite tags and xattrs?**
SQLite supports provenance, search, collections, and atomic updates. Xattrs make the
metadata visible to standard Linux tools and satisfy the course requirement.

**Why RRF instead of adding BM25 and cosine directly?**
Their scales are not comparable. RRF uses ranks, so it is stable when one source returns
very large or very small raw scores.

**What happens when Gemini is unavailable?**
The pipeline catches the failure, records analysis metrics, and uses deterministic local
analysis. Indexing continues.

**What happens if a file disappears?**
It is marked `missing`, metadata and tags are retained, and a later scan restores it if it
reappears. SmartFS does not delete the row automatically.

**How are secrets protected?**
Keys come from environment variables or the OS keyring. Config serialization and errors
redact API-key-shaped strings; document contents are never logged.

**Does the GUI block while embedding?**
No. Search and related lookup run in `QThreadPool` workers with their own SQLite
connections. Model loading and vector computation happen off the GUI thread.
