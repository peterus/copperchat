import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { computeKeyboardInset } from '@/hooks/useKeyboardInset';
import { useComposer } from '@/stores/composer';
import { PID, row } from '@/test/fakeMessageServer';
import { setMedia } from '@/test/media';
import { Composer } from './Composer';
import { composerAttachmentSlot } from './composerSlots';

function setup(props: Partial<Parameters<typeof Composer>[0]> = {}) {
  const onSend = vi.fn();
  const onStop = vi.fn();
  const onCancelQueued = vi.fn();
  render(<Composer projectId={PID} runActive={false} queued={[]} onSend={onSend} onStop={onStop} onCancelQueued={onCancelQueued} {...props} />);
  return { onSend, onStop, onCancelQueued, box: screen.getByRole('textbox', { name: 'Message' }) };
}

beforeEach(() => {
  useComposer.getState().clear();
  composerAttachmentSlot.register(null);
});

describe('Composer', () => {
  it('Enter sends on desktop and clears; Shift+Enter does not send', () => {
    const { onSend, box } = setup();
    fireEvent.change(box, { target: { value: 'hello' } });
    fireEvent.keyDown(box, { key: 'Enter', shiftKey: true });
    expect(onSend).not.toHaveBeenCalled();
    fireEvent.keyDown(box, { key: 'Enter' });
    expect(onSend).toHaveBeenCalledWith('hello', []);
    expect(useComposer.getState().text).toBe('');
  });

  it('Enter inserts a newline on touch devices (send via button)', () => {
    setMedia((q) => q === '(pointer: coarse)');
    const { onSend, box } = setup();
    fireEvent.change(box, { target: { value: 'hi' } });
    fireEvent.keyDown(box, { key: 'Enter' });
    expect(onSend).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(onSend).toHaveBeenCalledWith('hi', []);
  });

  it('does not send blank text or while disabled', () => {
    const { onSend, box } = setup();
    fireEvent.change(box, { target: { value: '   ' } });
    fireEvent.keyDown(box, { key: 'Enter' });
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled();
    expect(onSend).not.toHaveBeenCalled();
  });

  it('shows Stop only while a run is active and keeps Send available for queueing', () => {
    const { onStop } = setup({ runActive: true });
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
    expect(onStop).toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Send' })).toBeInTheDocument();
  });

  it('lists queued messages with cancel', () => {
    const { onCancelQueued } = setup({ queued: [row(42, { role: 'user', status: 'queued', content: 'next question' })] });
    expect(screen.getByRole('list', { name: 'Queued messages' })).toHaveTextContent('next question');
    fireEvent.click(screen.getByRole('button', { name: 'Cancel queued message: next question' }));
    expect(onCancelQueued).toHaveBeenCalledWith(42);
  });

  it('renders no attachment control until a slot is registered; the slot can set attachments', () => {
    const { onSend, box } = setup();
    expect(screen.queryByTestId('attach')).toBeNull();
    act(() =>
      composerAttachmentSlot.register(({ disabled, projectId, setAttachments }) => (
        <button type="button" data-testid="attach" disabled={disabled}
          onClick={() => setAttachments([{ id: 'u1', kind: 'image', name: `${projectId}.png` }])}>Attach</button>
      )),
    );
    fireEvent.click(screen.getByTestId('attach'));
    expect(screen.getByRole('list', { name: 'Attachments' })).toHaveTextContent(`${PID}.png`);
    fireEvent.change(box, { target: { value: 'see image' } });
    fireEvent.keyDown(box, { key: 'Enter' });
    expect(onSend).toHaveBeenCalledWith('see image', [{ id: 'u1', kind: 'image', name: `${PID}.png` }]);
    expect(useComposer.getState().attachments).toEqual([]);
  });

  it('insertText from outside (viewer mention) lands in the textarea', () => {
    const { box } = setup();
    act(() => useComposer.getState().insertText('@R12'));
    expect(box).toHaveValue('@R12 ');
    expect(document.activeElement).toBe(box);
  });

  it('keyboard inset = innerHeight − visual viewport bottom', () => {
    expect(computeKeyboardInset(800, { height: 450, offsetTop: 0 })).toBe(350);
    expect(computeKeyboardInset(800, { height: 800, offsetTop: 0 })).toBe(0);
    expect(computeKeyboardInset(800, null)).toBe(0);
  });
});
