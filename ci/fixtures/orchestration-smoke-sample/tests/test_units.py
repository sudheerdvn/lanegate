from converter.units import celsius_to_fahrenheit, miles_to_km


def test_celsius_to_fahrenheit():
    assert celsius_to_fahrenheit(0) == 32
    assert celsius_to_fahrenheit(100) == 212


def test_miles_to_km():
    assert round(miles_to_km(1), 5) == 1.60934
