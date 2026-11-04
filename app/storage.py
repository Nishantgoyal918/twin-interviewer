"""Object storage (plan 6.1): audio segments, raw Deepgram JSON, imports.

Browsers upload straight to storage with presigned URLs; audio never passes through a
function body (plan 10.3). `supabase` is the real backend; `local` keeps files under
.data/storage and serves them through /api/dev/storage for offline development.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import httpx

from app.settings import ROOT, get_settings

BUCKET = "audio"


class Storage(Protocol):
    def signed_upload(self, path: str, content_type: str) -> dict: ...
    def signed_read_url(self, path: str, expires_s: int = 3600) -> str: ...
    def put_bytes(self, path: str, data: bytes, content_type: str) -> None: ...
    def get_bytes(self, path: str) -> bytes: ...


class SupabaseStorage:
    def __init__(self, url: str, service_key: str) -> None:
        self.base = f"{url}/storage/v1"
        self.headers = {"Authorization": f"Bearer {service_key}", "apikey": service_key}

    def signed_upload(self, path: str, content_type: str) -> dict:
        # [VERIFY] Supabase signed-upload endpoint shape.
        r = httpx.post(
            f"{self.base}/object/upload/sign/{BUCKET}/{quote(path)}", headers=self.headers, timeout=15
        )
        r.raise_for_status()
        rel = r.json()["url"]
        return {"url": f"{self.base}{rel}", "method": "PUT",
                "headers": {"content-type": content_type, "x-upsert": "true"}}

    def signed_read_url(self, path: str, expires_s: int = 3600) -> str:
        r = httpx.post(
            f"{self.base}/object/sign/{BUCKET}/{quote(path)}",
            headers=self.headers, json={"expiresIn": expires_s}, timeout=15,
        )
        r.raise_for_status()
        return f"{self.base}{r.json()['signedURL']}"

    def put_bytes(self, path: str, data: bytes, content_type: str) -> None:
        r = httpx.post(
            f"{self.base}/object/{BUCKET}/{quote(path)}",
            headers={**self.headers, "content-type": content_type, "x-upsert": "true"},
            content=data, timeout=60,
        )
        r.raise_for_status()

    def get_bytes(self, path: str) -> bytes:
        r = httpx.get(f"{self.base}/object/{BUCKET}/{quote(path)}", headers=self.headers, timeout=60)
        r.raise_for_status()
        return r.content


class LocalStorage:
    def __init__(self) -> None:
        self.root = ROOT / ".data" / "storage"

    def _file(self, path: str) -> Path:
        p = (self.root / path).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError("bad path")
        return p

    @staticmethod
    def sign(path: str, method: str, exp: int) -> str:
        secret = get_settings().invite_token_secret.encode()
        return hmac.new(secret, f"{method}:{path}:{exp}".encode(), hashlib.sha256).hexdigest()

    def _url(self, path: str, method: str, expires_s: int) -> str:
        exp = int(time.time()) + expires_s
        sig = self.sign(path, method, exp)
        return f"{get_settings().app_base_url}/api/dev/storage/{quote(path)}?exp={exp}&sig={sig}"

    def signed_upload(self, path: str, content_type: str) -> dict:
        return {"url": self._url(path, "PUT", 3600), "method": "PUT",
                "headers": {"content-type": content_type}}

    def signed_read_url(self, path: str, expires_s: int = 3600) -> str:
        return self._url(path, "GET", expires_s)

    def put_bytes(self, path: str, data: bytes, content_type: str) -> None:
        f = self._file(path)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)

    def get_bytes(self, path: str) -> bytes:
        return self._file(path).read_bytes()

    def exists(self, path: str) -> bool:
        return self._file(path).exists()


_storage: Storage | None = None


def storage() -> Storage:
    global _storage
    if _storage is None:
        s = get_settings()
        if s.storage_backend == "local":
            _storage = LocalStorage()
        else:
            _storage = SupabaseStorage(s.supabase_url, s.supabase_service_role_key)
    return _storage
