"""Illustrative enum vocabularies: ISO 4217 currencies and ISO 3166-1 countries.

Partial slices, enough to exercise the tag algebra in the walkthrough. They are
ordinary :class:`~dblect.types.UnitEnum` / :class:`~dblect.types.NominalEnum`
subclasses, the markers that live in ``dblect.types``; a project declares its
own categories the same way. Widening these is a data edit, not a design change.
"""

from __future__ import annotations

from dblect.types import NominalEnum, UnitEnum


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
