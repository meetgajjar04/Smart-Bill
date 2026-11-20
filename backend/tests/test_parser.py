from datetime import date

from app.services.parser import parse_bill_text

SAMPLE = """ADANI ELECTRICITY MUMBAI LIMITED
Consumer No: 150234567
Bill Date: 05/10/2026
Units Consumed: 312 kWh
Total Amount Payable: Rs. 2,584.50
Due Date: 20 Oct 2026
"""


def test_parse_electricity_bill():
    p = parse_bill_text(SAMPLE)
    assert p.category == "electricity" and p.provider == "Adani Electricity"
    assert p.amount == 2584.50 and p.units == 312
    assert p.bill_date == date(2026, 10, 5) and p.due_date == date(2026, 10, 20)
    assert p.account_number == "150234567" and p.confidence == 1.0


def test_parse_mobile_and_missing_fields():
    p = parse_bill_text("Jio Postpaid\nMobile Number: 9876543210\nAmount Due ₹399.00")
    assert p.category == "mobile" and p.amount == 399.0 and p.due_date is None
    assert "due_date" in p.missing and p.confidence < 0.7


def test_garbage_text_is_safe():
    p = parse_bill_text("lorem ipsum 12345")
    assert p.amount is None and p.confidence == 0


def test_units_are_not_read_from_an_account_number_on_the_next_line():
    p = parse_bill_text(
        "ADANI ELECTRICITY\nConsumer No: 123456789\nBill Date: 01/11/2026\n"
        "Units Used: 310 kWh\nAmount Due: Rs 4,151.00\nDue Date: 20/11/2026"
    )
    assert p.account_number == "123456789"
    assert p.units == 310


def test_labels_split_across_ocr_lines():
    """OCR often breaks multi-word labels over two lines; that must still parse."""
    p = parse_bill_text("TOTAL AMOUNT\nPAYABLE    1,234.56\nDUE\nDATE 20 Oct 2026")
    assert p.amount == 1234.56
    assert p.due_date == date(2026, 10, 20)
