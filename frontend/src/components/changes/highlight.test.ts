import { describe, expect, it } from 'vitest';
import type { ChangeEntry } from '@/api/changes';
import { BOARD_KINDS, entriesHighlight, isSchematicEntry } from './highlight';

const ROOT = '/5b9623a5-6d01-41fc-9865-e1bc779418c8';
const entry = (id: number, over: Partial<ChangeEntry> = {}): ChangeEntry => ({
  id, runId: 'r', planStepId: null, callSeq: id, tool: 'sch_place_symbol', op: 'add', kind: 'symbol',
  uuid: `uuid-${id}`, sheetPath: ROOT, ref: `R${id}`, net: null, before: null, after: null, sideEffect: false,
  createdAt: '2026-11-18T10:00:00Z', ...over,
});
const footprint = (id: number, kind = 'footprint_created', ref = 'Nick:L_Test'): ChangeEntry =>
  entry(id, { tool: 'fp_create', kind, uuid: ref, ref, sheetPath: '', after: { geometry_hash: 'h' } });

describe('footprint library rows', () => {
  it('are no schematic entries', () => {
    expect(isSchematicEntry(entry(1))).toBe(true);
    expect(isSchematicEntry(footprint(2))).toBe(false);
    expect(isSchematicEntry(footprint(3, 'fp_lib_table_row_added', 'Nick'))).toBe(false);
    expect(isSchematicEntry(entry(4, { sheetPath: '' }))).toBe(false);
  });

  it('are skipped by entriesHighlight; the sheet comes from the first schematic entry', () => {
    const h = entriesHighlight([footprint(1, 'fp_lib_table_row_added', 'Nick'), footprint(2), entry(3)]);
    expect(h).toEqual({ refs: [{ ref: 'R3', kind: 'added', sheetPath: ROOT }], boxes: [], sheetPath: ROOT });
  });

  it('only footprint rows highlight nothing and keep the page', () => {
    expect(entriesHighlight([footprint(1)])).toEqual({ refs: [], boxes: [], sheetPath: null });
  });
});

describe('board rows', () => {
  it('are no schematic entries, even with a sheet path', () => {
    expect(isSchematicEntry(entry(1, { tool: 'pcb_move', kind: 'board_move', sheetPath: '/' }))).toBe(false);
    expect(isSchematicEntry(entry(2, { tool: 'pcb_move', kind: 'board_move', sheetPath: '' }))).toBe(false);
  });

  it('BOARD_KINDS equals the backend list (cc/changes/repo.py)', () => {
    expect([...BOARD_KINDS].sort()).toEqual(
      [
        'board_sync_from_schematic', 'board_set_outline', 'board_set_rules', 'board_define_blocks',
        'board_place_block', 'board_move', 'board_legalize', 'board_place_blocks', 'board_route',
        'board_route_fill', 'board_unroute', 'board_zones', 'board_fanout', 'board_stitch', 'board_tidy_silk', 'board_approve',
      ].sort(),
    );
  });
});
