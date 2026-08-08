"""F1 -- the byte-budgeted raster cache and the bounded prefetch policy
(Optimize06 sections 3.4 and 4, step 2 gate).

Step 2's gate is a whole-application measurement: peak `raster_cache_bytes`
never exceeds `max_cached_bytes` across the reference-PDF cycle. That bounds
the outcome without pinning any of the contracts the outcome rests on. Six of
the audit's twenty findings (4, 5, 6, 7, 8, 18) exist precisely because those
contracts were unspecified, and a contract that is specified but unasserted
regresses the same way an unspecified one does -- which is F6's lesson.

So each is asserted here directly:
  finding 4  -- estimate_page_bytes errs high and suppresses on no document
  finding 5  -- prefetch candidate ordering, ties toward the LOWER index
  finding 6  -- RasterPageEntries' LRU ordering contract
  finding 7  -- put()'s BUDGET-NOT-RESTORED branch, and that it warns
  finding 8  -- prefetch is SUPPRESSED, not clamped, when over budget
  finding 18 -- byte_size is measured on the source QImage, never via a copy
"""

import pathlib
from unittest.mock import Mock

import pytest
from PyQt6.QtGui import QImage, QPixmap

from cockpit.layout.renderer import PdfRenderer
from cockpit.services.layout_query import LayoutQueryService
from cockpit.services.views import LayoutContext
from cockpit.ui.canvas.layout_canvas import (
    CachedPage,
    LayoutCanvas,
    RasterPageCache,
    RasterPageEntries,
    RenderBudget,
    measure_rendered_bytes,
    pages_ordered_by_distance_from,
)
from cockpit.ui.theme import Theme
from cockpit.ui.workers.render_worker import RenderedImage


RENDER_HEIGHT = 100


@pytest.fixture(autouse=True)
def _gui_application(qapp):
    """CachedPage carries a QPixmap, and QPixmap construction aborts without a
    QGuiApplication. The cache tests are pure logic but still build the record
    the production path builds."""
    return qapp


@pytest.fixture
def theme():
    return Theme.for_testing(
        canvas={
            "colour": {
                "highlight_pen": {"rgb": "#FF00FF"},
                "dim_overlay": {"rgb": "#000000", "alpha": 128},
                "hint_label_background": {"rgb": "#FFFFFF"},
                "hint_label_text": {"rgb": "#000000"},
                "hint_label_border": {"rgb": "#000000"},
            },
            "zoom": {"min_scale": 1.0, "max_scale": 8.0, "step": 1.25, "render_multiplier": 2.0},
            "pen_width": {"highlight_pen": 3},
            "z_order": {"base_pixmap": 0.0, "dim": 1.0, "highlight": 2.0},
            "scalar": {"highlight_scale": 2.0},
            "hint_label": {"padding_px": 4, "border_width_px": 1},
        },
    )


def _page(index: int, size: int, height: int = RENDER_HEIGHT) -> CachedPage:
    return CachedPage(
        page_index=index,
        target_pixel_height=height,
        pixmap=QPixmap(1, 1),
        byte_size=size,
    )


# ---------------------------------------------------------------------------
# Finding 6 -- RasterPageEntries: the LRU structure the audit found unnamed
# ---------------------------------------------------------------------------

def test_entries_iterate_least_recently_used_first():
    entries = RasterPageEntries()
    entries.insert_or_replace_as_most_recent(_page(0, 100))
    entries.insert_or_replace_as_most_recent(_page(1, 100))
    entries.insert_or_replace_as_most_recent(_page(2, 100))

    assert entries.least_recently_used_excluding().page_index == 0


def test_reinserting_a_resident_page_makes_it_most_recent():
    """A dict gives no recency order; the invariant is that re-insertion moves
    the key to the end rather than leaving it where it was."""
    entries = RasterPageEntries()
    entries.insert_or_replace_as_most_recent(_page(0, 100))
    entries.insert_or_replace_as_most_recent(_page(1, 100))

    entries.insert_or_replace_as_most_recent(_page(0, 250))

    assert entries.least_recently_used_excluding().page_index == 1
    assert len(entries) == 2
    assert entries.total_bytes() == 350


def test_touch_promotes_without_changing_membership():
    entries = RasterPageEntries()
    entries.insert_or_replace_as_most_recent(_page(0, 100))
    entries.insert_or_replace_as_most_recent(_page(1, 100))

    entries.touch_as_most_recent(0)

    assert entries.least_recently_used_excluding().page_index == 1
    assert len(entries) == 2


def test_peek_does_not_alter_recency():
    """get() must be able to answer a hit test without promoting the entry, or
    eviction order would depend on who looked."""
    entries = RasterPageEntries()
    entries.insert_or_replace_as_most_recent(_page(0, 100))
    entries.insert_or_replace_as_most_recent(_page(1, 100))

    entries.peek(0)

    assert entries.least_recently_used_excluding().page_index == 0


def test_least_recently_used_skips_every_exempt_index():
    entries = RasterPageEntries()
    entries.insert_or_replace_as_most_recent(_page(0, 100))
    entries.insert_or_replace_as_most_recent(_page(1, 100))
    entries.insert_or_replace_as_most_recent(_page(2, 100))

    assert entries.least_recently_used_excluding(0, 1).page_index == 2


def test_least_recently_used_is_none_when_everything_is_exempt():
    """The condition that terminates put()'s eviction loop."""
    entries = RasterPageEntries()
    entries.insert_or_replace_as_most_recent(_page(0, 100))
    entries.insert_or_replace_as_most_recent(_page(1, 100))

    assert entries.least_recently_used_excluding(0, 1) is None


def test_remove_and_clear_drop_the_bytes_with_the_entries():
    entries = RasterPageEntries()
    entries.insert_or_replace_as_most_recent(_page(0, 100))
    entries.insert_or_replace_as_most_recent(_page(1, 300))
    assert entries.total_bytes() == 400

    entries.remove(0)
    assert entries.total_bytes() == 300

    entries.clear()
    assert entries.total_bytes() == 0
    assert len(entries) == 0


def test_removing_an_absent_page_is_not_an_error():
    entries = RasterPageEntries()
    entries.remove(9)
    assert len(entries) == 0


# ---------------------------------------------------------------------------
# Finding 7 -- RasterPageCache.put(), including the branch that does not
#              restore the invariant
# ---------------------------------------------------------------------------

def test_put_leaves_the_page_resident_and_most_recent():
    cache = RasterPageCache(RenderBudget(max_cached_bytes=10_000, prefetch_page_limit=1))

    cache.put(_page(0, 100), None)
    cache.put(_page(1, 100), None)

    assert cache.contains_at_height(1, RENDER_HEIGHT)
    assert cache._entries.least_recently_used_excluding().page_index == 0


def test_put_evicts_least_recently_used_until_the_budget_holds():
    cache = RasterPageCache(RenderBudget(max_cached_bytes=1_000, prefetch_page_limit=1))
    cache.put(_page(0, 400), None)
    cache.put(_page(1, 400), None)

    evicted = cache.put(_page(2, 400), None)

    assert evicted == [0]
    assert cache.total_bytes() == 800
    assert not cache.contains_at_height(0, RENDER_HEIGHT)


def test_put_evicts_as_many_as_the_budget_demands():
    cache = RasterPageCache(RenderBudget(max_cached_bytes=1_000, prefetch_page_limit=1))
    for index in range(4):
        cache.put(_page(index, 250), None)

    evicted = cache.put(_page(4, 750), None)

    assert evicted == [0, 1, 2]
    assert cache.total_bytes() == 1_000


def test_put_never_evicts_the_protected_page():
    """The displayed page. Evicting it would blank the canvas to satisfy a
    budget the eviction cannot satisfy anyway."""
    cache = RasterPageCache(RenderBudget(max_cached_bytes=1_000, prefetch_page_limit=1))
    cache.put(_page(0, 400), None)
    cache.put(_page(1, 400), None)

    evicted = cache.put(_page(2, 400), protected_page_index=0)

    assert evicted == [1]
    assert cache.contains_at_height(0, RENDER_HEIGHT)


def test_put_does_not_evict_the_page_it_just_inserted():
    cache = RasterPageCache(RenderBudget(max_cached_bytes=500, prefetch_page_limit=1))
    cache.put(_page(0, 400), None)

    cache.put(_page(1, 400), None)

    assert cache.contains_at_height(1, RENDER_HEIGHT)
    assert not cache.contains_at_height(0, RENDER_HEIGHT)


def test_put_returns_empty_when_nothing_needed_evicting():
    cache = RasterPageCache(RenderBudget(max_cached_bytes=10_000, prefetch_page_limit=1))
    assert cache.put(_page(0, 400), None) == []


def test_a_page_larger_than_the_budget_leaves_the_invariant_violated(caplog):
    """BUDGET-NOT-RESTORED. The post-conditions have to admit this or the
    contract claims something put() does not deliver -- audit finding 7."""
    cache = RasterPageCache(RenderBudget(max_cached_bytes=100, prefetch_page_limit=1))

    with caplog.at_level("WARNING"):
        evicted = cache.put(_page(0, 400), None)

    assert evicted == []
    assert cache.total_bytes() == 400  # above the 100 byte ceiling, on purpose
    assert cache.contains_at_height(0, RENDER_HEIGHT)


def test_the_over_budget_warning_names_both_byte_figures(caplog):
    """So the budget can be retuned from the log alone, without a repro."""
    cache = RasterPageCache(RenderBudget(max_cached_bytes=100, prefetch_page_limit=1))

    with caplog.at_level("WARNING"):
        cache.put(_page(0, 400), None)

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "400" in message and "100" in message


def test_the_eviction_loop_terminates_when_both_survivors_are_exempt():
    """Protected page plus just-inserted page, together over budget. Without
    the victim-is-None break this loop does not terminate."""
    cache = RasterPageCache(RenderBudget(max_cached_bytes=100, prefetch_page_limit=1))
    cache.put(_page(0, 400), None)

    evicted = cache.put(_page(1, 400), protected_page_index=0)

    assert evicted == []
    assert cache.total_bytes() == 800


def test_get_misses_when_the_page_is_cached_at_another_height():
    """A resize invalidates every entry; a hit at the wrong height would paint
    a stale-resolution page."""
    cache = RasterPageCache(RenderBudget(max_cached_bytes=10_000, prefetch_page_limit=1))
    cache.put(_page(0, 400, height=100), None)

    assert cache.get(0, 200) is None
    assert not cache.contains_at_height(0, 200)
    assert cache.get(0, 100) is not None


def test_get_promotes_the_page_it_returns():
    cache = RasterPageCache(RenderBudget(max_cached_bytes=10_000, prefetch_page_limit=1))
    cache.put(_page(0, 100), None)
    cache.put(_page(1, 100), None)

    cache.get(0, RENDER_HEIGHT)

    assert cache._entries.least_recently_used_excluding().page_index == 1


def test_clear_empties_the_cache():
    cache = RasterPageCache(RenderBudget(max_cached_bytes=10_000, prefetch_page_limit=1))
    cache.put(_page(0, 400), None)

    cache.clear()

    assert cache.total_bytes() == 0
    assert cache.resident_page_count() == 0


# ---------------------------------------------------------------------------
# Finding 5 -- pages_ordered_by_distance_from
# ---------------------------------------------------------------------------

def test_two_page_document_offers_exactly_the_other_page():
    """This is what makes the Optimize04 section 3.2 equivalence claim checkable
    rather than assumed: with prefetch_page_limit = 1 the single candidate for a
    2-page primary PDF must be the adjacent page."""
    assert pages_ordered_by_distance_from(0, 2) == [1]
    assert pages_ordered_by_distance_from(1, 2) == [0]


def test_candidates_are_ordered_by_ascending_distance():
    assert pages_ordered_by_distance_from(0, 5) == [1, 2, 3, 4]


def test_ties_break_toward_the_lower_index():
    """Round-robin or reverse-distance ordering would give a different first
    candidate, and the whole prefetch policy turns on the first candidate."""
    assert pages_ordered_by_distance_from(2, 5) == [1, 3, 0, 4]


def test_the_current_page_is_never_a_candidate():
    for current in range(6):
        assert current not in pages_ordered_by_distance_from(current, 6)


def test_a_single_page_document_offers_nothing():
    assert pages_ordered_by_distance_from(0, 1) == []


# ---------------------------------------------------------------------------
# Finding 18 -- byte_size measured on the source QImage
# ---------------------------------------------------------------------------

def test_bytes_are_read_from_the_image_the_worker_already_allocated():
    image = QImage(40, 30, QImage.Format.Format_ARGB32)

    measured = measure_rendered_bytes(RenderedImage(page_index=0, image=image))

    assert measured == image.sizeInBytes()
    assert measured > 0


class _ImageReportingNothing:
    """A platform whose sizeInBytes() is unavailable. The fallback exists so a
    zero never reaches the budget arithmetic as a free page."""

    def sizeInBytes(self) -> int:
        return 0

    def width(self) -> int:
        return 40

    def height(self) -> int:
        return 30


def test_a_zero_report_falls_back_to_the_four_byte_formula():
    measured = measure_rendered_bytes(
        RenderedImage(page_index=0, image=_ImageReportingNothing())
    )

    assert measured == 40 * 30 * 4


class _ImageThatRefusesToBeCopied:
    """toImage() on a QPixmap deep-copies it, transiently doubling the exact
    allocation being measured -- up to +168 MB per insertion at unscaled 4K.
    That was the audit's proposed remedy and the reason it was rejected."""

    def sizeInBytes(self) -> int:
        return 4_800

    def toImage(self):  # pragma: no cover - failing here is the assertion
        raise AssertionError("byte_size must not copy the buffer it measures")

    def copy(self, *args):  # pragma: no cover - as above
        raise AssertionError("byte_size must not copy the buffer it measures")


def test_measuring_never_copies_the_buffer():
    measured = measure_rendered_bytes(
        RenderedImage(page_index=0, image=_ImageThatRefusesToBeCopied())
    )

    assert measured == 4_800


# ---------------------------------------------------------------------------
# Findings 4 and 8 -- estimate_page_bytes and select_prefetch_pages
# ---------------------------------------------------------------------------

@pytest.fixture
def canvas(qtbot, theme):
    widget = LayoutCanvas(Mock(spec=LayoutQueryService), Mock(spec=PdfRenderer), theme=theme)
    qtbot.addWidget(widget)
    # Pinned so the arithmetic under test does not depend on the widget's
    # unshown viewport height.
    widget.current_render_height = lambda: RENDER_HEIGHT
    return widget


def _context(canvas, *dimensions: tuple[float, float]) -> None:
    """More than two pages is legal only on the reference document --
    get_page_dimensions enforces 1 or 2 for the primary. That asymmetry is why
    F1's unbounded case lives on the secondary PDF (section 2.1)."""
    canvas._current_context = LayoutContext(
        audit_id=1,
        pdf_source_file_id=2,
        pdf_path=pathlib.Path("fake.pdf"),
        page_count=len(dimensions),
        page_dimensions=tuple(dimensions),
        is_reference=len(dimensions) > 2,
    )


def test_estimate_is_height_times_width_times_four(canvas):
    _context(canvas, (1000.0, 800.0))  # aspect 1.25

    assert canvas.estimate_page_bytes(100) == 100 * 125 * 4


def test_estimate_uses_the_widest_page_so_it_errs_high(canvas):
    """A high estimate declines a prefetch that would have fit -- a latency
    cost. A low one admits a page that does not fit -- a memory cost, which is
    what this plan exists to prevent."""
    _context(canvas, (800.0, 800.0), (2000.0, 800.0))  # aspects 1.0 and 2.5

    assert canvas.estimate_page_bytes(100) == 100 * 250 * 4


def test_estimate_reads_no_cache_state(canvas):
    _context(canvas, (1000.0, 800.0))
    before = canvas.estimate_page_bytes(100)
    canvas._raster_cache.put(_page(0, 999_999), None)

    assert canvas.estimate_page_bytes(100) == before


def test_estimate_is_zero_with_no_document_loaded(canvas):
    canvas._current_context = None

    assert canvas.estimate_page_bytes(100) == 0


def test_estimate_is_zero_when_the_document_reports_no_page_geometry(canvas):
    """The context _render_source builds for an audit with no drawing attached."""
    canvas._current_context = LayoutContext(
        audit_id=1,
        pdf_source_file_id=None,
        pdf_path=None,
        page_count=0,
        page_dimensions=(),
    )

    assert canvas.estimate_page_bytes(100) == 0


def _budget(max_bytes: int, limit: int) -> RenderBudget:
    return RenderBudget(max_cached_bytes=max_bytes, prefetch_page_limit=limit)


def test_a_zero_limit_prefetches_nothing(canvas):
    _context(canvas, (1000.0, 800.0), (1000.0, 800.0))

    assert canvas.select_prefetch_pages(0, 2, _budget(10_000_000, 0)) == ()


def test_the_two_page_primary_still_prefetches_its_other_page(canvas):
    """Optimize04 section 3.2 preserved: both pages cached, TOP/BOT instant."""
    _context(canvas, (1000.0, 800.0), (1000.0, 800.0))

    assert canvas.select_prefetch_pages(0, 2, _budget(10_000_000, 1)) == (1,)


def test_an_eight_page_reference_is_capped_at_the_limit(canvas):
    """The S1 defect was that `others` was every uncached page. Page count is
    unbounded by contract on the reference PDF."""
    _context(canvas, *([(1000.0, 800.0)] * 8))

    assert canvas.select_prefetch_pages(0, 8, _budget(10_000_000, 1)) == (1,)
    assert canvas.select_prefetch_pages(0, 8, _budget(10_000_000, 3)) == (1, 2, 3)


def test_pages_already_cached_at_this_height_are_skipped(canvas):
    _context(canvas, *([(1000.0, 800.0)] * 4))
    canvas._raster_cache.put(_page(1, 400), None)

    assert canvas.select_prefetch_pages(0, 4, _budget(10_000_000, 1)) == (2,)


def test_a_page_cached_at_another_height_is_still_a_candidate(canvas):
    _context(canvas, *([(1000.0, 800.0)] * 4))
    canvas._raster_cache.put(_page(1, 400, height=RENDER_HEIGHT * 2), None)

    assert canvas.select_prefetch_pages(0, 4, _budget(10_000_000, 1)) == (1,)


def test_selection_stops_at_the_budget_before_the_limit(canvas):
    """Two pages fit under the ceiling; the third does not, and the limit of
    four never comes into play."""
    _context(canvas, *([(1000.0, 800.0)] * 6))  # 50_000 bytes per page

    assert canvas.select_prefetch_pages(0, 6, _budget(120_000, 4)) == (1, 2)


def test_resident_bytes_count_against_the_projection(canvas):
    _context(canvas, *([(1000.0, 800.0)] * 6))
    canvas._raster_cache.put(_page(5, 100_000), None)

    assert canvas.select_prefetch_pages(0, 6, _budget(120_000, 4)) == ()


def test_prefetch_is_suppressed_not_clamped_when_the_cache_is_over_budget(canvas):
    """Audit finding 8, remedy rejected. In this regime the displayed page has
    already consumed the whole budget; admitting a second would leave put()
    unable to evict either (both exempt) and would double an already-over-budget
    cache. Clamping projected_bytes to min(total_bytes(), max_cached_bytes)
    would trade a bounded latency cost for an unbounded memory one."""
    _context(canvas, (1000.0, 800.0), (1000.0, 800.0))
    canvas._raster_cache.put(_page(0, 500_000), None)  # alone over the ceiling
    budget = _budget(100_000, 1)
    assert canvas._raster_cache.total_bytes() > budget.max_cached_bytes

    assert canvas.select_prefetch_pages(0, 2, budget) == ()


def test_prefetch_is_suppressed_when_a_page_cannot_be_costed(canvas):
    """A zero estimate would otherwise admit every candidate as free."""
    canvas._current_context = None

    assert canvas.select_prefetch_pages(0, 8, _budget(10_000_000, 4)) == ()


def test_selected_pages_never_include_the_displayed_one(canvas):
    _context(canvas, *([(1000.0, 800.0)] * 6))

    selected = canvas.select_prefetch_pages(3, 6, _budget(10_000_000, 5))

    assert 3 not in selected
    assert selected == (2, 4, 1, 5, 0)  # ascending distance, ties toward lower
