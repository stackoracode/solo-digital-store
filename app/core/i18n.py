"""Minimal message-catalog i18n. en-US is the source/fallback locale; zh-CN is the second default."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

SUPPORTED_LOCALES = ("en-US", "zh-CN")
DEFAULT_LOCALE = "en-US"
LOCALE_DIR = Path(__file__).resolve().parent.parent / "locales"


@lru_cache
def catalog(locale: str) -> dict[str, str]:
    path = LOCALE_DIR / f"{locale}.json"
    with path.open(encoding="utf-8") as fh:
        data: dict[str, str] = json.load(fh)
    return data


def normalize_locale(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip().replace("_", "-").lower()
    if v.startswith("zh"):
        return "zh-CN"
    if v.startswith("en"):
        return "en-US"
    return None


def negotiate(query_lang: str | None, cookie_lang: str | None, accept_language: str | None) -> str:
    for candidate in (query_lang, cookie_lang):
        loc = normalize_locale(candidate)
        if loc:
            return loc
    if accept_language:
        for part in accept_language.split(","):
            loc = normalize_locale(part.split(";")[0])
            if loc:
                return loc
    return DEFAULT_LOCALE


def translate(locale: str, key: str, **kwargs: object) -> str:
    text = catalog(locale).get(key) or catalog(DEFAULT_LOCALE).get(key) or key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return text
    return text


class Translator:
    def __init__(self, locale: str) -> None:
        self.locale = locale

    def __call__(self, key: str, **kwargs: object) -> str:
        return translate(self.locale, key, **kwargs)
