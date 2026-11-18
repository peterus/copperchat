import { describe, expect, it } from 'vitest';
import type { ChangeEntry } from '@/api/changes';
import { entriesHighlight, isSchematicEntry } from './highlight';

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
