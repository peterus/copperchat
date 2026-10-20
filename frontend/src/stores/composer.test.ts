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

describe('drafts per conversation', () => {
  beforeEach(() => useComposer.getState().reset());
  it('activate() keeps each conversation its own draft', () => {
    const s = () => useComposer.getState();
    s().activate('p/a');
    s().setText('draft A');
    s().addAttachment({ id: 'a1', kind: 'pdf', name: 'ds.pdf' });
    s().activate('p/b');
    expect(s().text).toBe('');
    expect(s().attachments).toEqual([]);
    s().setText('draft B');
    s().activate('p/a');
    expect(s().text).toBe('draft A');
    expect(s().attachments).toEqual([{ id: 'a1', kind: 'pdf', name: 'ds.pdf' }]);
    s().activate('p/b');
    expect(s().text).toBe('draft B');
  });
  it('activating the same conversation keeps the draft; a sent (cleared) draft stays empty', () => {
    const s = () => useComposer.getState();
    s().activate('p/a');
    s().setText('x');
    s().activate('p/a');
    expect(s().text).toBe('x');
    s().clear();
    s().activate('p/b');
    s().activate('p/a');
    expect(s().text).toBe('');
  });
  it('reset() forgets every draft (logout)', () => {
    const s = () => useComposer.getState();
    s().activate('p/a');
    s().setText('secret');
    s().activate('p/b');
    s().reset();
    s().activate('p/a');
    expect(s().text).toBe('');
  });
});

describe('writes bound to a draft', () => {
  beforeEach(() => useComposer.getState().reset());
  it('addAttachmentFor() adds to the draft it names (parked or active), reading it at write time', () => {
    const s = () => useComposer.getState();
    const a = { id: 'u0', kind: 'pdf' as const, name: 'kept.pdf' };
    const late = { id: 'u1', kind: 'image' as const, name: 'late.png' };
    s().activate('p/a');
    s().addAttachment(a);
    s().activate('p/b');
    s().setText('B');
    s().addAttachmentFor('p/a', late);
    s().addAttachmentFor('p/a', late);
    expect(s().attachments).toEqual([]);
    s().addAttachmentFor('p/b', { id: 'u2', kind: 'pdf', name: 'now.pdf' });
    expect(s().attachments).toEqual([{ id: 'u2', kind: 'pdf', name: 'now.pdf' }]);
    s().activate('p/a');
    expect(s().attachments).toEqual([a, late]);
    // a sent (cleared) draft gets only the late upload, never the refs it was sent with
    s().clear();
    s().activate('p/b');
    s().addAttachmentFor('p/a', { id: 'u3', kind: 'image', name: 'later.png' });
    s().activate('p/a');
    expect(s().attachments).toEqual([{ id: 'u3', kind: 'image', name: 'later.png' }]);
  });
  it('deactivate() parks the draft; text written without a conversation is dropped', () => {
    const s = () => useComposer.getState();
    s().activate('p/a');
    s().setText('kept');
    s().deactivate();
    expect(s().activeKey).toBeNull();
    s().insertText('@R1');
    s().activate('p/b');
    expect(s().text).toBe('');
    s().activate('p/a');
    expect(s().text).toBe('kept');
  });
});
