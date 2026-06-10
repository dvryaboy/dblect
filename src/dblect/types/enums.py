"""Enum field types: the categorical vocabularies a domain type draws on.

Two marker bases divide the categories by how the substrate carries them. A
:class:`UnitEnum` is a *dimensional* unit (a currency), so it rides in a tag's
dimensional monomial and does exponent arithmetic under ``*`` and ``/``. A
:class:`NominalEnum` is a *nominal* category (a country code), carried by
equality only. Both subclass :class:`enum.StrEnum`, so a member equals its
string code and ``Currency("USD")`` round-trips a literal, which is what lets a
contract accept ``currency="USD"`` and ``currency=Currency.USD`` alike (an
out-of-domain literal is a finding, raised by neither the enum nor here).

The vocabularies are deliberately partial: a representative slice of ISO 4217
and ISO 3166-1 alpha-2, enough to exercise the algebra. Widening them is a data
edit, not a design change.
"""

from __future__ import annotations

from enum import StrEnum


class UnitEnum(StrEnum):
    """A dimensional unit: a category that multiplies and divides (a currency)."""


class NominalEnum(StrEnum):
    """A nominal category carried by equality (a country, a region code)."""


class Currency(UnitEnum):
    """ISO 4217 currency codes (a representative slice)."""

    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"
    JPY = "JPY"
    CHF = "CHF"
    CAD = "CAD"
    AUD = "AUD"
    NZD = "NZD"
    SEK = "SEK"
    NOK = "NOK"
    DKK = "DKK"
    CNY = "CNY"
    HKD = "HKD"
    SGD = "SGD"
    INR = "INR"
    BRL = "BRL"
    MXN = "MXN"
    ZAR = "ZAR"
    PLN = "PLN"
    KRW = "KRW"


class Country(NominalEnum):
    """ISO 3166-1 alpha-2 country codes (a representative slice)."""

    US = "US"
    GB = "GB"
    DE = "DE"
    FR = "FR"
    ES = "ES"
    IT = "IT"
    NL = "NL"
    SE = "SE"
    NO = "NO"
    DK = "DK"
    CH = "CH"
    CA = "CA"
    AU = "AU"
    NZ = "NZ"
    JP = "JP"
    CN = "CN"
    HK = "HK"
    SG = "SG"
    IN = "IN"
    BR = "BR"
    MX = "MX"
    ZA = "ZA"
    PL = "PL"
    KR = "KR"
