"""Strict scoring for natural-language multiple-choice demonstrations."""
import re


def explicit_choice(text):
    declarations = re.findall(r'选择\s*[：:]\s*(无法确定|[ABCD])(?=$|[\s.。，、：:])', text)
    if len(declarations) != 1 or declarations[0] == '无法确定':
        return None
    # Preserve a contradictory or self-revised response as invalid, rather
    # than rewarding its first (possibly subsequently withdrawn) letter.
    alternatives = re.findall(r'(?:答案(?:应)?为|应选|修正为)\s*[：:]?\s*([ABCD])(?=$|[\s.。，、：:])', text)
    if any(choice != declarations[0] for choice in alternatives):
        return None
    return declarations[0]
