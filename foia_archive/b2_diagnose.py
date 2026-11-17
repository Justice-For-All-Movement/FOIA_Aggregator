"""Explain why Backblaze B2 uploads fail, using B2's own error codes.

The S3 endpoint closes the TLS connection when it rejects an upload early
(e.g. a storage or transaction cap), so clients only see ``SSLError: EOF``.
The native API returns a JSON error ``code`` instead. This check:

1. authorizes the key (no write) and reports capabilities and bucket scope;
2. optionally (``write_probe``) uploads a 16-byte object under
   ``diagnostics/`` and deletes that exact version, so a cap or permission
   problem comes back as e.g. ``HTTP 403 cap_exceeded``.

Secrets and tokens are never printed. TLS verification stays on.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from typing import Any, Dict

import requests

AUTHORIZE = "https://api.backblazeb2.com/b2api/v4/b2_authorize_account"
CAP_CODES = {"cap_exceeded", "storage_cap_exceeded", "transaction_cap_exceeded",
             "download_cap_exceeded"}


def _code(response) -> str:
    try:
        payload = response.json()
        code = str(payload.get("code") or "") if isinstance(payload, dict) else ""
    except Exception:
        code = ""
    return code if re.fullmatch(r"[a-z_]{1,64}", code) else "unknown"


def diagnose(*, write_probe: bool = False, env=os.environ, http=requests) -> Dict[str, Any]:
    key_id = (env.get("B2_KEY_ID") or env.get("FOIA_S3_ACCESS_KEY_ID") or "").strip()
    app_key = (env.get("B2_APPLICATION_KEY") or env.get("FOIA_S3_SECRET_ACCESS_KEY") or "").strip()
    wanted = (env.get("B2_BUCKET") or env.get("FOIA_S3_BUCKET") or "").strip()
    report: Dict[str, Any] = {"credentials_present": bool(key_id and app_key),
                              "bucket_configured": bool(wanted), "verdict": "unknown"}
    if not report["credentials_present"]:
        report["verdict"] = "no_credentials"
        return report
    reply = http.get(AUTHORIZE, auth=(key_id, app_key), timeout=30, allow_redirects=False)
    if reply.status_code != 200:
        report.update(authorize=f"HTTP {reply.status_code} {_code(reply)}", verdict="key_rejected")
        return report
    payload = reply.json()
    storage = (payload.get("apiInfo") or {}).get("storageApi") or {}
    allowed = storage.get("allowed") or {}
    buckets = allowed.get("buckets") or []
    capabilities = sorted(allowed.get("capabilities") or [])
    report.update(authorize="ok", capabilities=capabilities,
                  bucket_scope=[b.get("name") for b in buckets] or "all buckets",
                  s3_endpoint=storage.get("s3ApiUrl"))
    bucket = next((b for b in buckets if not wanted or b.get("name") == wanted), None)
    if wanted and buckets and bucket is None:
        report["verdict"] = "bucket_not_allowed_for_key"
        return report
    missing = [c for c in ("writeFiles", "listFiles", "readFiles", "deleteFiles") if c not in capabilities]
    report["missing_capabilities"] = missing
    if not write_probe:
        report["verdict"] = "authorized_no_write_probe" if not missing else "missing_capabilities"
        return report
    if bucket is None or not bucket.get("id"):
        report["verdict"] = "bucket_id_unknown_set_B2_BUCKET_and_use_bucket_scoped_key"
        return report
    token = payload.get("authorizationToken")
    api = storage.get("apiUrl")
    up = http.post(f"{api}/b2api/v4/b2_get_upload_url", headers={"Authorization": token},
                   json={"bucketId": bucket["id"]}, timeout=30, allow_redirects=False)
    if up.status_code != 200:
        report.update(get_upload_url=f"HTTP {up.status_code} {_code(up)}")
        report["verdict"] = "cap_exceeded" if _code(up) in CAP_CODES else "upload_url_rejected"
        return report
    body = b"foia-diagnostic\n"
    name = f"diagnostics/b2-write-probe-{int(time.time())}.txt"
    target = up.json()
    put = http.post(target["uploadUrl"], data=body, timeout=60, allow_redirects=False, headers={
        "Authorization": target["authorizationToken"], "X-Bz-File-Name": name,
        "Content-Type": "text/plain", "Content-Length": str(len(body)),
        "X-Bz-Content-Sha1": hashlib.sha1(body).hexdigest()})
    if put.status_code != 200:
        code = _code(put)
        report.update(write_probe=f"HTTP {put.status_code} {code}",
                      verdict="cap_exceeded" if code in CAP_CODES else "upload_rejected")
        return report
    file_id = put.json().get("fileId")
    gone = http.post(f"{api}/b2api/v4/b2_delete_file_version", headers={"Authorization": token},
                     json={"fileName": name, "fileId": file_id}, timeout=30, allow_redirects=False)
    report.update(write_probe="ok", probe_deleted=gone.status_code == 200,
                  verdict="writable" if not missing else "writable_but_missing_capabilities")
    return report
