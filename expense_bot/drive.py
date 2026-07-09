"""Google Drive integration: folder organization, upload, and backups.

Folder layout maintained under the configured parent folder:

    <parent>/Expenses/<year>/<month name>/<slip files...>

Folder IDs are cached in-memory and persisted to a local JSON file so
repeated uploads within the same month don't re-query Drive for folders
that are known to already exist (requirement: reduce API calls).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import io

from config import DRIVE_ROOT_FOLDER_NAME
from utils import sync_retry, safe_filename

logger = logging.getLogger("expense_bot.drive")

SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveManager:
    """Wraps the Drive v3 API for folder management and file uploads."""

    def __init__(
        self,
        credentials_path: str,
        parent_folder_id: str,
        cache_path: str = ".drive_folder_cache.json",
    ) -> None:
        creds = service_account.Credentials.from_service_account_file(
            credentials_path, scopes=SCOPES
        )
        self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
        self._parent_folder_id = parent_folder_id
        self._cache_path = Path(cache_path)
        self._folder_cache: dict[str, str] = self._load_cache()
        self._root_folder_id: Optional[str] = self._folder_cache.get("__root__")

    # -- cache persistence -------------------------------------------------

    def _load_cache(self) -> dict[str, str]:
        if self._cache_path.exists():
            try:
                return json.loads(self._cache_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not read Drive folder cache, starting fresh: %s", exc)
        return {}

    def _save_cache(self) -> None:
        try:
            self._cache_path.write_text(
                json.dumps(self._folder_cache, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            logger.warning("Could not persist Drive folder cache: %s", exc)

    # -- folder management ---------------------------------------------------

    @sync_retry(exceptions=(Exception,))
    def _find_child_folder(self, parent_id: str, name: str) -> Optional[str]:
        """Search Drive for an existing, non-trashed child folder by name.

        Always queries Drive directly (not the cache) so we never create a
        duplicate folder even if the cache is stale or was cleared.
        """
        safe_name = name.replace("'", "\\'")
        query = (
            f"'{parent_id}' in parents and name = '{safe_name}' "
            f"and mimeType = '{FOLDER_MIME}' and trashed = false"
        )
        response = self._service.files().list(
            q=query, spaces="drive", fields="files(id, name)", pageSize=1
        ).execute()
        files = response.get("files", [])
        return files[0]["id"] if files else None

    @sync_retry(exceptions=(Exception,))
    def _create_folder(self, parent_id: str, name: str) -> str:
        metadata = {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]}
        folder = self._service.files().create(body=metadata, fields="id").execute()
        logger.info("Created Drive folder '%s' under parent %s", name, parent_id)
        return folder["id"]

    def ensure_folder(self, parent_id: str, name: str, cache_key: str) -> str:
        """Get-or-create a folder by name under `parent_id`, never duplicating.

        `cache_key` is the dotted path used for the in-memory/disk cache
        (e.g. "root", "root/2026", "root/2026/January").
        """
        cached = self._folder_cache.get(cache_key)
        if cached:
            return cached

        existing = self._find_child_folder(parent_id, name)
        folder_id = existing or self._create_folder(parent_id, name)

        self._folder_cache[cache_key] = folder_id
        self._save_cache()
        return folder_id

    def get_root_folder_id(self) -> str:
        if self._root_folder_id:
            return self._root_folder_id
        self._root_folder_id = self.ensure_folder(
            self._parent_folder_id, DRIVE_ROOT_FOLDER_NAME, cache_key="root"
        )
        return self._root_folder_id

    def get_month_folder_id(self, year: int, month_name: str) -> str:
        """Return (creating if needed) the Expenses/<year>/<month> folder id."""
        root_id = self.get_root_folder_id()
        year_id = self.ensure_folder(root_id, str(year), cache_key=f"root/{year}")
        month_id = self.ensure_folder(
            year_id, month_name, cache_key=f"root/{year}/{month_name}"
        )
        return month_id

    # -- uploads --------------------------------------------------------------

    @sync_retry(exceptions=(Exception,))
    def upload_file(
        self,
        data: bytes,
        filename: str,
        parent_id: str,
        mime_type: str = "image/jpeg",
    ) -> tuple[str, str]:
        """Upload bytes to Drive under `parent_id`.

        Returns (file_id, web_view_link).
        """
        metadata = {"name": safe_filename(filename), "parents": [parent_id]}
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=False)
        file = self._service.files().create(
            body=metadata, media_body=media, fields="id, webViewLink"
        ).execute()
        logger.info("Uploaded '%s' to Drive folder %s (file id %s)", filename, parent_id, file["id"])
        return file["id"], file.get("webViewLink", f"https://drive.google.com/file/d/{file['id']}/view")

    def upload_slip(self, data: bytes, filename: str, year: int, month_name: str, mime_type: str) -> tuple[str, str]:
        """Convenience: upload a slip into the correct year/month folder."""
        month_folder_id = self.get_month_folder_id(year, month_name)
        return self.upload_file(data, filename, month_folder_id, mime_type)

    @sync_retry(exceptions=(Exception,))
    def copy_file(self, file_id: str, new_name: str, parent_id: str) -> str:
        """Copy an existing Drive file (e.g. the spreadsheet) for backups."""
        body = {"name": new_name, "parents": [parent_id]}
        copied = self._service.files().copy(fileId=file_id, body=body, fields="id").execute()
        logger.info("Backed up file %s as '%s' (new id %s)", file_id, new_name, copied["id"])
        return copied["id"]
