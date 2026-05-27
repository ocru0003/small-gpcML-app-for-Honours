from lxml import etree

def run_custom_rules(xml_tree: etree._Element):
    """
    Runs scientific / logical rules that XSD 1.0 cannot express.
    Returns a list of error strings (with line numbers where possible).
    """
    errors = []

    # ------------------------------------------------------------
    # Rule 1: PoreSizeMin <= PoreSizeMax
    # ------------------------------------------------------------
    for col in xml_tree.xpath("//Column | //GuardColumn"):
        min_val = col.get("PoreSizeMin")
        max_val = col.get("PoreSizeMax")

        if min_val and max_val:
            if int(min_val) > int(max_val):
                line = col.sourceline   # <-- THIS IS THE NEW PART
                errors.append(
                    f"Line {line}: PoreSizeMin ({min_val}) > "
                    f"PoreSizeMax ({max_val}) in <{col.tag}>"
                )

    # ------------------------------------------------------------
    # Rule 2: Ratio parts must sum to 100
    # ------------------------------------------------------------
    for ratio in xml_tree.xpath("//Ratio"):
        parts = ratio.text.split(":")

        try:
            total = sum(int(p) for p in parts)

            if total != 100:
                line = ratio.sourceline   # <-- THIS IS THE NEW PART
                errors.append(
                    f"Line {line}: Ratio '{ratio.text}' does not sum to 100"
                )

        except ValueError:
            line = ratio.sourceline      # <-- ALSO HERE
            errors.append(
                f"Line {line}: Invalid Ratio format '{ratio.text}'"
            )

    # ------------------------------------------------------------
    # Rule 3: Disallow NaN / Infinity explicitly
    # ------------------------------------------------------------
    for el in xml_tree.xpath("//*[text()]"):
        if el.text in {"NaN", "Infinity", "-Infinity"}:
            line = el.sourceline         # <-- AND HERE
            errors.append(
                f"Line {line}: Illegal numeric value '{el.text}' "
                f"in <{el.tag}>"
            )

    return errors
