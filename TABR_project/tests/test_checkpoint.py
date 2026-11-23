"""Regression tests for deterministic and atomic project checkpoints."""
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import checkpoint


class CheckpointTest(unittest.TestCase):
    def test_roundtrip_and_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            basedir = Path(directory)
            src = basedir / 'work'
            src.mkdir()
            (src / 'empty').mkdir()
            (src / 'source.py').write_bytes(b'one\r\ntwo\n')
            (src / 'out').mkdir()
            (src / 'out' / 'object.o').write_bytes(b'ignored')
            (src / '\u6838\u9a8c.txt').write_bytes(b'ABC')
            archive = basedir / 'a.zip'
            dest = basedir / 'restored'
            dest.mkdir()
            first = checkpoint.pack(src, archive)
            other = checkpoint.pack(src, basedir / 'b.zip')
            self.assertEqual(first['archive_sha256'], other['archive_sha256'])
            with self.assertRaisesRegex(ValueError, 'already exists'):
                checkpoint.pack(src, archive)
            checkpoint.check_archive(archive, first['archive_sha256'], destination=dest)
            checkpoint.check_archive(archive, first['archive_sha256'], project=dest)
            self.assertTrue((dest / 'empty').is_dir())
            self.assertFalse((dest / 'out').exists())
            with self.assertRaisesRegex(ValueError, 'new or empty'):
                checkpoint.check_archive(archive, None, destination=dest)
            (dest / 'source.py').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'content differs'):
                checkpoint.check_archive(archive, None, project=dest)
            damaged = basedir / 'damaged.zip'
            with zipfile.ZipFile(archive) as original, zipfile.ZipFile(damaged, 'w') as out:
                for name in original.namelist():
                    contents = original.read(name)
                    if name.endswith('source.py'):
                        contents = bytes([contents[0] ^ 1]) + contents[1:]
                    out.writestr(name, contents)
            empty = basedir / 'empty'
            empty.mkdir()
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                checkpoint.check_archive(damaged, None, destination=empty)
            self.assertEqual(list(empty.iterdir()), [])
            with self.assertRaisesRegex(ValueError, 'does not match'):
                checkpoint.check_archive(damaged, first['archive_sha256'])

    def test_rejects_ambiguous_archive_paths(self):
        for bad in ('../other', 'a\\b', 'C:/x', '/etc/hosts', 'x./y', 'nul.txt'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                checkpoint.validate_name(bad)
        with self.assertRaisesRegex(ValueError, 'case-conflicting'):
            checkpoint.validate_paths([], ['Which.txt', 'which.txt'])


if __name__ == '__main__':
    unittest.main()
