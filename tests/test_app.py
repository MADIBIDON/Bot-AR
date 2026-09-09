from app import __version__
from app.main import get_banner, main


def test_version_is_set() -> None:
    assert __version__ == "0.0.1"


def test_get_banner_contains_version() -> None:
    banner = get_banner()
    assert "Retail Opportunity & Purchase Assistant" in banner
    assert __version__ in banner


def test_main_prints_banner(capsys) -> None:
    main()
    captured = capsys.readouterr()
    assert captured.out.strip() == get_banner()
