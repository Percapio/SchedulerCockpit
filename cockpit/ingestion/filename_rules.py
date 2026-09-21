"""Filename parsing rules."""

import pathlib
import re

def derive_part_number_from_filename(path: pathlib.Path) -> str:
    """Extract the base part number from a given file path.
    
    The rule is: the part number is the first whitespace-delimited token 
    of the file's name.
    """
    return path.name.split()[0].strip()


# Phase 51 section 4.2: article subfolders are named by bare ordinal, with
# optional trailing text -- "2nd", "3rd Article", "4TH_REV_B". Permissive on the
# suffix ("2th" matches) deliberately: rejecting a real folder over a typo in
# its name is the worse failure, and a wrong ordinal is visible immediately in
# the replacement confirmation and the Audit List column.
ARTICLE_FOLDER_GRAMMAR = re.compile(r"^(?P<ordinal>\d{1,2})(ST|ND|RD|TH)(\b|_)", re.IGNORECASE)


def article_designation(folder_name: str) -> str | None:
    """The article designation a folder name declares, or None.

    post: returns a normalised lower-case ordinal ("2nd", "3rd"); the first
          article is "FA" and is never produced here, because it has no folder
    """
    match = ARTICLE_FOLDER_GRAMMAR.match(folder_name.strip())
    if not match:
        return None
    return f"{match.group('ordinal')}{match.group(2).lower()}"
