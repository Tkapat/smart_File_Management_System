# SmartFS on macOS

SmartFS can run its Python CLI and PySide6 GUI on macOS, but it is designed for Linux. macOS
does not provide Linux inotify, the Linux `user.*` xattr namespace, or `getfattr`. Therefore:

- Use macOS for development, CLI search, local analysis, and the GUI.
- Use Linux or WSL2 for the graded xattr/inotify demonstration.
- Treat xattr mirroring on macOS as experimental; disable it if you want predictable
  SQLite-only behavior.

## 1. Prerequisites

- macOS 12 or later (Apple Silicon or Intel).
- Xcode Command Line Tools.
- Homebrew.
- Python 3.12 or later through `uv`.
- Optional: a desktop session for the GUI.

Install the command-line tools:

```bash
xcode-select --install
```

Install Homebrew if it is not already present:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Install the basic tools:

```bash
brew install uv git python@3.12
```

## 2. Get the project and install dependencies

```bash
cd /path/to/smartfs
# or: git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git smartfs
uv python install 3.12
uv sync --all-extras
```

## 3. Initialise SmartFS

```bash
uv run smartfs init
```

This creates the configuration and database in the macOS user directories.

## 4. Disable Linux-style xattr mirroring (recommended)

macOS xattrs are not the Linux `user.*` namespace and cannot be verified with `getfattr`.
Run `uv run smartfs status`, open the file shown on the **Config** line, and set:

```toml
xattr_enabled = false
```

SQLite remains the authoritative store for categories, tags, summaries, hashes, and search.
You can experiment with `xattr_enabled = true`, but it is not equivalent to the Linux
requirement. Use Linux/WSL2 for the xattr demonstration.

## 5. Set up your own Gemini API key

1. Open <https://aistudio.google.com/apikey>.
2. Sign in with your own Google account.
3. Create a new API key.
4. Never commit or share the key.

Store it in the macOS Keychain through the keyring:

```bash
uv run smartfs ai set-key
uv run smartfs ai status
```

If the keyring backend is unavailable, use a session variable:

```bash
read -rsp "Gemini API key: " GEMINI_API_KEY; echo
export GEMINI_API_KEY
```

Enable cloud analysis and local embeddings:

```bash
uv run smartfs ai enable --embeddings
uv run smartfs ai status
```

The default model is `gemini-3.5-flash-lite`. Local embeddings use
`BAAI/bge-small-en-v1.5` and download on first use. For local-only operation:

```bash
uv run smartfs ai disable
```

## 6. Create and index documents

```bash
mkdir -p ~/SmartFS-Demo
printf 'Sourdough bread recipe. Mix flour, water, salt, and starter.\n' \
  > ~/SmartFS-Demo/bread.txt

uv run smartfs watch add ~/SmartFS-Demo
uv run smartfs scan ~/SmartFS-Demo
```

## 7. Search and GUI

```bash
uv run smartfs find "bread recipe" --explain
uv run smartfs find "flour water salt" --mode semantic --explain
uv run smartfs gui
```

The watcher uses macOS filesystem events through `watchdog`, not Linux inotify. The GUI and
CLI search still work against the SQLite index. For live watching:

```bash
uv run smartfs daemon
```

The daemon lock uses `fcntl`, which is available on macOS. The inotify-specific behavior
described in the Linux guide is not available.

## 8. Maintenance

```bash
uv run smartfs status
uv run smartfs doctor
uv run smartfs doctor --repair
uv run smartfs backup create
uv run smartfs backup list
```

## 9. macOS limitations

- No `user.smartfs.*` xattr semantics equivalent to Linux.
- No `getfattr` verification; `xattr -l` is the macOS tool, but it is not the graded Linux
  interface.
- No inotify; the watcher uses a different backend.
- The GUI is supported, but the project is not officially tested on every macOS release.
- For the final viva/demo, use Linux or WSL2 so the xattr and inotify requirements are real.

## Troubleshooting

- **PySide6 installation fails** — confirm the macOS version and architecture, then retry
  `uv sync --all-extras`.
- **Keychain prompt appears** — allow access when storing the key with `ai set-key`.
- **xattr warnings** — set `xattr_enabled = false` as described above.
- **Semantic search unavailable** — run `uv run smartfs ai enable --embeddings` and rescan.
- **Model download fails** — check network access to the model host; later runs use the
  SmartFS cache.
