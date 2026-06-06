"""Filename parsing rules."""

import pathlib

def derive_part_number_from_filename(path: pathlib.Path) -> str:
    """Extract the base part number from a given file path.
    
    The rule is: the part number is the first whitespace-delimited token 
    of the file's name.
    """
    return path.name.split()[0].strip()
