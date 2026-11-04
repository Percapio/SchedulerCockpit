"""Shared label normalisation for ingestion parsers."""


def header_key(raw_label: str) -> str:
    """Comparison key for a header label.

    post: casefolded, with every Unicode whitespace code point (including the
          non-breaking space Word inserts) and every "_" removed; "Ref Des",
          "REF_DES", " ref_des " and "Ref_Des" all map to "refdes"
    """
    return "".join(ch for ch in raw_label if not ch.isspace() and ch != "_").casefold()
