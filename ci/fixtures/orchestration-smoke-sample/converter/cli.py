import argparse

from converter.units import celsius_to_fahrenheit, miles_to_km

# TICK-415: this batch of small validators exists to push this file's own
# AST skeleton (see lanegate/analyze.py:_build_file_skeleton) past the 10KB
# inline-prompt threshold (lanegate/executor.py:_SKELETON_INLINE_THRESHOLD_BYTES).
# TICK-403's discovery-guidance A/B fixture stayed under that threshold and
# never exercised the sidecar-contradiction code path fixed in 790066d --
# these functions are the fix for that gap, not real converter behavior.
def validate_length_001(value: float, *, strict: bool = False) -> bool:
    """Validate length constraint #1 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_range_001(value: float, *, strict: bool = False) -> bool:
    """Validate range constraint #1 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_precision_001(value: float, *, strict: bool = False) -> bool:
    """Validate precision constraint #1 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_unit_001(value: float, *, strict: bool = False) -> bool:
    """Validate unit constraint #1 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_sign_001(value: float, *, strict: bool = False) -> bool:
    """Validate sign constraint #1 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_format_001(value: float, *, strict: bool = False) -> bool:
    """Validate format constraint #1 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_rounding_001(value: float, *, strict: bool = False) -> bool:
    """Validate rounding constraint #1 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_bounds_001(value: float, *, strict: bool = False) -> bool:
    """Validate bounds constraint #1 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_scale_001(value: float, *, strict: bool = False) -> bool:
    """Validate scale constraint #1 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_offset_001(value: float, *, strict: bool = False) -> bool:
    """Validate offset constraint #1 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_length_002(value: float, *, strict: bool = False) -> bool:
    """Validate length constraint #2 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_range_002(value: float, *, strict: bool = False) -> bool:
    """Validate range constraint #2 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_precision_002(value: float, *, strict: bool = False) -> bool:
    """Validate precision constraint #2 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_unit_002(value: float, *, strict: bool = False) -> bool:
    """Validate unit constraint #2 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_sign_002(value: float, *, strict: bool = False) -> bool:
    """Validate sign constraint #2 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_format_002(value: float, *, strict: bool = False) -> bool:
    """Validate format constraint #2 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_rounding_002(value: float, *, strict: bool = False) -> bool:
    """Validate rounding constraint #2 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_bounds_002(value: float, *, strict: bool = False) -> bool:
    """Validate bounds constraint #2 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_scale_002(value: float, *, strict: bool = False) -> bool:
    """Validate scale constraint #2 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_offset_002(value: float, *, strict: bool = False) -> bool:
    """Validate offset constraint #2 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_length_003(value: float, *, strict: bool = False) -> bool:
    """Validate length constraint #3 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_range_003(value: float, *, strict: bool = False) -> bool:
    """Validate range constraint #3 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_precision_003(value: float, *, strict: bool = False) -> bool:
    """Validate precision constraint #3 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_unit_003(value: float, *, strict: bool = False) -> bool:
    """Validate unit constraint #3 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_sign_003(value: float, *, strict: bool = False) -> bool:
    """Validate sign constraint #3 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_format_003(value: float, *, strict: bool = False) -> bool:
    """Validate format constraint #3 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_rounding_003(value: float, *, strict: bool = False) -> bool:
    """Validate rounding constraint #3 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_bounds_003(value: float, *, strict: bool = False) -> bool:
    """Validate bounds constraint #3 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_scale_003(value: float, *, strict: bool = False) -> bool:
    """Validate scale constraint #3 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_offset_003(value: float, *, strict: bool = False) -> bool:
    """Validate offset constraint #3 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_length_004(value: float, *, strict: bool = False) -> bool:
    """Validate length constraint #4 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_range_004(value: float, *, strict: bool = False) -> bool:
    """Validate range constraint #4 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_precision_004(value: float, *, strict: bool = False) -> bool:
    """Validate precision constraint #4 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_unit_004(value: float, *, strict: bool = False) -> bool:
    """Validate unit constraint #4 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_sign_004(value: float, *, strict: bool = False) -> bool:
    """Validate sign constraint #4 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_format_004(value: float, *, strict: bool = False) -> bool:
    """Validate format constraint #4 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_rounding_004(value: float, *, strict: bool = False) -> bool:
    """Validate rounding constraint #4 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_bounds_004(value: float, *, strict: bool = False) -> bool:
    """Validate bounds constraint #4 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_scale_004(value: float, *, strict: bool = False) -> bool:
    """Validate scale constraint #4 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_offset_004(value: float, *, strict: bool = False) -> bool:
    """Validate offset constraint #4 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_length_005(value: float, *, strict: bool = False) -> bool:
    """Validate length constraint #5 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_range_005(value: float, *, strict: bool = False) -> bool:
    """Validate range constraint #5 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_precision_005(value: float, *, strict: bool = False) -> bool:
    """Validate precision constraint #5 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_unit_005(value: float, *, strict: bool = False) -> bool:
    """Validate unit constraint #5 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_sign_005(value: float, *, strict: bool = False) -> bool:
    """Validate sign constraint #5 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_format_005(value: float, *, strict: bool = False) -> bool:
    """Validate format constraint #5 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_rounding_005(value: float, *, strict: bool = False) -> bool:
    """Validate rounding constraint #5 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_bounds_005(value: float, *, strict: bool = False) -> bool:
    """Validate bounds constraint #5 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_scale_005(value: float, *, strict: bool = False) -> bool:
    """Validate scale constraint #5 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_offset_005(value: float, *, strict: bool = False) -> bool:
    """Validate offset constraint #5 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_length_006(value: float, *, strict: bool = False) -> bool:
    """Validate length constraint #6 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_range_006(value: float, *, strict: bool = False) -> bool:
    """Validate range constraint #6 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_precision_006(value: float, *, strict: bool = False) -> bool:
    """Validate precision constraint #6 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_unit_006(value: float, *, strict: bool = False) -> bool:
    """Validate unit constraint #6 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_sign_006(value: float, *, strict: bool = False) -> bool:
    """Validate sign constraint #6 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_format_006(value: float, *, strict: bool = False) -> bool:
    """Validate format constraint #6 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_rounding_006(value: float, *, strict: bool = False) -> bool:
    """Validate rounding constraint #6 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_bounds_006(value: float, *, strict: bool = False) -> bool:
    """Validate bounds constraint #6 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_scale_006(value: float, *, strict: bool = False) -> bool:
    """Validate scale constraint #6 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_offset_006(value: float, *, strict: bool = False) -> bool:
    """Validate offset constraint #6 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_length_007(value: float, *, strict: bool = False) -> bool:
    """Validate length constraint #7 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_range_007(value: float, *, strict: bool = False) -> bool:
    """Validate range constraint #7 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_precision_007(value: float, *, strict: bool = False) -> bool:
    """Validate precision constraint #7 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_unit_007(value: float, *, strict: bool = False) -> bool:
    """Validate unit constraint #7 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_sign_007(value: float, *, strict: bool = False) -> bool:
    """Validate sign constraint #7 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_format_007(value: float, *, strict: bool = False) -> bool:
    """Validate format constraint #7 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_rounding_007(value: float, *, strict: bool = False) -> bool:
    """Validate rounding constraint #7 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_bounds_007(value: float, *, strict: bool = False) -> bool:
    """Validate bounds constraint #7 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_scale_007(value: float, *, strict: bool = False) -> bool:
    """Validate scale constraint #7 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_offset_007(value: float, *, strict: bool = False) -> bool:
    """Validate offset constraint #7 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_length_008(value: float, *, strict: bool = False) -> bool:
    """Validate length constraint #8 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_range_008(value: float, *, strict: bool = False) -> bool:
    """Validate range constraint #8 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_precision_008(value: float, *, strict: bool = False) -> bool:
    """Validate precision constraint #8 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_unit_008(value: float, *, strict: bool = False) -> bool:
    """Validate unit constraint #8 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_sign_008(value: float, *, strict: bool = False) -> bool:
    """Validate sign constraint #8 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_format_008(value: float, *, strict: bool = False) -> bool:
    """Validate format constraint #8 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_rounding_008(value: float, *, strict: bool = False) -> bool:
    """Validate rounding constraint #8 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_bounds_008(value: float, *, strict: bool = False) -> bool:
    """Validate bounds constraint #8 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_scale_008(value: float, *, strict: bool = False) -> bool:
    """Validate scale constraint #8 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_offset_008(value: float, *, strict: bool = False) -> bool:
    """Validate offset constraint #8 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_length_009(value: float, *, strict: bool = False) -> bool:
    """Validate length constraint #9 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_range_009(value: float, *, strict: bool = False) -> bool:
    """Validate range constraint #9 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_precision_009(value: float, *, strict: bool = False) -> bool:
    """Validate precision constraint #9 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_unit_009(value: float, *, strict: bool = False) -> bool:
    """Validate unit constraint #9 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_sign_009(value: float, *, strict: bool = False) -> bool:
    """Validate sign constraint #9 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_format_009(value: float, *, strict: bool = False) -> bool:
    """Validate format constraint #9 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_rounding_009(value: float, *, strict: bool = False) -> bool:
    """Validate rounding constraint #9 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_bounds_009(value: float, *, strict: bool = False) -> bool:
    """Validate bounds constraint #9 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_scale_009(value: float, *, strict: bool = False) -> bool:
    """Validate scale constraint #9 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_offset_009(value: float, *, strict: bool = False) -> bool:
    """Validate offset constraint #9 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_length_010(value: float, *, strict: bool = False) -> bool:
    """Validate length constraint #10 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_range_010(value: float, *, strict: bool = False) -> bool:
    """Validate range constraint #10 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_precision_010(value: float, *, strict: bool = False) -> bool:
    """Validate precision constraint #10 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_unit_010(value: float, *, strict: bool = False) -> bool:
    """Validate unit constraint #10 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_sign_010(value: float, *, strict: bool = False) -> bool:
    """Validate sign constraint #10 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_format_010(value: float, *, strict: bool = False) -> bool:
    """Validate format constraint #10 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_rounding_010(value: float, *, strict: bool = False) -> bool:
    """Validate rounding constraint #10 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_bounds_010(value: float, *, strict: bool = False) -> bool:
    """Validate bounds constraint #10 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_scale_010(value: float, *, strict: bool = False) -> bool:
    """Validate scale constraint #10 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_offset_010(value: float, *, strict: bool = False) -> bool:
    """Validate offset constraint #10 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_length_011(value: float, *, strict: bool = False) -> bool:
    """Validate length constraint #11 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_range_011(value: float, *, strict: bool = False) -> bool:
    """Validate range constraint #11 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_precision_011(value: float, *, strict: bool = False) -> bool:
    """Validate precision constraint #11 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_unit_011(value: float, *, strict: bool = False) -> bool:
    """Validate unit constraint #11 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_sign_011(value: float, *, strict: bool = False) -> bool:
    """Validate sign constraint #11 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_format_011(value: float, *, strict: bool = False) -> bool:
    """Validate format constraint #11 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_rounding_011(value: float, *, strict: bool = False) -> bool:
    """Validate rounding constraint #11 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_bounds_011(value: float, *, strict: bool = False) -> bool:
    """Validate bounds constraint #11 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_scale_011(value: float, *, strict: bool = False) -> bool:
    """Validate scale constraint #11 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_offset_011(value: float, *, strict: bool = False) -> bool:
    """Validate offset constraint #11 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_length_012(value: float, *, strict: bool = False) -> bool:
    """Validate length constraint #12 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_range_012(value: float, *, strict: bool = False) -> bool:
    """Validate range constraint #12 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_precision_012(value: float, *, strict: bool = False) -> bool:
    """Validate precision constraint #12 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_unit_012(value: float, *, strict: bool = False) -> bool:
    """Validate unit constraint #12 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_sign_012(value: float, *, strict: bool = False) -> bool:
    """Validate sign constraint #12 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_format_012(value: float, *, strict: bool = False) -> bool:
    """Validate format constraint #12 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_rounding_012(value: float, *, strict: bool = False) -> bool:
    """Validate rounding constraint #12 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_bounds_012(value: float, *, strict: bool = False) -> bool:
    """Validate bounds constraint #12 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_scale_012(value: float, *, strict: bool = False) -> bool:
    """Validate scale constraint #12 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_offset_012(value: float, *, strict: bool = False) -> bool:
    """Validate offset constraint #12 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_length_013(value: float, *, strict: bool = False) -> bool:
    """Validate length constraint #13 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_range_013(value: float, *, strict: bool = False) -> bool:
    """Validate range constraint #13 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_precision_013(value: float, *, strict: bool = False) -> bool:
    """Validate precision constraint #13 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_unit_013(value: float, *, strict: bool = False) -> bool:
    """Validate unit constraint #13 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_sign_013(value: float, *, strict: bool = False) -> bool:
    """Validate sign constraint #13 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_format_013(value: float, *, strict: bool = False) -> bool:
    """Validate format constraint #13 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_rounding_013(value: float, *, strict: bool = False) -> bool:
    """Validate rounding constraint #13 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_bounds_013(value: float, *, strict: bool = False) -> bool:
    """Validate bounds constraint #13 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_scale_013(value: float, *, strict: bool = False) -> bool:
    """Validate scale constraint #13 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_offset_013(value: float, *, strict: bool = False) -> bool:
    """Validate offset constraint #13 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_length_014(value: float, *, strict: bool = False) -> bool:
    """Validate length constraint #14 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_range_014(value: float, *, strict: bool = False) -> bool:
    """Validate range constraint #14 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_precision_014(value: float, *, strict: bool = False) -> bool:
    """Validate precision constraint #14 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_unit_014(value: float, *, strict: bool = False) -> bool:
    """Validate unit constraint #14 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_sign_014(value: float, *, strict: bool = False) -> bool:
    """Validate sign constraint #14 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_format_014(value: float, *, strict: bool = False) -> bool:
    """Validate format constraint #14 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_rounding_014(value: float, *, strict: bool = False) -> bool:
    """Validate rounding constraint #14 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_bounds_014(value: float, *, strict: bool = False) -> bool:
    """Validate bounds constraint #14 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_scale_014(value: float, *, strict: bool = False) -> bool:
    """Validate scale constraint #14 for a converter input."""
    if strict and value < 0:
        return False
    return True


def validate_offset_014(value: float, *, strict: bool = False) -> bool:
    """Validate offset constraint #14 for a converter input."""
    if strict and value < 0:
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser(prog="converter")
    sub = parser.add_subparsers(dest="command", required=True)

    c2f = sub.add_parser("c2f", help="Celsius to Fahrenheit")
    c2f.add_argument("value", type=float)

    m2km = sub.add_parser("m2km", help="Miles to kilometers")
    m2km.add_argument("value", type=float)

    args = parser.parse_args()

    if args.command == "c2f":
        print(celsius_to_fahrenheit(args.value))
    elif args.command == "m2km":
        print(miles_to_km(args.value))


if __name__ == "__main__":
    main()
