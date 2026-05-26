import sys
import gc
import psutil
import tracemalloc
import argparse
import time
import os
import pathlib

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer

# Make sure we can import cockpit
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent.absolute()))

from cockpit.ui.config import resolve_config
from cockpit.ui.bootstrap import bootstrap
from cockpit.ui.main_window import MainWindow
from cockpit.ui.theme import ThemeLoader
from cockpit.ui.widgets.checklist_row import ChecklistRow
from cockpit.ui.widgets.audit_bom_panel import AuditBomRow, RefDesChip
from cockpit.ui.canvas.layout_canvas import LayoutCanvas

def parse_args():
    parser = argparse.ArgumentParser(description="Optimize01 Churn Smoke Fixture")
    parser.add_argument("--cycles", type=int, default=100)
    parser.add_argument("--render-multiplier", type=float, default=3.0)
    parser.add_argument("--audits", type=int, default=3)
    parser.add_argument("--page-switches-per-cycle", type=int, default=4)
    parser.add_argument("--selections-per-cycle", type=int, default=20)
    return parser.parse_args()

def run_smoke():
    args = parse_args()
    
    app = QApplication(sys.argv)
    
    # Bootstrap
    config = resolve_config()
    bootstrapped = bootstrap(config)
    
    ui_dir = pathlib.Path(__file__).parent.parent.parent / "cockpit" / "ui"
    theme = ThemeLoader.load(ui_dir / "theme.json", ui_dir / "theme.schema.json")
    app.setStyleSheet(theme.qss())
    
    main_window = MainWindow(
        theme=theme,
        app=app, 
        bootstrapped_app=bootstrapped,
        audit_read_svc=bootstrapped.audit_read_svc,
        checklist_svc=bootstrapped.checklist_svc,
        split_svc=bootstrapped.split_svc,
        completion_svc=bootstrapped.completion_svc,
        audit_metadata_svc=bootstrapped.audit_metadata_svc,
        layout_query_svc=bootstrapped.layout_query_svc,
        pdf_renderer=bootstrapped.pdf_renderer
    )
    main_window.showMaximized()
    
    # Process initial events to let UI settle
    app.processEvents()
    time.sleep(1)
    app.processEvents()
    
    print("UI Settled. Starting churn...")
    
    process = psutil.Process(os.getpid())
    
    baseline_rss = process.memory_info().rss
    print(f"Baseline RSS: {baseline_rss / 1024 / 1024:.2f} MB")
    
    open_audits = bootstrapped.audit_read_svc.list_open()
    if not open_audits:
        print("No open audits available. Please create one before running smoke.")
        return
    
    audit_ids = [a.audit_id for a in open_audits][:args.audits]
    print(f"Churning on audits: {audit_ids}")
    
    from PyQt6.QtGui import QKeyEvent, QWheelEvent, QMouseEvent
    from PyQt6.QtCore import QPointF, QPoint, Qt
    
    for cycle in range(1, args.cycles + 1):
        audit_id = audit_ids[(cycle - 1) % len(audit_ids)]
        
        main_window._audit_view.load(audit_id)
        main_window.stacked.setCurrentWidget(main_window._audit_view)
        app.processEvents()
        
        # P page switches
        for p in range(args.page_switches_per_cycle):
            switcher = main_window._audit_view._layout_canvas._page_switcher
            if switcher.buttons:
                next_idx = 1 if switcher._current_index == 0 else 0
                if next_idx < len(switcher.buttons):
                    switcher.buttons[next_idx].click()
            app.processEvents()
            
        # S selections
        for s in range(args.selections_per_cycle):
            # Click THT row body
            if main_window._audit_view._dashboard.checklist_tht._layout.count() > 0:
                row = main_window._audit_view._dashboard.checklist_tht._layout.itemAt(0).widget()
                if isinstance(row, ChecklistRow):
                    row._body.mouseReleaseEvent(QMouseEvent(QMouseEvent.Type.MouseButtonRelease, QPointF(0, 0), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
                    app.processEvents()
                    
            # Click BOM chip
            if len(main_window._audit_view._bom_panel._row_index) > 0:
                bom_row = next(iter(main_window._audit_view._bom_panel._row_index.values()))
                if bom_row.chips:
                    chip = next(iter(bom_row.chips.values()))
                    chip.mouseReleaseEvent(QMouseEvent(QMouseEvent.Type.MouseButtonRelease, QPointF(0, 0), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
                    app.processEvents()
                    
            # press Esc
            esc_event = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
            main_window._audit_view.keyPressEvent(esc_event)
            app.processEvents()
            
        # Zoom in 5 notches
        canvas = main_window._audit_view._layout_canvas
        for _ in range(5):
            wheel = QWheelEvent(QPointF(100, 100), QPointF(100, 100), QPoint(0, 120), QPoint(0, 120), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.ScrollBegin, False)
            canvas.wheelEvent(wheel)
        app.processEvents()
        
        # Zoom out 5 notches
        for _ in range(5):
            wheel = QWheelEvent(QPointF(100, 100), QPointF(100, 100), QPoint(0, -120), QPoint(0, -120), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.ScrollBegin, False)
            canvas.wheelEvent(wheel)
        app.processEvents()
        
        # Double click (reset zoom)
        dc_event = QMouseEvent(QMouseEvent.Type.MouseButtonDblClick, QPointF(100, 100), QPointF(100, 100), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        canvas.mouseDoubleClickEvent(dc_event)
        app.processEvents()
        
        # Close audit (return to dashboard / picker)
        main_window._audit_view.exit_requested.emit()
        app.processEvents()
        
        from PyQt6.QtCore import QCoreApplication, QEvent
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        
        gc.collect()
        rss = process.memory_info().rss
        delta = rss - baseline_rss
        
        objects = gc.get_objects()
        cl_rows = sum(1 for o in objects if isinstance(o, ChecklistRow))
        bom_rows = sum(1 for o in objects if isinstance(o, AuditBomRow))
        chips = sum(1 for o in objects if isinstance(o, RefDesChip))
        canvases = sum(1 for o in objects if isinstance(o, LayoutCanvas))
        garbage = len(gc.garbage)
        
        print(f"Cycle {cycle:3}: Delta RSS {delta / 1024 / 1024:6.2f} MB | Garbage {garbage} | "
              f"cl_rows {cl_rows} | bom_rows {bom_rows} | chips {chips} | canvases {canvases}")
              
        if cycle == 2:
            import objgraph
            for o in objects:
                if isinstance(o, ChecklistRow):
                    referrers = gc.get_referrers(o)
                    is_live = False
                    for ref in referrers:
                        if isinstance(ref, dict):
                            for k, v in ref.items():
                                if v is o and type(k).__name__ == 'ChecklistRowKey':
                                    is_live = True
                    if is_live:
                        continue
                    
                    print(f"Tracing LEAKED ChecklistRow {id(o)}...")
                    print(f"  Refcount: {sys.getrefcount(o)}")
                    try:
                        _ = o.objectName()
                        print("  C++ object is ALIVE")
                    except RuntimeError:
                        print("  C++ object is DELETED (but Python wrapper leaked)")
                        
                    print(f"  Referrers count: {len(referrers)}")
                    for idx, ref in enumerate(referrers):
                        print(f"    {idx}: {type(ref)}")
                        if isinstance(ref, list):
                            print(f"      List length: {len(ref)}")
                            # print first few types
                            types = [type(x).__name__ for x in ref[:5]]
                            print(f"      List starts with: {types}")
                        elif isinstance(ref, dict):
                            for k, v in ref.items():
                                if v is o:
                                    print(f"      Key in dict: {k}")
                    break
                    
        del objects
        del cl_rows
        del bom_rows
        del chips
        del canvases
        
    app.quit()

if __name__ == "__main__":
    run_smoke()
