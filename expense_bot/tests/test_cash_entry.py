from conversation import CASH_AMOUNT_REGEX


def test_cash_amount_regex_matches_bare_number():
    assert CASH_AMOUNT_REGEX.match("150")
    assert CASH_AMOUNT_REGEX.match("150.50")
    assert CASH_AMOUNT_REGEX.match("1,200")
    assert CASH_AMOUNT_REGEX.match("150 บาท")
    assert CASH_AMOUNT_REGEX.match("฿150")
    assert CASH_AMOUNT_REGEX.match("  99.99  ")


def test_cash_amount_regex_rejects_non_amount_text():
    assert not CASH_AMOUNT_REGEX.match("สรุปค่าใช้จ่าย")
    assert not CASH_AMOUNT_REGEX.match("hello 150")
    assert not CASH_AMOUNT_REGEX.match("150 coffee with friends")
    assert not CASH_AMOUNT_REGEX.match("")
    assert not CASH_AMOUNT_REGEX.match("TXN123456789")
