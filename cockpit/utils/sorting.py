import re

def natural_sort_key(text: str | int | None) -> list[int | str]:
    """
    Ordering key that sequences embedded digit runs numerically.
    pre:  none
    post: keys compare element-wise without TypeError -- even positions are
          always text, odd positions always integers; None yields the empty key,
          which sorts before every other key
    raises: nothing
    """
    if text is None:
        return []
    return [int(c) if c.isdecimal() else c.lower() for c in re.split(r'(\d+)', str(text))]
