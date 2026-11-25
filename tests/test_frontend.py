from html.parser import HTMLParser
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_locale_catalogs_match():
    english = json.loads((ROOT / "web/locales/en.json").read_text())
    russian = json.loads((ROOT / "web/locales/ru.json").read_text())
    assert english.keys() == russian.keys()
    for key in english:
        assert isinstance(english[key], str) and english[key]
        assert isinstance(russian[key], str) and russian[key]


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paths = []
        self.translation_keys = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for name in ("src", "href"):
            value = attrs.get(name, "")
            if value.startswith("/") and not value.startswith("/api"):
                self.paths.append(value.split("#")[0])
        for name in ("data-i18n", "data-i18n-placeholder", "data-i18n-title", "data-i18n-aria"):
            if name in attrs:
                self.translation_keys.append(attrs[name])


def test_static_assets_exist_and_html_keys_are_translated():
    parser = Assets()
    parser.feed((ROOT / "web/index.html").read_text())
    for path in parser.paths:
        if path == "/":
            continue
        assert (ROOT / "web" / path.lstrip("/")).is_file(), path
    catalog = json.loads((ROOT / "web/locales/en.json").read_text())
    assert set(parser.translation_keys) <= catalog.keys()


def test_no_frontend_build_or_remote_assets():
    assert not (ROOT / "package.json").exists()
    html = (ROOT / "web/index.html").read_text()
    assert "https://" not in html
    assert "<script> " not in html
