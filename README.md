# Quick Itch Updater

A Windows desktop app that checks the itch.io asset packs you choose and keeps the latest downloads in one place.

**[Download for Windows x64](https://github.com/myric227/Quick-Itch-Updater/releases/latest)** · Standalone executable with Python bundled.

## Quick start

1. Open the app and enter your personal itch.io API key in **Settings**. Windows Credential Manager stores the key.
2. Turn on **Tracking** for the packs you care about, then click **Check for Updates**. The app also checks those packs on startup.
3. Click **Download Updates**, select the files you want, then use **Open Download Folder** to find them in Explorer.

<details>
<summary>Library and download options</summary>

- Search by pack title, creator, or URL. Click a column header to sort. **Needs attention** shows available downloads, missing files, and failed checks.
- Double-click a pack name to open its itch.io page. Use **Manage Files** to hide individual uploads. New uploads appear for review automatically.
- Extract-only is the default. Future updates replace the previous managed copy after verification. Local edits and older dated folders are preserved. Use **Pack Settings** to choose a folder or keep compressed archives instead.
- **Retry Failed Downloads** retries eligible files from the current session. If you own a pack through a bundle, claim it on itch.io before refreshing the library.
- Classic web uploads use file metadata for update detection. A same-size replacement can be missed when itch.io reports no metadata change.

</details>

<details>
<summary>Run, test, or build from source</summary>

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m quick_itch
```

After setup, you can also double-click **Open Quick Itch Updater.cmd** to launch from source.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm quick-itch-updater.spec
```

The executable is written to `dist/`.

</details>

App icon sprites by [Ansimuz](https://ansimuz.itch.io/gothicvania-patreon-collection).
