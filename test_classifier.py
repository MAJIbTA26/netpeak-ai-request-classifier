from classifier import classify_error, parse_retry_delay


def test_classify_error_recognizes_rate_limit():
    """Перевіряємо, що помилка 429 розпізнається як rate_limit."""
    error_message = "429 RESOURCE_EXHAUSTED. Quota exceeded..."
    assert classify_error(error_message) == "rate_limit"


def test_classify_error_recognizes_temporary_unavailable():
    """Перевіряємо, що помилка 503 розпізнається як temporary_unavailable."""
    error_message = "503 UNAVAILABLE. This model is currently experiencing high demand."
    assert classify_error(error_message) == "temporary_unavailable"


def test_classify_error_recognizes_model_not_found():
    """Перевіряємо, що помилка 404 розпізнається як model_not_found."""
    error_message = "404 NOT_FOUND. This model is no longer available."
    assert classify_error(error_message) == "model_not_found"
    
def test_parse_retry_delay_extracts_seconds_from_google_message():
    """Перевіряємо, що функція правильно витягує час очікування з тексту помилки."""
    error_message = "Please retry in 13.051032916s."
    delay = parse_retry_delay(error_message)
    assert delay == 14.051032916  # 13.05 + 1 секунда "про запас"


def test_parse_retry_delay_returns_default_when_no_delay_found():
    """Перевіряємо, що функція повертає значення за замовчуванням, якщо у тексті немає підказки про час."""
    error_message = "Якась помилка без інформації про затримку"
    delay = parse_retry_delay(error_message)
    assert delay == 5.0  # значення за замовчуванням з функції