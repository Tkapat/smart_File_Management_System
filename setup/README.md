# SmartFS Setup Guides

SmartFS is developed and graded as a **Linux-native** application. Choose the guide that
matches your machine:

| Platform | Guide | Support level |
| --- | --- | --- |
| Linux | [Linux setup](linux.md) | Full support: inotify and `user.smartfs.*` xattrs. |
| Windows | [Windows setup](windows.md) | Full functionality through WSL2. Native Windows is not supported. |
| macOS | [macOS setup](macos.md) | CLI/GUI can run, but Linux xattrs and inotify are not native; use Linux or WSL2 for the graded demo. |

## Gemini API key

Each guide includes the same key setup because every user must use their **own** Gemini API
key. Create one at <https://aistudio.google.com/apikey>. Never commit a key to Git, paste it
into `config.toml`, or include it in screenshots, reports, or chat messages. SmartFS can use
the operating-system keyring or the `GEMINI_API_KEY` / `GOOGLE_API_KEY` environment variables.

The default model is `gemini-3.5-flash-lite`. Cloud analysis is optional and remains disabled
until you explicitly enable it. Local analysis, local embeddings, full-text search, and
semantic search work without Gemini.

## After setup

All guides finish with the same workflow:

```bash
uv run smartfs init
uv run smartfs ai set-key
uv run smartfs ai enable --embeddings
uv run smartfs ai status
mkdir -p ~/SmartFS-Demo
uv run smartfs scan ~/SmartFS-Demo
uv run smartfs gui
```

For live updates, run `uv run smartfs daemon` in a second terminal.
