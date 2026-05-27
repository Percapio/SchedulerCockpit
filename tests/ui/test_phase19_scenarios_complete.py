import pytest
from _pytest.nodes import Item

def test_phase19_all_scenarios_represented(request):
    """
    Intent: Every row of Patch 01 section 8 has at least one test that declares the
            matching phase19_scenario marker. Fails the build if any row 1-13
            is unrepresented.
    """
    # This is a bit tricky, since we can't easily collect all tests from within a test.
    # We rely on the session items.
    session = request.session
    collected_markers = []
    for item in session.items:
        marker = item.get_closest_marker("phase19_scenario")
        if marker:
            collected_markers.append(marker)
            
    covered_rows = {m.args[0] for m in collected_markers}
    expected_rows = set(range(1, 14))
    
    missing = expected_rows - covered_rows
    assert not missing, f"Missing scenarios: {missing}"
