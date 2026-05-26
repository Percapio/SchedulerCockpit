import json
import pathlib
import sys
import pytest

from cockpit.ui.crash_reporter import install_crash_reporter, LocalFileCrashSink, CrashReport
from cockpit._build_info import BuildInfo

@pytest.fixture(autouse=True)
def restore_hooks():
    orig_hook = sys.excepthook
    import cockpit.ui.crash_reporter
    orig_chain = cockpit.ui.crash_reporter._installed_chain
    yield
    sys.excepthook = orig_hook
    cockpit.ui.crash_reporter._installed_chain = orig_chain

class MockSink:
    def __init__(self):
        self.reports = []
        
    def emit(self, report):
        self.reports.append(report)

def test_install_crash_reporter_replaces_sinks_preserves_chain():
    # Install an original dummy hook to simulate the default sys.excepthook
    calls = []
    def original_hook(t, v, tb):
        calls.append("original")
    sys.excepthook = original_hook
    
    build_info = BuildInfo("test", "test", "test")
    
    sink1 = MockSink()
    install_crash_reporter(pathlib.Path("."), build_info, (sink1,))
    
    # Fire an exception
    try:
        raise ValueError("test exception 1")
    except ValueError as e:
        sys.excepthook(type(e), e, e.__traceback__)
        
    assert len(sink1.reports) == 1
    assert calls == ["original"]
    
    # Second install, should replace sink1 with sink2
    sink2 = MockSink()
    install_crash_reporter(pathlib.Path("."), build_info, (sink2,))
    
    calls.clear()
    
    try:
        raise ValueError("test exception 2")
    except ValueError as e:
        sys.excepthook(type(e), e, e.__traceback__)
        
    assert len(sink1.reports) == 1 # unchanged
    assert len(sink2.reports) == 1 # got the new one
    assert calls == ["original"] # still chained exactly once

def test_local_file_sink(tmp_path):
    sink = LocalFileCrashSink(tmp_path)
    
    report = CrashReport(
        schema_version=1,
        occurred_at_utc="2026-05-25T12:00:00Z",
        app_version="1.0.0",
        build_commit="abc",
        runtime_kind="source",
        python_version="3.12",
        qt_version="6.0",
        exception_class="MyError",
        exception_message="msg",
        traceback_lines=("tb1", "tb2"),
        thread_name="MainThread",
        reason_code="MY_ERR"
    )
    
    sink.emit(report)
    
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["exception_class"] == "MyError"
    assert data["reason_code"] == "MY_ERR"
