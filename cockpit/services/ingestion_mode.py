"""How a New Job request was made, and what that implies.

The mode is chosen before anything is read, and it decides two things the
application cannot infer from the files themselves: where to look for the
required roles, and which article the result is.
"""

from enum import Enum


class IngestionMode(Enum):
    STANDARD = "STANDARD"
    SPLIT = "SPLIT"
    ARTICLE_REVISION = "ARTICLE_REVISION"

    @property
    def label(self) -> str:
        return {
            IngestionMode.STANDARD: "Standard",
            IngestionMode.SPLIT: "Split Job",
            IngestionMode.ARTICLE_REVISION: "Article Revision",
        }[self]

    @property
    def scope(self):
        """Where locate() looks for the BOM, traveler and ECO."""
        from cockpit.ingestion.locator import IngestionScope

        if self is IngestionMode.ARTICLE_REVISION:
            return IngestionScope.ARTICLE_SUBFOLDER
        return IngestionScope.ROOT

    @property
    def prompts_for_quantity(self) -> bool:
        """Only an article revision carries a quantity the documents got wrong."""
        return self is IngestionMode.ARTICLE_REVISION

    @property
    def splits_after_ingest(self) -> bool:
        return self is IngestionMode.SPLIT
