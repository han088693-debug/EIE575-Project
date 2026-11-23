"""Create and restore a portable, SHA-256 verified EIE575 project checkpoint."""
import argparse
import hashlib
import json
import ntpath
import os
from pathlib import Path, PureWindowsPath
import stat
import sys
import tempfile
import zipfile


FORMAT = 'eie575-checkpoint-v1'
INDEX = 'checkpoint-manifest.json'
PREFIX = 'project/'
BLOCK = 1024 * 1024
is_reserved = getattr(ntpath, 'isreserved', lambda part: PureWindowsPath(part).is_reserved())


def validate_name(name):
    parts = name.split('/')
    for part in parts:
        if (not part or part in ('.', '..') or part[-1] in ' .' or
                any(ch in '\\:<>"|?*' or ord(ch) < 32 for ch in part) or
                is_reserved(part)):
            raise ValueError('non-portable project path: ' + repr(name))
    return name


def reparse_point(path):
    mode = path.lstat()
    return stat.S_ISLNK(mode.st_mode) or bool(getattr(mode, 'st_file_attributes', 0) & 0x400)


def excluded(relative):
    parts = relative.parts
    return parts[0] in ('out', '.git') or '__pycache__' in parts or relative.suffix in ('.pyc', '.pyo')


def validate_paths(directories, files):
    seen = set()
    folder_set = set(directories)
    for name in [*directories, *files]:
        validate_name(name)
        folded = name.casefold()
        if folded in seen:
            raise ValueError('duplicate or case-conflicting name: ' + name)
        seen.add(folded)
        parent = name.rpartition('/')[0]
        if parent and parent not in folder_set:
            raise ValueError('missing parent directory: ' + name)


def inventory(root):
    if not root.is_dir() or reparse_point(root):
        raise ValueError('source must be a plain directory: ' + str(root))
    dirs, files = [], []

    def walk(directory):
        for path in directory.iterdir():
            relative = path.relative_to(root)
            if excluded(relative):
                continue
            if reparse_point(path):
                raise ValueError('reparse points are unsupported: ' + str(path))
            name = validate_name(relative.as_posix())
            if path.is_dir():
                dirs.append(name)
                walk(path)
            elif path.is_file():
                files.append(name)
            else:
                raise ValueError('unsupported entry: ' + str(path))

    walk(root)
    validate_paths(dirs, files)
    return sorted(dirs), sorted(files)


def transfer(reader, writer=None):
    digest = hashlib.sha256()
    size = 0
    while block := reader.read(BLOCK):
        digest.update(block)
        size += len(block)
        if writer is not None:
            writer.write(block)
    return {'size': size, 'sha256': digest.hexdigest()}


def file_hash(path):
    with path.open('rb') as reader:
        return transfer(reader)['sha256']


def member(name, length):
    entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
    entry.create_system = 3
    entry.external_attr = (stat.S_IFREG | 0o644) << 16
    entry.compress_type = zipfile.ZIP_DEFLATED
    entry.file_size = length
    return entry


def stamp(path):
    s = path.stat()
    return s.st_size, s.st_mtime_ns, s.st_ino


def summary(archive, digest, manifest):
    return {'archive': str(archive), 'archive_sha256': digest, 'archive_bytes': archive.stat().st_size,
            'files': len(manifest['files']), 'directories': len(manifest['directories']),
            'uncompressed_bytes': sum(entry['size'] for entry in manifest['files'].values())}


def pack(root, archive):
    if archive.is_relative_to(root):
        raise ValueError('archive must be outside source')
    if archive.exists():
        raise ValueError('output already exists: ' + str(archive))
    dirs, files = inventory(root)
    initial = {name: stamp(root / name) for name in files}
    records = {}
    archive.parent.mkdir(parents=True, exist_ok=True)
    # Stage next to the delivery file so Windows inherits its directory permissions.
    descriptor, name = tempfile.mkstemp(prefix='.tabr-pack-', suffix='.tmp', dir=archive.parent)
    os.close(descriptor)
    temp = Path(name)
    try:
        with zipfile.ZipFile(temp, 'w', allowZip64=True) as bundle:
            for name in files:
                size = initial[name][0]
                with (root / name).open('rb') as reader, bundle.open(member(PREFIX + name, size), 'w') as writer:
                    records[name] = transfer(reader, writer)
                if records[name]['size'] != size:
                    raise ValueError('file changed during packaging: ' + name)
            if inventory(root) != (dirs, files) or any(stamp(root / n) != s for n, s in initial.items()):
                raise ValueError('source changed during packaging; wait for writers to finish')
            manifest = {'format': FORMAT, 'directories': dirs, 'files': records}
            index = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')
            bundle.writestr(member(INDEX, len(index)), index)
        digest = file_hash(temp)
        if os.name == 'nt':
            temp.rename(archive)  # Windows rejects an existing destination.
        else:
            os.link(temp, archive)
    finally:
        temp.unlink(missing_ok=True)
    return summary(archive, digest, manifest)


def read_manifest(bundle):
    members = bundle.infolist()
    names = [m.filename for m in members]
    if len(names) != len(set(names)) or INDEX not in names:
        raise ValueError('missing manifest or duplicate ZIP member')
    manifest = json.loads(bundle.read(INDEX))
    if manifest.get('format') != FORMAT:
        raise ValueError('unsupported manifest format')
    dirs, files = manifest['directories'], manifest['files']
    validate_paths(dirs, files)
    if set(names) != {INDEX, *(PREFIX + name for name in files)}:
        raise ValueError('archive members differ from manifest')
    for n, record in files.items():
        info = bundle.getinfo(PREFIX + n)
        if (info.file_size != record['size'] or not isinstance(record['sha256'], str) or
                len(record['sha256']) != 64 or stat.S_ISLNK(info.external_attr >> 16)):
            raise ValueError('bad file record: ' + n)
    return manifest


def compare_tree(root, manifest):
    dirs, files = inventory(root)
    if set(dirs) != set(manifest['directories']) or set(files) != set(manifest['files']):
        raise ValueError('project paths differ from manifest')
    for name in files:
        with (root / name).open('rb') as reader:
            if transfer(reader) != manifest['files'][name]:
                raise ValueError('project file content differs: ' + name)


def ensure_empty(dest):
    if dest.is_symlink() or (dest.exists() and (reparse_point(dest) or not dest.is_dir() or any(dest.iterdir()))):
        raise ValueError('restore destination must be new or empty: ' + str(dest))
    if not str(dest).isascii() or ' ' in str(dest):
        raise ValueError('the simulator requires an ASCII restore path without spaces')


def check_archive(archive, expected, project=None, destination=None):
    digest = file_hash(archive)
    if expected and digest != expected.lower():
        raise ValueError('archive SHA-256 does not match the expected digest')
    with zipfile.ZipFile(archive) as bundle:
        manifest = read_manifest(bundle)
        if destination is None:
            for name, wanted in manifest['files'].items():
                with bundle.open(PREFIX + name) as reader:
                    if transfer(reader) != wanted:
                        raise ValueError('archive hash mismatch: ' + name)
            if project is not None:
                compare_tree(project, manifest)
        else:
            ensure_empty(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='.tabr-restore-', dir=destination.parent) as staging:
                staged = Path(staging) / 'tree'
                staged.mkdir()
                for name in sorted(manifest['directories']):
                    (staged / name).mkdir()
                for name, wanted in manifest['files'].items():
                    with bundle.open(PREFIX + name) as reader, (staged / name).open('xb') as writer:
                        if transfer(reader, writer) != wanted:
                            raise ValueError('archive hash mismatch: ' + name)
                compare_tree(staged, manifest)
                ensure_empty(destination)
                existed = destination.is_dir()
                if existed:
                    destination.rmdir()
                try:
                    staged.rename(destination)
                except OSError:
                    if existed:
                        destination.mkdir(exist_ok=True)
                    raise
    answer = summary(archive, digest, manifest)
    if project is not None or destination is not None:
        answer['verified_project'] = str(destination or project)
    return answer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action', required=True)
    for name in ('pack', 'verify', 'restore'):
        command = actions.add_parser(name)
        command.add_argument('--archive', type=Path, required=True)
        if name != 'pack':
            command.add_argument('--sha256', help='compare the complete ZIP against an external digest')
        if name == 'restore':
            command.add_argument('--destination', type=Path, required=True)
        else:
            command.add_argument('--project', type=Path, required=(name == 'pack'))
    args = parser.parse_args()
    destination = getattr(args, 'destination', None)
    project = getattr(args, 'project', None)
    try:
        archive = args.archive.absolute()
        if args.action == 'pack':
            result = pack(project.resolve(strict=True), archive.parent.resolve() / archive.name)
        else:
            result = check_archive(archive, args.sha256, project and project.absolute(), destination and destination.absolute())
        print(json.dumps({'ok': True, **result}, ensure_ascii=True, indent=2))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, zipfile.BadZipFile) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=True), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
