| Question | Result | Failed checks |
|---|---|---|
| q01_place | 14 pass / 1 fail | rotate_then_mirror_matrix_as_requested |
| q02_fields | 14 pass / 2 fail | multi_unit_full_definition_update_responds, multi_unit_fields_synced_by_kicad |
| q03_connect | 36 pass / 3 fail | netlist_job_reflects_unsaved_edits, netlist_job_reflects_edits_after_save, png_job_reflects_edits_after_save |
| q04_transform | 10 pass / 3 fail | move_symbol_moves_fields, move_symbol_drags_attached_wires, subsheet_update_via_unbound_schematic_applies |
| q05_delete | 12 pass / 4 fail | dangling_junction_kept_after_push, delete_wire_keeps_junction, delete_wire_keeps_neighbour_segments, collinear_pair_kept_separate_on_create |
| q06_commit | 24 pass / 2 fail | update_of_missing_item_raises, recreate_with_sibling_keeps_library_link |
| q07_sheets | 20 pass / 2 fail | relative_sheetfile_new_sheet_save, workaround_field_fix_inside_create_commit |
| q08_save_latency | 9 pass / 4 fail | new_symbol_on_shared_sheet_visible_in_other_instance, copy_without_save_finds_all_hierarchy_files, copy_without_save_has_subsheet_edits, shared_sheet_second_instance_unique_ref_after_save |
| q09_bbox | 4 pass / 1 fail | api_bbox_matches_plot_within_0_6mm |
| q10_duplicate | 9 pass / 2 fail | definition_keeps_all_body_style_pins, dup_symbols_create_items_keep_library_link |
| q11_files_load | 9 pass / 0 fail |  |
| q12_kicanvas (host) | 7 pass / 1 fail | kicanvas_loads_q10_duplicate_B |
