import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.request

SOURCE = Path(__file__).resolve().parents[1] / "downloader/download.py"
spec = importlib.util.spec_from_file_location("small_downloader", SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Opener:
    def __init__(self, files):
        self.files, self.calls = files, []
    def open(self, request, timeout):
        self.calls.append(request)
        value = self.files[request.full_url.rsplit("/", 1)[-1]]
        if isinstance(value, Exception):
            raise value
        return io.BytesIO(value)


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.files = {"config.json": b'{}', "model.safetensors": b'fake-weights'}
        self.manifest = {"model_id": "test/model", "revision": "a" * 40,
                         "files": {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in self.files.items()}}

    def ensure(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return module.ensure_model(self.manifest, self.directory, sleep=lambda _: None, **kwargs)

    def test_first_download_and_ready_marker(self):
        opener = Opener(self.files)
        self.ensure(opener=opener)
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(json.loads((self.directory / ".ready.json").read_text()), self.manifest)

    def test_valid_cache_makes_zero_network_calls(self):
        self.ensure(opener=Opener(self.files))
        opener = Opener({})
        self.ensure(opener=opener, offline=True)
        self.assertEqual(opener.calls, [])

    def test_same_size_corruption_repairs_only_bad_file(self):
        self.ensure(opener=Opener(self.files))
        (self.directory / "config.json").write_bytes(b'xx')
        opener = Opener(self.files)
        self.ensure(opener=opener)
        self.assertEqual(len(opener.calls), 1)
        self.assertTrue(module.valid_file(self.directory / "config.json", self.manifest["files"]["config.json"]))

    def test_missing_marker_with_valid_files_does_not_download(self):
        self.ensure(opener=Opener(self.files))
        (self.directory / ".ready.json").unlink()
        opener = Opener({})
        self.ensure(opener=opener, offline=True)
        self.assertEqual(opener.calls, [])

    def test_offline_damaged_cache_fails_without_network(self):
        opener = Opener(self.files)
        with self.assertRaises(RuntimeError):
            self.ensure(opener=opener, offline=True)
        self.assertEqual(opener.calls, [])

    def test_failed_transfer_does_not_publish_ready(self):
        opener = Opener({"config.json": b'{}', "model.safetensors": OSError("SECRET_TOKEN")})
        with self.assertRaisesRegex(RuntimeError, "^Model file download failed$"):
            self.ensure(opener=opener, retries=2)
        self.assertFalse((self.directory / ".ready.json").exists())
        self.assertEqual((self.directory / "config.json").read_bytes(), b'{}')

    def test_wrong_hash_is_not_published(self):
        opener = Opener({**self.files, "model.safetensors": b'x' * len(self.files["model.safetensors"])})
        with self.assertRaises(RuntimeError):
            self.ensure(opener=opener, retries=1)
        self.assertFalse((self.directory / "model.safetensors").exists())
        self.assertFalse((self.directory / ".ready.json").exists())

    def test_path_traversal_rejected(self):
        self.manifest["files"]["../outside"] = self.manifest["files"]["config.json"]
        with self.assertRaises(ValueError):
            self.ensure(opener=Opener({}))

    def test_token_sent_only_to_huggingface(self):
        opener = Opener(self.files)
        with patch.dict(os.environ, {"HF_TOKEN": "PRIVATE_TEST_KEY"}):
            self.ensure(opener=opener)
        request = opener.calls[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer PRIVATE_TEST_KEY")
        redirected = module.SafeRedirects().redirect_request(request, None, 302, "", {}, "https://cdn.example/file")
        self.assertIsNone(redirected.get_header("Authorization"))
        with self.assertRaises(ValueError):
            module.SafeRedirects().redirect_request(request, None, 302, "", {}, "http://cdn.example/file")


if __name__ == "__main__":
    unittest.main()
