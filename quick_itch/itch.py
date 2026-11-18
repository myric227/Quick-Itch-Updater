from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import time
from urllib.parse import urljoin, urlsplit

import httpx

from .model import Asset, Upload


API = "https://api.itch.io"
REDIRECTS = {301, 302, 303, 307, 308}


class ItchError(Exception):
    pass


def retry_delay(response, attempt):
    value = response.headers.get("Retry-After", "")
    if value.isdecimal():
        return int(value)
    if value:
        try:
            date = parsedate_to_datetime(value)
            return max(0, (date - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            pass
    return 2**attempt


class ItchClient:
    def __init__(self, key, transport=None):
        self.key = key
        self.session = httpx.Client(
            transport=transport, timeout=httpx.Timeout(30, connect=10),
            headers={"User-Agent": "Quick-Itch-Updater/0.1", "Accept-Encoding": "identity"},
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.session.close()

    def _get(self, path, params=None, allow_redirect=False):
        for attempt in range(3):
            try:
                response = self.session.get(f"{API}{path}", params=params,
                                            headers={"Authorization": f"Bearer {self.key}"})
            except httpx.TransportError:
                if attempt == 2:
                    raise ItchError("Could not reach itch.io. Check your internet connection.") from None
                time.sleep(2**attempt)
                continue
            if response.status_code in {429, 502, 503, 504} and attempt < 2:
                delay = retry_delay(response, attempt)
                if delay > 15:
                    raise ItchError(f"itch.io asked us to retry after {int(delay) + 1} seconds.")
                time.sleep(delay)
                continue
            break
        if allow_redirect and response.status_code in REDIRECTS and response.headers.get("Location"):
            return {"url": urljoin(str(response.url), response.headers["Location"])}
        if response.status_code in {401, 403}:
            raise ItchError("itch.io denied access. Check your API key and ownership of this file.")
        if response.is_error or response.is_redirect:
            raise ItchError(f"itch.io returned HTTP {response.status_code}.")
        try:
            result = response.json()
        except ValueError:
            raise ItchError("itch.io returned an unreadable response.") from None
        if not isinstance(result, dict) or result.get("errors"):
            raise ItchError("itch.io rejected the request. Check your account and access to this pack.")
        return result

    def profile(self):
        user = self._get("/profile").get("user")
        if not isinstance(user, dict) or not user.get("id"):
            raise ItchError("itch.io did not return an account for this key.")
        return int(user["id"]), user.get("display_name") or user.get("username") or "itch.io account"

    def owned_assets(self):
        assets = {}
        keys_by_asset = {}
        page = 1
        while True:
            result = self._get("/profile/owned-keys", {"page": page, "per_page": 100})
            if result and not any(field in result for field in ("owned_keys", "ownedKeys", "page")):
                raise ItchError("itch.io returned an unexpected owned-library response.")
            keys = result.get("owned_keys", result.get("ownedKeys"))
            if keys in (None, {}, []):
                break
            if not isinstance(keys, list):
                raise ItchError("itch.io returned an unexpected owned-key list.")
            for key in keys:
                game = key.get("game") or {}
                asset_id = int(key["game_id"])
                keys_by_asset.setdefault(asset_id, set()).add(int(key["id"]))
                if game and asset_id not in assets:
                    assets[asset_id] = game
            page += 1
        for asset_id, game in assets.items():
            url = game.get("url") or ""
            creator = (game.get("user") or {}).get("display_name")
            if not creator:
                creator = (urlsplit(url).hostname or "Unknown creator").removesuffix(".itch.io")
            yield Asset(asset_id, game.get("title") or f"Project {asset_id}", creator,
                        url, tuple(sorted(keys_by_asset[asset_id])), game.get("classification"))

    def uploads(self, asset):
        uploads = {}
        for key_id in asset.key_ids:
            payload = self._get(f"/games/{asset.id}/uploads", {"download_key_id": key_id})
            items = payload.get("uploads")
            if items is None or items == {}:
                continue
            if not isinstance(items, list):
                raise ItchError("itch.io returned an unexpected upload list.")
            for item in items:
                if item.get("storage") not in {"hosted", "build"} or item.get("demo") or item.get("preorder"):
                    continue
                upload_id = int(item["id"])
                build = item.get("build") or {}
                uploads.setdefault(upload_id, Upload(
                    upload_id, item.get("filename") or f"upload-{upload_id}", item.get("size"),
                    item["storage"], key_id, item.get("build_id") or build.get("id"),
                    item.get("updated_at"), build.get("created_at") or item.get("created_at"),
                    item.get("display_name"),
                ))
        return list(uploads.values())

    @contextmanager
    def open_download(self, upload):
        payload = self._get(f"/uploads/{upload.id}/download", {"download_key_id": upload.key_id},
                            allow_redirect=True)
        url = payload.get("url")
        for _ in range(6):
            parts = urlsplit(url or "")
            if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
                raise ItchError("itch.io did not return a usable HTTPS download link.")
            try:
                with self.session.stream("GET", url) as response:
                    if response.status_code in REDIRECTS and response.headers.get("Location"):
                        url = urljoin(url, response.headers["Location"])
                        continue
                    if response.status_code != 200 or response.headers.get("Content-Type", "").startswith("text/html"):
                        raise ItchError(f"The file host did not return an archive or file (HTTP {response.status_code}).")
                    yield response
                    return
            except httpx.HTTPError:
                raise ItchError("The transfer was interrupted. You can retry the download.") from None
        raise ItchError("The file host redirected too many times.")
