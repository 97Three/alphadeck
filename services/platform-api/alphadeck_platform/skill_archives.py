import io
import re
import stat
import tarfile
import zipfile
from pathlib import PurePosixPath

from .scaffold import controlled_path

MAXIMUM = 5 * 1024 * 1024


def archive_path(value):
    value = re.sub(r"^(?:\./)+", "", value).rstrip("/")
    if (
        not value
        or "\\" in value
        or "\0" in value
        or value.startswith("/")
        or any(part in ("", ".", "..") for part in value.split("/"))
        or ":" in value.split("/")[0]
    ):
        raise ValueError("压缩包包含不安全路径。")
    return value


def unpack_archive(content, filename, target, identity):
    if not 0 < len(content) <= MAXIMUM:
        raise ValueError("上传包大小必须在 1B 到 5MB 之间。")
    filename = filename.lower()
    if not filename.endswith((".zip", ".tgz", ".tar.gz")):
        raise ValueError("仅支持 .zip、.tgz 或 .tar.gz。")
    target.mkdir(parents=True, exist_ok=True)
    seen, total = set(), 0

    def entry(name, size, directory, source):
        nonlocal total
        name = archive_path(name)
        if name in seen or len(seen) >= 250:
            raise ValueError("压缩包条目重复或超过 250 个。")
        seen.add(name)
        if not 0 <= size <= 10 * 1024 * 1024:
            raise ValueError("压缩包包含超过 10MB 的文件。")
        total += size
        if total > 50 * 1024 * 1024:
            raise ValueError("压缩包展开后的文件总量不得超过 50MB。")
        destination = controlled_path(target, name)
        if directory:
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            data = source.read(size + 1)
            if len(data) != size:
                raise ValueError("压缩包文件尺寸与声明不一致。")
            destination.write_bytes(data)

    if filename.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for item in archive.infolist():
                mode = item.external_attr >> 16
                if item.flag_bits & 1 or mode and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    raise ValueError("压缩包只能包含普通文件和目录。")
                with archive.open(item) as source:
                    entry(item.filename, item.file_size, item.is_dir(), source)
    else:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
            for item in archive:
                if not (item.isfile() or item.isdir()):
                    raise ValueError("压缩包只能包含普通文件和目录。")
                if item.isdir():
                    entry(item.name, 0, True, None)
                else:
                    with archive.extractfile(item) as source:
                        entry(item.name, item.size, False, source)
    if not seen:
        raise ValueError("压缩包不能为空。")
    roots = [
        target / identity,
        target,
        *(path for path in sorted(target.iterdir()) if path.is_dir()),
    ]
    for root in roots:
        if (root / "SKILL.md").is_file():
            # Reject suspicious nested filesystem objects before any candidate is activated.
            paths = list(target.rglob("*"))
            if len(paths) > 250 or any(path.is_symlink() for path in paths):
                raise ValueError("压缩包展开后的条目不安全。")
            for path in root.rglob("*"):
                if (
                    path.is_file()
                    and PurePosixPath(path.relative_to(root).as_posix()).parts[0] == "scripts"
                ):
                    path.chmod(0o755)
            return root
    raise ValueError("压缩包中未找到 SKILL.md。")
