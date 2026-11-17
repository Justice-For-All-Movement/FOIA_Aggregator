"""B2 diagnosis reports B2 error codes, never secrets (fake HTTP only)."""
import unittest

from foia_archive.b2_diagnose import diagnose


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


class FakeHttp:
    def __init__(self, upload_status=200, upload_code=None):
        self.upload_status, self.upload_code, self.calls = upload_status, upload_code, []

    def get(self, url, **kw):
        self.calls.append(url)
        return FakeResponse(200, {"authorizationToken": "TOKEN-SECRET", "apiInfo": {"storageApi": {
            "apiUrl": "https://api005.backblazeb2.com", "s3ApiUrl": "https://s3.us-east-005.backblazeb2.com",
            "allowed": {"buckets": [{"id": "b1", "name": "FOIA-Search"}],
                        "capabilities": ["listFiles", "readFiles", "writeFiles", "deleteFiles"]}}}})

    def post(self, url, **kw):
        self.calls.append(url)
        if url.endswith("b2_get_upload_url"):
            return FakeResponse(200, {"uploadUrl": "https://pod-000.backblaze.com/up", "authorizationToken": "UP"})
        if url.endswith("/up"):
            if self.upload_status != 200:
                return FakeResponse(self.upload_status, {"code": self.upload_code, "message": "secret-ish text"})
            return FakeResponse(200, {"fileId": "f1"})
        return FakeResponse(200, {})


ENV = {"B2_KEY_ID": "kid", "B2_APPLICATION_KEY": "appkey"}


class DiagnoseTests(unittest.TestCase):
    def test_cap_exceeded_is_named(self):
        report = diagnose(write_probe=True, env=ENV, http=FakeHttp(403, "cap_exceeded"))
        self.assertEqual(report["verdict"], "cap_exceeded")
        self.assertEqual(report["write_probe"], "HTTP 403 cap_exceeded")
        text = repr(report)
        for secret in ("TOKEN-SECRET", "appkey", "secret-ish"):
            self.assertNotIn(secret, text)

    def test_writable_probe_is_deleted(self):
        http = FakeHttp()
        report = diagnose(write_probe=True, env=ENV, http=http)
        self.assertEqual(report["verdict"], "writable")
        self.assertTrue(report["probe_deleted"])
        self.assertTrue(any(c.endswith("b2_delete_file_version") for c in http.calls))

    def test_no_credentials_makes_no_request(self):
        http = FakeHttp()
        self.assertEqual(diagnose(env={}, http=http)["verdict"], "no_credentials")
        self.assertEqual(http.calls, [])

    def test_wrong_bucket(self):
        env = dict(ENV, B2_BUCKET="other")
        self.assertEqual(diagnose(env=env, http=FakeHttp())["verdict"], "bucket_not_allowed_for_key")


if __name__ == "__main__":
    unittest.main()
