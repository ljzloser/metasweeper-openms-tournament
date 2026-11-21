from pathlib import Path
from zipfile import ZipFile

from scripts.build_zip import build_zip


def test_zip_contains_one_installable_package_without_dev_files():
    root = Path(__file__).resolve().parents[1]
    archive = build_zip(root)
    with ZipFile(archive) as source:
        names = source.namelist()
    assert "openms_tournament/__init__.py" in names
    assert "openms_tournament/README.md" in names
    assert "openms_tournament/icon.svg" in names
    assert "openms_tournament/requirements.txt" in names
    assert "openms_tournament/analysis.py" in names
    assert "openms_tournament/analysis_dialog.py" in names
    assert {Path(name).parts[0] for name in names} == {"openms_tournament"}
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)
