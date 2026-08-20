from __future__ import annotations

import argparse
import ast
import hashlib
from datetime import date
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHECKSUM_FILE = "RELEASE-MANIFEST.sha256"

SERVER_MANIFEST = (
    ".dockerignore",
    ".env.example",
    ".gitignore",
    "AI-DEPLOY-PROMPT.txt",
    "API.zh-CN.md",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "Dockerfile",
    "INSTALL.zh-CN.md",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "app/__init__.py",
    "app/article.py",
    "app/config.py",
    "app/credentials.py",
    "app/database.py",
    "app/image_tools.py",
    "app/main.py",
    "app/passwords.py",
    "app/static/app.css",
    "app/static/app.js",
    "app/static/index.html",
    "app/wechat.py",
    "docker-compose.yml",
    "docs/images/console-api.png",
    "docs/images/console-overview.png",
    "docs/images/project-flow.svg",
    "docs/images/social/creator-value.png",
    "docs/images/social/server-showcase.png",
    "install.sh",
    "nginx/wechat-uploader.conf",
    "pyproject.toml",
    "requirements-dev.txt",
    "requirements.txt",
    "rotate-api-keys.sh",
    "scripts/build_release.py",
    "show-client-config.sh",
)
REQUIRED_FILES = {
    "LICENSE",
    "app/article.py",
    "app/credentials.py",
    "app/main.py",
    "app/passwords.py",
    "docker-compose.yml",
    "install.sh",
    "rotate-api-keys.sh",
    "show-client-config.sh",
}
EXCLUDED_PARTS = {
    ".git",
    ".pytest_cache",
    ".qa",
    ".ruff_cache",
    "__pycache__",
    "artifacts",
}
SENSITIVE_SUFFIXES = {".db", ".key", ".pem", ".sqlite", ".sqlite3"}


def project_version() -> str:
    module = ast.parse((PROJECT_ROOT / "app/__init__.py").read_text("utf-8"))
    for statement in module.body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name) and target.id == "__version__":
                    value = ast.literal_eval(statement.value)
                    if isinstance(value, str):
                        return value
    raise RuntimeError("app/__init__.py does not define a string __version__")


def is_excluded(relative_path: Path) -> bool:
    if any(part in EXCLUDED_PARTS for part in relative_path.parts):
        return True
    if relative_path.name == ".env" or (
        relative_path.name.startswith(".env.")
        and relative_path.name != ".env.example"
    ):
        return True
    return relative_path.suffix.lower() in SENSITIVE_SUFFIXES or relative_path.suffix == ".pyc"


def release_entries() -> list[tuple[Path, str]]:
    entries = [(Path(name), Path(name).as_posix()) for name in SERVER_MANIFEST]
    missing = [name for source, name in entries if not (PROJECT_ROOT / source).is_file()]
    if missing:
        raise FileNotFoundError("manifest files are missing: " + ", ".join(missing))
    excluded = [name for source, name in entries if is_excluded(source)]
    if excluded:
        raise RuntimeError("manifest includes excluded files: " + ", ".join(excluded))
    return sorted(entries, key=lambda entry: entry[1])


def zip_info(name: str, *, executable: bool = False) -> ZipInfo:
    info = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (0o100755 if executable else 0o100644) << 16
    return info


def build_archive(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output.with_suffix(f"{output.suffix}.tmp")
    checksums: list[str] = []
    try:
        with ZipFile(temporary_output, "w") as archive:
            for source, archive_name in release_entries():
                data = (PROJECT_ROOT / source).read_bytes()
                archive.writestr(
                    zip_info(archive_name, executable=source.suffix == ".sh"),
                    data,
                )
                checksums.append(
                    f"{hashlib.sha256(data).hexdigest()}  {archive_name}"
                )
            manifest = ("\n".join(checksums) + "\n").encode()
            archive.writestr(zip_info(CHECKSUM_FILE), manifest)
        verify_archive(temporary_output)
        temporary_output.replace(output)
    finally:
        temporary_output.unlink(missing_ok=True)


def validate_archive_name(name: str) -> None:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise RuntimeError(f"unsafe archive path: {name}")
    if is_excluded(Path(*path.parts)):
        raise RuntimeError(f"excluded file found in archive: {name}")


def verify_archive(archive_path: Path) -> None:
    with ZipFile(archive_path, "r") as archive:
        name_list = archive.namelist()
        names = set(name_list)
        if len(name_list) != len(names):
            raise RuntimeError("archive contains duplicate paths")
        for name in name_list:
            validate_archive_name(name)

        expected_names = {name for _, name in release_entries()} | {CHECKSUM_FILE}
        missing = expected_names.difference(names)
        unexpected = names.difference(expected_names)
        if missing:
            raise RuntimeError("archive is missing files: " + ", ".join(sorted(missing)))
        if unexpected:
            raise RuntimeError(
                "archive contains files outside its whitelist: "
                + ", ".join(sorted(unexpected))
            )
        required_missing = REQUIRED_FILES.difference(names)
        if required_missing:
            raise RuntimeError(
                "archive is missing required files: "
                + ", ".join(sorted(required_missing))
            )

        checksum_lines = archive.read(CHECKSUM_FILE).decode("utf-8").splitlines()
        checked_names: set[str] = set()
        for line in checksum_lines:
            checksum, separator, name = line.partition("  ")
            if not separator or len(checksum) != 64 or name not in names:
                raise RuntimeError(f"invalid checksum entry: {line}")
            if name in checked_names:
                raise RuntimeError(f"duplicate checksum entry: {name}")
            if hashlib.sha256(archive.read(name)).hexdigest() != checksum:
                raise RuntimeError(f"checksum mismatch: {name}")
            checked_names.add(name)
        expected_checksums = names - {CHECKSUM_FILE}
        if checked_names != expected_checksums:
            missing_checksums = expected_checksums.difference(checked_names)
            raise RuntimeError(
                "manifest is missing checksums: "
                + ", ".join(sorted(missing_checksums))
            )


def default_output() -> Path:
    stamp = f"{date.today():%Y%m%d}"
    filename = f"wechat-console-server-v{project_version()}-{stamp}.zip"
    return PROJECT_ROOT / "artifacts" / filename


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build or verify the server release ZIP")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--output", type=Path, help="output ZIP path")
    group.add_argument("--verify-only", type=Path, help="verify an existing ZIP")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.verify_only:
        verify_archive(args.verify_only.resolve())
        print(f"Verified server release: {args.verify_only}")
        return 0

    output = args.output or default_output()
    output = output if output.is_absolute() else PROJECT_ROOT / output
    build_archive(output.resolve())
    print(f"Built and verified server release: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
