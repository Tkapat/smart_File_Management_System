# SmartFS on Linux

This is the fully supported platform. SmartFS uses Linux extended attributes
(`user.smartfs.*`) and inotify through `watchdog`. The instructions below use Fedora as the
primary example and include Debian/Ubuntu and Arch equivalents.

## 1. Prerequisites

- A modern Linux distribution with Python 3.12 or later.
- `git`, `curl`, and a C build toolchain for optional Python packages.
- `attr` for `getfattr`/`setfattr` verification.
- A filesystem that supports user extended attributes for the xattr mirror. ext4, XFS, and
  Btrfs normally work. FAT/exFAT and some network mounts do not.
- Optional: a desktop session for the PySide6 GUI.

### Fedora

```bash
sudo dnf install -y python3 python3-devel gcc redhat-rpm-config git curl attr
```

### Debian / Ubuntu

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-dev build-essential git curl attr
```

### Arch Linux

```bash
sudo pacman -S --needed python git curl base-devel attr
```

## 2. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Restart the shell, or source the installer's environment file if prompted. Verify:

```bash
uv --version
```

## 3. Get the project

```bash
cd /path/to/smartfs
# or clone your repository:
# git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git smartfs
```

## 4. Install Python and dependencies

```bash
uv python install 3.12
uv sync --all-extras
```

`--all-extras` installs the AI, embedding, GUI, test, and development dependencies. A
minimal text-only installation can use `uv sync`, but the full demonstration needs
`--all-extras`.

## 5. Initialise SmartFS

```bash
uv run smartfs init
```

This creates the configuration, database, cache, and log directories. The default mode is
local-only and makes no network calls.

## 6. Set up your own Gemini API key

1. Open <https://aistudio.google.com/apikey>.
2. Sign in with your own Google account.
3. Create a new API key for this project.
4. Do not share the key or commit it to Git.

Store it in the desktop keyring with a hidden prompt:

```bash
uv run smartfs ai set-key
```

The key is stored under keyring service `smartfs`, username `gemini-api-key`. If the
keyring backend is unavailable, use an environment variable for the current shell instead:

```bash
read -rsp "Gemini API key: " GEMINI_API_KEY; echo
export GEMINI_API_KEY
```

Do not place the key in `config.toml` or a committed `.env` file.

Enable cloud analysis and local embeddings:

```bash
uv run smartfs ai enable --embeddings
uv run smartfs ai status
```

`ai status` reports only whether a key exists and where it came from; it never prints the
key. The default cloud model is `gemini-3.5-flash-lite`. Local embeddings use
`BAAI/bge-small-en-v1.5` and download on first use.

If you want local-only analysis, skip `ai set-key` and run:

```bash
uv run smartfs ai disable
```

## 7. Create and index documents

```bash
mkdir -p ~/SmartFS-Demo
printf 'Thermodynamics lecture notes.\nEntropy always increases in an isolated system.\n' \
  > ~/SmartFS-Demo/thermo.txt

uv run smartfs watch add ~/SmartFS-Demo
uv run smartfs scan ~/SmartFS-Demo
```

Add your own PDF, DOCX, Markdown, or text files to `~/SmartFS-Demo` and run `scan` again.
Use `--force` to re-index unchanged files.

## 8. Verify Linux metadata

```bash
uv run smartfs tag add ~/SmartFS-Demo/thermo.txt physics
uv run smartfs tags ~/SmartFS-Demo/thermo.txt
getfattr -d -m user.smartfs ~/SmartFS-Demo/thermo.txt
```

Expected attributes include:

```text
user.smartfs.category
user.smartfs.hash
user.smartfs.summary
user.smartfs.tags
user.smartfs.version
```

If the filesystem does not support user xattrs, SmartFS continues with SQLite-only metadata
and reports the fallback. For the graded xattr requirement, use ext4, XFS, or Btrfs.

## 9. Search and launch the GUI

```bash
uv run smartfs find "entropy isolated system" --explain
uv run smartfs find "energy in closed systems" --mode semantic --explain
uv run smartfs find "thermodynamics energy" --mode hybrid --explain

uv run smartfs gui
```

For live filesystem updates, run the daemon in another terminal:

```bash
uv run smartfs daemon
```

The GUI can also scan watched directories with **Tools → Scan now**.

## 10. Maintenance

```bash
uv run smartfs status
uv run smartfs doctor
uv run smartfs doctor --repair
uv run smartfs backup create
uv run smartfs backup list
uv run smartfs backup prune --keep 5
```

`doctor --repair` creates a pre-repair backup before rebuilding derived data. It never
deletes user documents.

## 11. Large watched trees

Raise the inotify watch limit if indexing many directories:

```bash
sudo tee /etc/sysctl.d/99-smartfs.conf >/dev/null <<'EOF'
fs.inotify.max_user_watches = 524288
EOF
sudo sysctl --system
```

## 12. Run the tests

```bash
uv run pytest --disable-warnings
uv run ruff check src tests
uv run ruff format --check src tests
```

## Troubleshooting

- **`getfattr: command not found`** — install the `attr` package.
- **xattrs unsupported** — move the watched directory to ext4, XFS, or Btrfs.
- **Gemini falls back to local** — check `uv run smartfs ai status`, consent, network, key,
  and the rate limit. The log contains non-secret error information.
- **Semantic search unavailable** — run `uv run smartfs ai enable --embeddings` and rescan.
- **GUI does not start** — install GUI extras with `uv sync --all-extras`.
- **Database busy** — wait for a running scan, repair, or backup to finish.
