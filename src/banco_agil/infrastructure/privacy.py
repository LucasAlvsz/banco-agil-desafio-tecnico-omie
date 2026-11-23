import logging
import re
import traceback
from collections.abc import Collection
from threading import RLock
from typing import Any

from banco_agil.domain.errors import DomainValidationError
from banco_agil.domain.rules import normalize_birth_date, normalize_cpf

REDACTED_PLACEHOLDER = "[dado omitido]"
_CPF_PATTERN = re.compile(r"(?<!\d)[0-9](?:[.\-\s]*[0-9]){10}(?!\d)")
_DATE_PATTERN = re.compile(
    r"(?<!\d)(?:[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}|"
    r"[0-9]{1,2}([/.-])[0-9]{1,2}\1[0-9]{4})(?!\d)"
)
_SECRET_PATTERN = re.compile(
    r"\b(?:(?i:Bearer)[ \t]+[A-Za-z0-9._~+/-]+=*|"
    r"(?:gsk_|pk-lf-|sk-lf-|sk-|AIza|AKIA)[A-Za-z0-9_-]+)"
)
_LOGGING_LOCK = RLock()


def contains_pii(
    text: str, *, cpfs: Collection[str] = (), birth_dates: Collection[str] = ()
) -> bool:
    known_cpfs = {normalize_cpf(cpf) for cpf in cpfs}
    known_birth_dates = {normalize_birth_date(date) for date in birth_dates}
    if known_cpfs and any(
        normalize_cpf(match.group()) in known_cpfs for match in _CPF_PATTERN.finditer(text)
    ):
        return True
    if known_birth_dates:
        for match in _DATE_PATTERN.finditer(text):
            try:
                if normalize_birth_date(match.group()) in known_birth_dates:
                    return True
            except DomainValidationError:
                continue
    return False


def mask_pii(text: str, *, preserve_dates_except: str | None = None) -> str:
    text = _SECRET_PATTERN.sub(REDACTED_PLACEHOLDER, text)
    text = _CPF_PATTERN.sub(REDACTED_PLACEHOLDER, text)
    if preserve_dates_except is None:
        return _DATE_PATTERN.sub(REDACTED_PLACEHOLDER, text)

    def redact_birth_date(match: re.Match[str]) -> str:
        candidate = match.group()
        try:
            if normalize_birth_date(candidate) == preserve_dates_except:
                return REDACTED_PLACEHOLDER
        except DomainValidationError:
            pass
        return candidate

    return _DATE_PATTERN.sub(redact_birth_date, text)


def mask_pii_data(value: Any) -> Any:
    if isinstance(value, str):
        return mask_pii(value)
    if isinstance(value, dict):
        return {mask_pii_data(key): mask_pii_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [mask_pii_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(mask_pii_data(item) for item in value)
    return value


class PiiMaskingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (KeyError, TypeError, ValueError):
            message = record.msg if isinstance(record.msg, str) else "Mensagem indisponível."
        record.msg = mask_pii(message)
        record.args = None
        if record.exc_text:
            record.exc_text = mask_pii(record.exc_text)
        if record.exc_info:
            if record.exc_info[0] is not None:
                record.exc_text = mask_pii("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        if record.stack_info:
            record.stack_info = mask_pii(record.stack_info)
        return True


def install_pii_filter(target: logging.Logger | logging.Handler) -> PiiMaskingFilter:
    with _LOGGING_LOCK:
        for installed in target.filters:
            if isinstance(installed, PiiMaskingFilter):
                return installed
        pii_filter = PiiMaskingFilter()
        target.addFilter(pii_filter)
        return pii_filter


def configure_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    app = logging.getLogger("banco_agil")
    with _LOGGING_LOCK:
        for handler in (*root.handlers, *app.handlers):
            install_pii_filter(handler)
        if not root.handlers:
            handler = logging.StreamHandler()
            install_pii_filter(handler)
            root.addHandler(handler)
        app.setLevel(level)
