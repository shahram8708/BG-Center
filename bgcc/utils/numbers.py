"""Deterministic amount-to-words conversion.

Legally an amount-in-words figure must be mechanically exact, so this is a
pure-Python utility - never an AI call. Supports the Indian numbering system
(units of lakh and crore) appropriate for INR, falling back to the short-scale
for other currencies.
"""
from decimal import Decimal, InvalidOperation

_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
         "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
         "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _two_digits(n):
    n = int(n)
    if n < 20:
        return _ONES[n]
    return (_TENS[n // 10] + " " + _ONES[n % 10]).strip()


def _three_digits(n):
    n = int(n)
    if n < 100:
        return _two_digits(n)
    h = n // 100
    return (_ONES[h] + " Hundred " + _two_digits(n % 100)).strip()


def _indian_number(n):
    """Convert an integer to words using the Indian (lakh/crore) system."""
    if n == 0:
        return "Zero"
    parts = []
    crore = n // 10000000
    n %= 10000000
    lakh = n // 100000
    n %= 100000
    thousand = n // 1000
    n %= 1000
    if crore:
        parts.append(_indian_number(crore) + " Crore")
    if lakh:
        parts.append(_two_digits(lakh) + " Lakh")
    if thousand:
        parts.append(_two_digits(thousand) + " Thousand")
    if n:
        parts.append(_three_digits(n))
    return " ".join(parts)


def _short_scale_number(n):
    """Convert an integer to words using short-scale groups (thousand/million/billion)."""
    if n == 0:
        return "Zero"
    units = ["", "Thousand", "Million", "Billion", "Trillion"]
    parts = []
    i = 0
    while n:
        chunk = n % 1000
        if chunk:
            label = _three_digits(chunk) + ((" " + units[i]) if units[i] else "")
            parts.append(label)
        n //= 1000
        i += 1
    return " ".join(reversed(parts))


def amount_in_words(amount, currency="INR"):
    """Return the amount as words, e.g. 'Two Lakh Fifty Thousand Rupees Only'.

    `amount` may be a str, int, float, or Decimal. For INR the Indian numbering
    system is used; other currencies use short-scale grouping.
    """
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError, TypeError):
        return ""

    negative = value < 0
    value = abs(value)
    whole = int(value)
    paise = int(round((value - whole) * 100))

    if currency.upper() == "INR":
        whole_words = _indian_number(whole)
        unit = "Rupees"
        sub = "Paise"
    else:
        whole_words = _short_scale_number(whole)
        unit = currency.upper()
        sub = "Cents"

    parts = [whole_words, unit]
    if paise:
        parts.append("and")
        parts.append(_two_digits(paise))
        parts.append(sub)
    parts.append("Only")
    text = " ".join(parts)
    return ("Minus " + text) if negative else text
