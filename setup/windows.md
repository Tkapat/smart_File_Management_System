# SmartFS on Windows

SmartFS is a Linux-native application. It relies on Linux `user.*` extended attributes,
inotify, and `fcntl` file locking, so **native Windows is not supported**. The supported way
to use SmartFS on Windows is **WSL2 with Ubuntu**, which provides a real Linux environment
inside Windows.

This guide uses Windows 11. Windows 10 can run the CLI in WSL2, but the GUI requires WSLg or
a separate Linux desktop.

## 1. Install WSL2

Open PowerShell as Administrator:

```powershell
wsl --install -d Ubuntu
```

Restart Windows if prompted, launch **Ubuntu** from the Start menu, and create your Linux
username and password.

Verify inside Ubuntu:

```bash
uname -a
```

A `Linux ... microsoft-standard-WSL2` result confirms WSL2.

## 2. Install Linux prerequisites

Inside the Ubuntu terminal:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-dev build-essential git curl attr
```

Install `uv`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"
uv --version
```

## 3. Get the project

Keep the repository and indexed documents in the WSL Linux filesystem, for example
`~/smartfs` and `~/SmartFS-Demo`. Do not use `/mnt/c/...` for the graded xattr demo:
Windows drives mounted through WSL do not provide the same Linux xattr and event behavior,
and performance is worse.

```bash
cd ~
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git smartfs
cd smartfs
```

If the project is already on Windows, copy it into WSL or clone it again rather than running
it from `/mnt/c`.

## 4. Install Python and dependencies

```bash
uv python install 3.12
uv sync --all-extras
```

For CLI-only use, `uv sync --extra ai` is enough. Use `--all-extras` for the GUI and tests.

## 5. Initialise SmartFS

```bash
uv run smartfs init
```

The default configuration is local-only. It stores data in the WSL home directory.

## 6. Set up your own Gemini API key

1. Open <https://aistudio.google.com/apikey> in a browser.
2. Sign in with your own Google account.
3. Create a new API key.
4. Never commit the key or include it in a screenshot/report.

Try the keyring first:

```bash
uv run smartfs ai set-key
uv run smartfs ai status
```

A minimal WSL installation may not have a Secret Service/keyring backend. If `set-key` fails,
use a session environment variable instead:

```bash
read -rsp "Gemini API key: " GEMINI_API_KEY; echo
export GEMINI_API_KEY
uv run smartfs ai enable --embeddings
uv run smartfs ai status
```

For a persistent environment variable, add it to your shell profile only if you understand
that shell profiles are plain text. Do not use a shared key. If a key is exposed, revoke it in
Google AI Studio and create a new one.

The default model is `gemini-3.5-flash-lite`. Cloud analysis is optional; local analysis and
local embeddings work without a key.

## 7. Create and index documents

```bash
mkdir -p ~/SmartFS-Demo
printf 'Networking lecture notes on TCP, DNS, and protocol layers.\n' \
  > ~/SmartFS-Demo/network.txt

uv run smartfs watch add ~/SmartFS-Demo
uv run smartfs scan ~/SmartFS-Demo
```

Access the files from Windows Explorer through:

```text
\\wsl$\Ubuntu\home\<your-linux-user>\SmartFS-Demo
```

## 8. Verify Linux metadata

```bash
uv run smartfs tag add ~/SmartFS-Demo/network.txt networking
getfattr -d -m user.smartfs ~/SmartFS-Demo/network.txt
```

This works because the files are stored in the WSL ext4 filesystem, not on `/mnt/c`.

## 9. Search and GUI

```bash
uv run smartfs find "network protocols" --explain
uv run smartfs find "packets and DNS" --mode semantic --explain
uv run smartfs gui
```

On Windows 11, WSLg provides the GUI window automatically. On Windows 10, use the CLI or run
a Linux desktop/VM.

For live updates:

```bash
uv run smartfs daemon
```

## 10. Maintenance

```bash
uv run smartfs status
uv run smartfs doctor
uv run smartfs doctor --repair
uv run smartfs backup create
uv run smartfs backup list
```

## 11. Native Windows limitations

Do **not** expect the following to work natively without WSL2:

- `user.smartfs.*` extended attributes;
- Linux inotify events;
- `fcntl` daemon locking;
- `getfattr` verification.

The project imports Linux-specific modules, so native Windows is intentionally unsupported.
Use WSL2 for the complete feature set and for the graded demonstration.

## Troubleshooting

- **`wsl --install` fails** — enable virtualization in BIOS/UEFI and install pending Windows
  updates.
- **GUI does not appear** — use Windows 11 with WSLg, or use the CLI.
- **xattrs unsupported** — confirm files are under `/home/...`, not `/mnt/c/...`.
- **Keyring unavailable** — use `GEMINI_API_KEY` for the session.
- **Model download is slow** — the first embedding run downloads the model; later runs use
  the cache.
