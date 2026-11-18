# Quick Itch Updater

Windows desktop app for selectively tracking itch.io asset packs with account-linked download keys. Download the Windows x64 executable from Releases and double-click it to run. The executable includes Python and the app's dependencies.

## Run from source

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m quick_itch
```

After setup, double-click **Open Quick Itch Updater.cmd** to launch from source.

1. In **Settings**, enter a personal itch.io API key and save. The app stores it in Windows Credential Manager and imports library metadata into SQLite.
2. Search by pack title, creator, or public URL. Click a column header to sort and click it again to reverse the order. Double-click a pack name or choose **Open Pack Page** to see it in your browser. Check **Tracking** for each pack to follow. The app checks enabled packs on startup and when you click **Check for Updates**.
3. Click **Download Updates** and check the files you want to download. Use **Hide Checked Files** to leave specific uploads out of update alerts and downloads. Select a pack and use **Manage Files** to restore hidden uploads. New upload IDs are included automatically.
4. Each upload uses a folder named after the file, such as `GameAssets/Creator/Pack [ID]/Legacy Collection plus`. Future updates replace the prior managed copy after verification. Earlier dated folders and folders with local edits are preserved. Use **Pack Settings** to override the pack location. Choose **Keep the original download only** to keep archives compressed. Password-protected or unsupported archives are kept as downloaded.
5. Open **Download History** to access current downloads and any older copies. The app records the download's SHA-256 and a manifest of extracted paths, sizes, and checksums so it can detect missing files, compare revisions, and recognize local edits without saving a second copy of the ZIP.

Set **Theme** in **Settings** to **Follow Windows**, **Light**, or **Dark**. Click a tracking cell or file row to toggle its checkbox, or press Space with the cell or row focused.

Use **Needs attention** to see packs with downloads ready, possible updates, missing files, or failed checks. Select a pack and click **Open Download Folder** to see its files in Explorer. **Retry Failed Downloads** appears after a batch and retries eligible failed files from the current session.

Classic web uploads use file metadata to identify potential changes. If itch.io supplies no distinguishing metadata, the app may miss a same-size replacement. Claim a bundle pack on itch.io before refreshing the library.

App icon artwork uses sprites from [Ansimuz's Legacy Collection](https://ansimuz.itch.io/gothicvania-patreon-collection).

## Build and test

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm quick-itch-updater.spec
```

The standalone executable is written to `dist\`.
