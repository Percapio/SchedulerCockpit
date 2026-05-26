"""Build metadata injected by CI."""

from dataclasses import dataclass

@dataclass(frozen=True)
class BuildInfo:
    version: str
    commit: str
    built_at_utc: str

def get_build_info() -> BuildInfo:
    """Get the build info, using sentinels for source runs."""
    return BuildInfo(
        version="0.0.0-dev",
        commit="source",
        built_at_utc="source"
    )
