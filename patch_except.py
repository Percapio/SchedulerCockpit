import os
import re

files = [
    "cockpit/ui/widgets/dashboard.py",
    "cockpit/ui/widgets/audit_bom_panel.py",
    "cockpit/ui/widgets/checklist_row.py",
    "cockpit/ui/widgets/qt_lifecycle.py",
    "cockpit/ui/widgets/add_drawing_dialog.py",
    "cockpit/ui/widgets/split_dialog.py",
    "cockpit/ui/canvas/layout_canvas.py",
    "cockpit/ui/main_window.py",
    "cockpit/ui/theme.py",
    "cockpit/ui/config.py",
    "cockpit/ui/data_migration.py",
    "cockpit/ui/font_scale_controller.py",
    "cockpit/ui/workers/ingestion_worker.py",
    "cockpit/services/audit_metadata.py",
    "cockpit/services/completion.py",
    "cockpit/services/split.py",
    "cockpit/services/storage_reaper.py",
    "cockpit/services/startup_reconciler.py"
]

def process_file(fpath):
    if not os.path.exists(fpath):
        return

    with open(fpath, "r", encoding="utf-8") as f:
        lines = f.readlines()
        
    has_logging_import = any(re.match(r"^(?:import logging|from logging )", line) for line in lines)
    has_logger_def = any("logger = logging.getLogger" in line for line in lines)
    
    if not has_logging_import or not has_logger_def:
        insert_idx = 0
        for i, line in enumerate(lines):
            if line.startswith("import ") or line.startswith("from "):
                insert_idx = i + 1
        if insert_idx == 0:
            insert_idx = 1 if lines and lines[0].startswith('"""') else 0
            
        imports = []
        if not has_logging_import:
            imports.append("import logging\n")
        if not has_logger_def:
            imports.append("logger = logging.getLogger(__name__)\n")
            
        lines = lines[:insert_idx] + imports + ["\n"] + lines[insert_idx:]

    new_lines = []
    i = 0
    while i < len(lines):
        line = lines[i]
        new_lines.append(line)
        m = re.match(r"^(\s*)except\s*(.*?):", line)
        if m:
            indent = m.group(1) + "    "
            exc_part = m.group(2).strip()
            # check if next line is a logger call
            next_is_logger = False
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            if j < len(lines) and lines[j].strip().startswith("logger."):
                next_is_logger = True
                
            if not next_is_logger:
                context_name = os.path.basename(fpath).split(".")[0]
                new_lines.append(indent + f"logger.exception('Exception caught in {context_name}')\n")
        i += 1
        
    with open(fpath, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
        
    print(f"Processed {fpath}")

for f in files:
    process_file(f)
