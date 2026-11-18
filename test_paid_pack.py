"""Interactive, local smoke test for one paid itch.io upload."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import tempfile
import tkinter as tk
from tkinter import ttk
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


API = "https://api.itch.io"
TIMEOUT = 30
REDIRECT_CODES = {301, 302, 303, 307, 308}


class TestError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = build_opener(NoRedirect)


def api_request(path, api_key, *, params=None, allow_download_link=False):
    url = f"{API}{path}"
    if params:
        url += "?" + urlencode(params)
    request = Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with OPENER.open(request, timeout=TIMEOUT) as response:
            payload = json.load(response)
    except HTTPError as error:
        with error:
            if allow_download_link and error.code in REDIRECT_CODES:
                location = error.headers.get("Location")
                if location:
                    return {"url": urljoin(url, location)}
            raise TestError(f"itch.io returned HTTP {error.code}.") from None
    if not isinstance(payload, dict):
        raise TestError("itch.io returned an unexpected response.")
    if payload.get("errors"):
        raise TestError("itch.io rejected the request. Check your API key and pack access.")
    return payload


def project_address(url):
    parts = urlsplit(url)
    return parts.hostname, parts.path.rstrip("/")


def find_owned_keys(api_key, pack_url):
    page = 1
    matches = []
    while True:
        params = {"page": page, "per_page": 100}
        result = api_request("/profile/owned-keys", api_key, params=params)
        if "owned_keys" in result:
            keys = result["owned_keys"]
        elif "ownedKeys" in result:
            keys = result["ownedKeys"]
        elif not result or result.get("page") == page:
            keys = None
        else:
            raise TestError(f"API key accepted. Unexpected owned-library response on page {page}.")

        # Accept empty JSON representations without discarding matches from earlier pages.
        if keys is None or keys == {} or keys == []:
            return matches
        if not isinstance(keys, list):
            raise TestError(f"API key accepted. Unexpected owned-key format on page {page}.")
        for key in keys:
            if project_address((key.get("game") or {}).get("url") or "") == pack_url:
                matches.append(key)
        page += 1


def list_uploads(api_key, keys):
    uploads = {}
    for key in keys:
        result = api_request(
            f"/games/{int(key['game_id'])}/uploads",
            api_key,
            params={"download_key_id": int(key["id"])},
        )
        if not isinstance(result.get("uploads"), list):
            raise TestError("itch.io did not return an upload list.")
        for upload in result["uploads"]:
            if upload.get("storage") in {"hosted", "build"} and not any(
                upload.get(field) for field in ("demo", "preorder")
            ):
                uploads.setdefault(int(upload["id"]), (upload, int(key["id"])))
    return list(uploads.values())


def open_file(url):
    for _ in range(6):
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            raise TestError("itch.io returned an invalid or non-HTTPS download link.")
        try:
            # The API can delegate a hosted upload to another provider. Each hop is credential-free.
            return OPENER.open(Request(url), timeout=TIMEOUT)
        except HTTPError as error:
            with error:
                location = error.headers.get("Location")
                if error.code not in REDIRECT_CODES or not location:
                    raise TestError(f"The file host returned HTTP {error.code}.") from None
                url = urljoin(url, location)
    raise TestError("The download exceeded the redirect limit.")


def download_file(api_key, upload, key_id):
    upload_id = int(upload["id"])
    result = api_request(
        f"/uploads/{upload_id}/download",
        api_key,
        params={"download_key_id": key_id},
        allow_download_link=True,
    )
    url = result.get("url")
    if not isinstance(url, str) or not url:
        raise TestError("itch.io did not return a download URL.")

    with open_file(url) as response:
        if response.status != 200 or response.headers.get_content_type() == "text/html":
            raise TestError("The file host did not return a downloadable file.")
        name = re.sub(r"[^a-zA-Z0-9._-]", "_", upload.get("filename") or "file")
        folder = Path(tempfile.mkdtemp(prefix="quick-itch-test-"))
        destination = folder / f"{upload_id}-{name[:120].rstrip('.')}"
        partial = folder / "download.part"
        digest = hashlib.sha256()
        received = 0
        announced_size = response.headers.get("Content-Length")
        expected = int(announced_size) if announced_size else None
        if upload.get("storage") == "hosted" and upload.get("size", 0) > 0:
            expected = upload["size"]
        try:
            with partial.open("xb") as file:
                while chunk := response.read(256 * 1024):
                    file.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
            if received == 0 or (expected is not None and received != expected):
                raise TestError("The file was empty or did not match the expected size.")
            partial.rename(destination)
        finally:
            partial.unlink(missing_ok=True)
            if not destination.exists():
                folder.rmdir()
    return destination, received, digest.hexdigest()


def check_pack(pack_url, api_key):
    url = urlsplit(pack_url.strip())
    if not (
        url.scheme == "https"
        and (url.hostname or "").endswith(".itch.io")
        and re.fullmatch(r"/[^/]+/?", url.path)
        and not (url.username or url.password or url.query or url.fragment)
    ):
        raise TestError("Enter the public pack URL, like https://creator.itch.io/pack-name.")
    if not api_key:
        raise TestError("Enter your personal itch.io API key.")

    try:
        profile = api_request("/profile", api_key)
    except TestError as error:
        raise TestError(f"Could not verify the API key. {error}") from None
    if not profile.get("user", {}).get("id"):
        raise TestError("itch.io did not return a profile for this key.")
    keys = find_owned_keys(api_key, project_address(url.geturl()))
    if not keys:
        raise TestError("API key accepted. Pack not found. Check the URL and confirm the purchase is linked or claimed.")

    uploads = list_uploads(api_key, keys)
    if not uploads:
        raise TestError("API key accepted. No eligible itch-hosted downloads were found for this pack.")
    return uploads


class PaidPackPrompt:
    def __init__(self, window):
        self.window = window
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.uploads = []
        self.saved_folder = None
        self.pack_url = tk.StringVar()
        self.key = tk.StringVar()
        self.reveal_key = tk.BooleanVar()
        self.key_count = tk.StringVar(value="0 characters")
        self.status = tk.StringVar(value="Paste the public pack URL and your personal API key.")

        window.title("Quick Itch Updater | Paid pack download test")
        window.minsize(570, 310)
        window.columnconfigure(0, weight=1)
        frame = ttk.Frame(window, padding=20)
        frame.grid(sticky="nsew")
        frame.columnconfigure(0, weight=1)

        ttk.Label(frame, text="Public itch.io pack URL").grid(sticky="w")
        self.url_entry = ttk.Entry(frame, textvariable=self.pack_url)
        self.url_entry.grid(sticky="ew", pady=(3, 10))
        ttk.Label(frame, text="Personal itch.io API key").grid(sticky="w")
        self.key_entry = ttk.Entry(frame, textvariable=self.key, show="*")
        self.key_entry.grid(sticky="ew", pady=(3, 2))
        key_options = ttk.Frame(frame)
        key_options.grid(sticky="ew")
        ttk.Checkbutton(key_options, text="Show key", variable=self.reveal_key,
                        command=self.update_key_visibility).pack(side="left")
        ttk.Label(key_options, textvariable=self.key_count).pack(side="right")

        self.check_button = ttk.Button(frame, text="Check pack", command=self.lookup)
        self.check_button.grid(sticky="w", pady=(10, 8))
        self.files = ttk.Combobox(frame, state="disabled")
        self.files.grid(sticky="ew")
        file_actions = ttk.Frame(frame)
        file_actions.grid(sticky="ew", pady=(8, 10))
        self.download_button = ttk.Button(file_actions, text="Download selected file", state="disabled",
                                          command=self.download)
        self.download_button.pack(side="left")
        self.open_folder_button = ttk.Button(file_actions, text="Open download folder", state="disabled",
                                             command=self.open_download_folder)
        self.open_folder_button.pack(side="left", padx=(8, 0))
        ttk.Label(frame, textvariable=self.status, wraplength=515).grid(sticky="w")
        self.key.trace_add("write", self.inputs_changed)
        self.pack_url.trace_add("write", self.inputs_changed)
        self.url_entry.focus_set()
        window.protocol("WM_DELETE_WINDOW", self.close)

    def update_key_visibility(self):
        self.key_entry.configure(show="" if self.reveal_key.get() else "*")

    def clear_selection(self):
        self.uploads = []
        self.files.set("")
        self.files.configure(state="disabled", values=())
        self.download_button.configure(state="disabled")

    def inputs_changed(self, *_):
        count = len(self.key.get().strip())
        self.key_count.set(f"{count} character{'s' if count != 1 else ''}")
        self.clear_selection()
        self.status.set("Select Check pack to verify these details.")

    def lookup(self):
        self.clear_selection()
        self.status.set("Checking the API key and looking up your pack…")
        self.start(check_pack, (self.pack_url.get(), self.key.get().strip()), self.show_uploads)

    def show_uploads(self, uploads):
        self.uploads = uploads
        labels = []
        for upload, _ in uploads:
            filename = upload.get("filename") or f"Upload {upload['id']}"
            label = "".join(character for character in filename if character.isprintable())
            labels.append(f"{label} ({upload.get('size', 0):,} bytes)")
        self.files.configure(state="readonly", values=labels)
        self.files.current(0)
        self.download_button.configure(state="normal")
        self.status.set("API key accepted. Choose one file to download.")

    def download(self):
        upload, key_id = self.uploads[self.files.current()]
        self.status.set("Downloading one file to a temporary folder…")
        self.start(download_file, (self.key.get().strip(), upload, key_id), self.show_download)

    def show_download(self, result):
        destination, size, digest = result
        self.saved_folder = destination.parent
        self.open_folder_button.configure(state="normal")
        self.clear_selection()
        self.key.set("")
        self.status.set(f"Download complete. Saved {destination.name} ({size:,} bytes).\nSHA-256 {digest}")

    def open_download_folder(self):
        try:
            os.startfile(self.saved_folder)
        except OSError:
            self.status.set(f"Could not open the download folder. The file was saved to {self.saved_folder}.")

    def start(self, operation, arguments, on_success):
        self.reveal_key.set(False)
        self.update_key_visibility()
        for control in (self.url_entry, self.key_entry, self.check_button, self.download_button):
            control.configure(state="disabled")
        future = self.executor.submit(operation, *arguments)

        def finish_when_ready():
            if not future.done():
                self.window.after(100, finish_when_ready)
                return
            for control in (self.url_entry, self.key_entry, self.check_button):
                control.configure(state="normal")
            if self.uploads:
                self.download_button.configure(state="normal")
            try:
                on_success(future.result())
            except TestError as error:
                self.status.set(str(error))
            except (URLError, TimeoutError, http.client.HTTPException):
                self.status.set("The network request failed or timed out. You can try again.")
            except (ValueError, TypeError, KeyError, OSError):
                self.status.set("The test received unexpected data or could not save the file.")

        self.window.after(100, finish_when_ready)

    def close(self):
        self.key.set("")
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.window.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    PaidPackPrompt(root)
    root.mainloop()
