"""Judge 2026-10-13.1: small product-label text is not a visual defect.

Small, illegible or garbled lettering printed on the product or its packaging
(label fine print) was judged inconsistently under ``no_visual_defects``; it is
now explicitly exempt, while requested in-image text (``text_correct``) is still
judged exactly and large ad-like gibberish stays a defect.
"""

import re

from creative_eval import prompts as eval_prompts
from creative_eval.dimensions import JUDGE_VERSION

VIS = eval_prompts.VISUAL_CONCEPT_EVAL_USER


def _gate_line(name: str) -> str:
    match = re.search(rf"^\d+\. \*\*{name}\*\*:.*$", VIS, flags=re.MULTILINE)
    assert match, f"gate {name} missing from the visual prompt"
    return match.group(0)


def test_judge_version_current():
    assert JUDGE_VERSION == "2026-10-15"


def test_no_visual_defects_exempts_product_label_text():
    line = _gate_line("no_visual_defects")
    assert (
        "Small or illegible text printed on the product or its packaging "
        "(label fine print, small lettering) is not a defect; judge the product's "
        "shape, parts and placement instead." in line
    )


def test_no_visual_defects_keeps_large_ad_gibberish_a_defect():
    line = _gate_line("no_visual_defects")
    assert "Large, prominent garbled text that reads as part of the ad" in line
    assert "is still a defect" in line


def test_text_correct_still_judges_requested_text_exactly():
    line = _gate_line("text_correct")
    assert "appears exactly as quoted and is legible, with no garbled lettering" in line
    assert "the requested text clearly appears exactly as quoted" in VIS
    # Product label fine print is not requested in-image text for this gate.
    assert (
        "Small text printed on the product or its packaging (label fine print) "
        "is not requested in-image text" in line
    )
