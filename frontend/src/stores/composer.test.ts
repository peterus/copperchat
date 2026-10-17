import { beforeEach, describe, expect, it } from 'vitest';
import { insertAt, useComposer } from './composer';

describe('insertAt', () => {
  it('appends with a separating space', () => {
    expect(insertAt('check', '@R12', null)).toEqual({ text: 'check @R12 ', caret: 11 });
  });
  it('inserts at the caret without doubling spaces', () => {
    expect(insertAt('is  ok', '@C3', { start: 3, end: 3 })).toEqual({ text: 'is @C3 ok', caret: 7 });
  });
  it('replaces a selection', () => {
    expect(insertAt('what is X?', '@U1', { start: 8, end: 9 })).toEqual({ text: 'what is @U1 ?', caret: 12 });
  });
  it('into an empty prompt', () => {
    expect(insertAt('', '@R1', null)).toEqual({ text: '@R1 ', caret: 4 });
  });
  it('clamps a stale selection beyond the text', () => {
    expect(insertAt('ab', '@R1', { start: 9, end: 9 })).toEqual({ text: 'ab @R1 ', caret: 7 });
  });
});

describe('useComposer', () => {
  beforeEach(() => useComposer.getState().clear());
  it('insertText updates text, caret and focus token', () => {
    useComposer.getState().setText('why is  hot');
    useComposer.getState().setSelection(7, 7);
    useComposer.getState().insertText('@Q2');
    const s = useComposer.getState();
    expect(s.text).toBe('why is @Q2 hot');
    expect(s.selection).toEqual({ start: 11, end: 11 });
    expect(s.focusToken).toBe(1);
  });
  it('manages attachments', () => {
    useComposer.getState().addAttachment({ id: 'a1', kind: 'pdf', name: 'ds.pdf' });
    useComposer.getState().addAttachment({ id: 'a1', kind: 'pdf', name: 'ds.pdf' });
    expect(useComposer.getState().attachments).toHaveLength(1);
    useComposer.getState().removeAttachment('a1');
    expect(useComposer.getState().attachments).toEqual([]);
  });
});
