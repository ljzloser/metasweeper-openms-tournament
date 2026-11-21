"""生成可由元扫雷插件管理器导入的 ZIP，不导入插件或依赖宿主。"""
from pathlib import Path
import tomllib
from zipfile import ZIP_DEFLATED, ZipFile


def build_zip(root: Path | None = None) -> Path:
    root = root or Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    package = root / "openms_tournament"
    output = root / "dist" / f"metasweeper-openms-tournament-{version}.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path in sorted(package.iterdir()):
            if path.is_file() and (path.suffix in {".py", ".md", ".svg"} or path.name == "requirements.txt"):
                archive.write(path, path.relative_to(root).as_posix())
    return output


if __name__ == "__main__":
    print(build_zip())
