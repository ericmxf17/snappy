from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_py2app_is_an_explicit_pinned_requirement():
    requirements = (ROOT / "requirements.txt").read_text()
    assert "py2app==0.28.10" in requirements


def test_snaptrade_sdk_runtime_contract_is_pinned():
    requirements = (ROOT / "requirements.txt").read_text()
    assert "snaptrade-python-sdk==11.0.212" in requirements


def test_setup_does_not_use_legacy_setup_requires():
    setup = (ROOT / "setup.py").read_text()
    assert "setup_requires" not in setup


def test_build_script_checks_for_preinstalled_py2app():
    script = (ROOT / "scripts" / "build_dmg.sh").read_text()
    assert "import py2app" in script
    assert "uv pip install -r requirements.txt" in script
