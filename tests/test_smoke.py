import banco_agil


def test_package_exposes_version() -> None:
    assert banco_agil.__version__ == "0.1.0"
