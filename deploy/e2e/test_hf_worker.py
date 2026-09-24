import hashlib
from pathlib import Path
from hf_worker import sha_manifest

def test_sha_manifest_hashes_files_and_skips_cache(tmp_path: Path):
    (tmp_path / "config.json").write_bytes(b"abc")
    sub = tmp_path / "sub"; sub.mkdir()
    (sub / "model.safetensors").write_bytes(b"weights")
    cache = tmp_path / ".cache"; cache.mkdir()
    (cache / "junk").write_bytes(b"ignore me")

    got = sha_manifest(tmp_path)

    assert got == {
        "config.json": hashlib.sha256(b"abc").hexdigest(),
        "sub/model.safetensors": hashlib.sha256(b"weights").hexdigest(),
    }
