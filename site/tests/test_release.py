import json
import platform
import tempfile
import unittest
from pathlib import Path

from tmchess.config import CONFIG, PROJECT_ROOT, SITE_ROOT
from tmchess.release import runtime_build_identity
from tools.verify_release_assets import verify_manifest


class ReleaseVerificationTests(unittest.TestCase):
    def test_runtime_records_exact_declared_asset_hashes(self):
        manifest = json.loads(
            (PROJECT_ROOT / "release_manifest.json").read_text(encoding="utf-8")
        )
        declared = {item["role"]: item["sha256"] for item in manifest["files"]}
        identity = runtime_build_identity(CONFIG)

        self.assertEqual(identity["python_version"], manifest["python_version"])
        self.assertEqual(identity["requirements_lock_sha256"], declared["python-dependencies"])
        self.assertEqual(identity["yolo_model_sha256"], declared["yolo-weights"])
        self.assertEqual(identity["pikafish_executable_sha256"], declared["xiangqi-engine"])
        self.assertEqual(identity["pikafish_nnue_sha256"], declared["xiangqi-nnue"])

    def test_release_verifier_fails_closed_after_asset_mutation(self):
        (SITE_ROOT / "runtime").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SITE_ROOT / "runtime") as temporary:
            root = Path(temporary)
            asset = root / "asset.bin"
            asset.write_bytes(b"declared")
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps({
                "schema_version": 1,
                "python_version": platform.python_version(),
                "files": [{
                    "path": "asset.bin",
                    "bytes": 8,
                    "sha256": "f833228cb8681bbf3e38af9de7a212dfc542b3e3ba1312ec8a45bb2b67605dc7",
                }],
            }), encoding="utf-8")

            self.assertEqual(verify_manifest(root, manifest_path), [])
            asset.write_bytes(b"mutated")
            errors = verify_manifest(root, manifest_path)
            self.assertTrue(any("SHA256 不符" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
