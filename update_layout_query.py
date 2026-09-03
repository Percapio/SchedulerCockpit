import re

def update():
    with open('cockpit/services/layout_query.py', 'r', encoding='utf-8') as f:
        content = f.read()

    if 'from cockpit.utils.sorting import natural_sort_key' not in content:
        content = 'from cockpit.utils.sorting import natural_sort_key\n' + content

    old_locate = '''    def locate_refdes(self, audit_id: int, ref_des: str) -> RefDesLocation | None:
        bom_sf = self.source_file_repo.find_by_audit_and_category(audit_id, SourceFileCategory.BOM)
        if bom_sf is None:
            return None
        bom_components = self.bom_component_repo.list_for_source_file(bom_sf.id)
        matches = [c for c in bom_components if c.ref_des == ref_des]
        if not matches:
            return None
        if len(matches) > 1:
            import logging
            logging.getLogger(__name__).warning("ref_des %s maps to %d components; routing to first", ref_des, len(matches))
        return RefDesLocation(mpn=matches[0].component_mpn, mount_type=matches[0].mount_type)'''

    new_locate = '''    def locate_refdes(self, audit_id: int, ref_des: str) -> RefDesLocation | None:
        """
        Resolves a Ref_Des to the BOM line that owns it. Where a split job lists the
        designator on more than one line, the lowest Find# wins.

        INVARIANT -- shared with SelectionCoordinator.on_renderer_refdes_clicked:
          that caller re-resolves the THT branch itself, by taking the first row of
          ActiveAuditView.tht_rows carrying the designator. It agrees with this
          function ONLY because tht_rows is ordered by natural_sort_key(find_number)
          (checklist.py sort_key, see 4.2) and this function selects by the same key.
          Change either ordering and the two desynchronise silently: mount_type is
          read off one BOM line while the row that gets selected and scrolled to
          belongs to another. Neither path raises when they disagree.
        """
        bom_sf = self.source_file_repo.find_by_audit_and_category(audit_id, SourceFileCategory.BOM)
        if bom_sf is None:
            return None
        bom_components = self.bom_component_repo.list_for_source_file(bom_sf.id)
        matches = [c for c in bom_components if c.ref_des == ref_des]
        if not matches:
            return None
        if len(matches) > 1:
            import logging
            logging.getLogger(__name__).debug("ref_des %s shared across %d split lines; routing to lowest Find#", ref_des, len(matches))
        
        owning_line = min(matches, key=lambda c: natural_sort_key(c.find_number))
        return RefDesLocation(mpn=owning_line.component_mpn, mount_type=owning_line.mount_type)'''
    
    content = content.replace(old_locate, new_locate)

    content = content.replace(
        'sorted(grouped.items(), key=lambda item: item[1]["find_number"])',
        'sorted(grouped.items(), key=lambda item: natural_sort_key(item[1]["find_number"]))'
    )
    
    content = content.replace(
        'tuple(sorted(data["ref_des_list"]))',
        'tuple(sorted(data["ref_des_list"], key=natural_sort_key))'
    )

    with open('cockpit/services/layout_query.py', 'w', encoding='utf-8') as f:
        f.write(content)

if __name__ == '__main__':
    update()
