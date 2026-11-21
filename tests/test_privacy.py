import logging
from io import StringIO

import pytest

from banco_agil.infrastructure.privacy import (
    REDACTED_PLACEHOLDER,
    PiiMaskingFilter,
    configure_logging,
    install_pii_filter,
    mask_pii,
    mask_pii_data,
)


@pytest.mark.parametrize(
    "sensitive",
    [
        "12345678901",
        "123.456.789-01",
        "123 456 789 01",
        "14/05/1990",
        "14-05-1990",
        "14.05.1990",
        "1990-05-14",
        "Bearer example.header_signature+/=",
        "gsk_example",
        "sk-example",
        "AIzaExample",
        "AKIAEXAMPLE",
        "pk-lf-example",
        "sk-lf-example",
    ],
)
def test_masks_sensitive_values(sensitive):
    assert mask_pii(f"Dado: {sensitive}.") == f"Dado: {REDACTED_PLACEHOLDER}."


def test_preserves_dates_other_than_the_verified_birth_date():
    text = "14/05/1990, 14-05-1990, 14.05.1990, 1990-05-14; BCE 2026-11-20; 31/02/2020."
    assert mask_pii(text, preserve_dates_except="1990-05-14") == (
        f"{', '.join([REDACTED_PLACEHOLDER] * 4)}; BCE 2026-11-20; 31/02/2020."
    )


def test_masks_nested_data_without_mutating_it():
    original = {"cpf": ["12345678901", ("14/05/1990", 42, False, None)], "gsk_example": "BC"}
    masked = mask_pii_data(original)
    assert masked == {
        "cpf": [REDACTED_PLACEHOLDER, (REDACTED_PLACEHOLDER, 42, False, None)],
        REDACTED_PLACEHOLDER: "BC",
    }
    assert original == {
        "cpf": ["12345678901", ("14/05/1990", 42, False, None)],
        "gsk_example": "BC",
    }
    assert masked is not original
    assert masked["cpf"] is not original["cpf"]


@pytest.mark.parametrize(
    ("msg", "args", "expected"),
    [
        ("%s a 100%%", ("123.456.789-01",), "[dado omitido] a 100%"),
        ("%(date)s a 100%%", ({"date": "1990-05-14"},), "[dado omitido] a 100%"),
        ("%d 12345678901", ("incompatível",), "%d [dado omitido]"),
        ("%y gsk_example", ("incompatível",), "%y [dado omitido]"),
        ("%(missing)s gsk_example", ({"other": "dado"},), "%(missing)s [dado omitido]"),
    ],
)
def test_filter_formats_and_masks_log_arguments(msg, args, expected):
    record = logging.LogRecord("test", logging.INFO, __file__, 1, msg, args, None)
    record.exc_text = "Erro: 14/05/1990"
    record.stack_info = "Stack: Bearer placeholder"
    assert PiiMaskingFilter().filter(record)
    assert record.getMessage() == expected
    assert record.args is None
    assert record.exc_text == "Erro: [dado omitido]"
    assert record.stack_info == "Stack: [dado omitido]"


def test_log_exceptions_are_masked_before_handlers_emit(isolated_logging, caplog):
    configure_logging()
    logger = logging.getLogger("banco_agil.child")
    try:
        raise RuntimeError("12345678901, 14/05/1990, gsk_example")
    except RuntimeError:
        logger.exception("Falha: %s", "123.456.789-01")

    [record] = caplog.records
    assert record.exc_info is None
    assert record.exc_text.endswith(
        "RuntimeError: [dado omitido], [dado omitido], [dado omitido]\n"
    )
    assert "Falha: [dado omitido]" in caplog.text
    for secret in ("12345678901", "123.456.789-01", "14/05/1990", "gsk_example"):
        assert secret not in caplog.text


def test_invalid_exception_tuple_is_cleared():
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "Mensagem", (), (None, None, None)
    )
    assert PiiMaskingFilter().filter(record)
    assert record.exc_info is None
    assert record.exc_text is None


def test_unformattable_log_message_has_safe_fallback():
    class InvalidMessage:
        def __str__(self):
            raise ValueError("12345678901")

    record = logging.LogRecord("test", logging.INFO, __file__, 1, InvalidMessage(), (), None)
    assert PiiMaskingFilter().filter(record)
    assert record.getMessage() == "Mensagem indisponível."


def test_logging_configuration_is_idempotent_and_covers_propagated_logs(isolated_logging):
    root = logging.getLogger()
    app = logging.getLogger("banco_agil")
    root.handlers.clear()
    stream = StringIO()
    app_handler = logging.StreamHandler(stream)
    app.addHandler(app_handler)

    configure_logging(logging.DEBUG)
    [root_handler] = root.handlers
    assert isinstance(root_handler, logging.StreamHandler)
    root_handler.setStream(stream)
    configure_logging(logging.DEBUG)

    assert root.handlers == [root_handler]
    assert app.level == logging.DEBUG
    for handler in (root_handler, app_handler):
        assert handler.filters == [install_pii_filter(handler)]
    logging.getLogger("banco_agil.child").debug("CPF %s", "12345678901")
    logging.getLogger("test.third_party").warning("Chave: %s", "gsk_example")
    assert stream.getvalue().splitlines() == [
        "CPF [dado omitido]",
        "CPF [dado omitido]",
        "Chave: [dado omitido]",
    ]


def test_filter_can_also_be_installed_on_a_logger():
    logger = logging.Logger("test")
    pii_filter = install_pii_filter(logger)
    assert install_pii_filter(logger) is pii_filter
    assert logger.filters == [pii_filter]
