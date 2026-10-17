import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const http = vi.hoisted(() => ({ apiGet: vi.fn(), apiPost: vi.fn(), apiPut: vi.fn() }));
vi.mock('@/api/http', async (importOriginal) => ({ ...(await importOriginal<object>()), ...http }));

import { ApiError } from '@/api/http';
import type { SkillDetail, SkillSummary } from '@/api/skills';
import { SkillsPanel } from './SkillsPanel';

const userSkill: SkillSummary = {
  name: 'copperchat-tools',
  origin: 'user',
  description: 'Tools',
  shadowed: ['builtin'],
  hasBuiltin: true,
  originUpdated: true,
  enabled: true,
  agent: null,
};
const builtinSkill: SkillSummary = {
  name: 'datasheet-reading',
  origin: 'builtin',
  description: 'Read datasheets',
  shadowed: [],
  hasBuiltin: true,
  originUpdated: false,
  enabled: true,
  agent: 'part-researcher',
};
const projectSkill: SkillSummary = { ...builtinSkill, name: 'house-rules', origin: 'project', hasBuiltin: false, agent: null };
const ownSkill: SkillSummary = { ...userSkill, name: 'mine', hasBuiltin: false, shadowed: [], originUpdated: false };
const all = [userSkill, builtinSkill, projectSkill, ownSkill];

function detail(s: SkillSummary): SkillDetail {
  return {
    ...s,
    content: `---\nname: ${s.name}\n---\nbody`,
    originDiff: s.originUpdated ? '+New rule' : null,
    editable: s.origin === 'user',
  };
}

beforeEach(() => {
  vi.resetAllMocks();
  http.apiGet.mockImplementation(async (path: string) => {
    if (path.startsWith('/skills?')) return all;
    const name = decodeURIComponent(path.slice('/skills/'.length).split('?')[0] ?? '');
    return detail(all.find((s) => s.name === name)!);
  });
  http.apiPut.mockResolvedValue(undefined);
  http.apiPost.mockResolvedValue(undefined);
});

describe('SkillsPanel', () => {
  it('lists skills with origin badge, shadowed info, update marker and researcher tag', async () => {
    render(<SkillsPanel projectId="p1" />);
    const list = await screen.findByRole('list', { name: 'Skills' });
    expect(await within(list).findByText('copperchat-tools')).toBeInTheDocument();
    expect(http.apiGet).toHaveBeenCalledWith('/skills?projectId=p1');
    const row = within(list).getByText('copperchat-tools').closest('button')!;
    expect(within(row).getByText('user')).toBeInTheDocument();
    expect(within(row).getByText('overrides builtin')).toBeInTheDocument();
    expect(within(row).getByText('original updated')).toBeInTheDocument();
    const researcher = within(list).getByText('datasheet-reading').closest('button')!;
    expect(within(researcher).getByText('researcher')).toBeInTheDocument();
    expect(within(row).queryByText('researcher')).toBeNull();
  });

  it('shows the update notice with diff, marks it reviewed and toggles enable', async () => {
    render(<SkillsPanel projectId="p1" />);
    await userEvent.click(await screen.findByText('copperchat-tools'));
    expect(await screen.findByText(/original updated since/i)).toBeInTheDocument();
    expect(screen.getByText(/\+New rule/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /mark as reviewed/i }));
    await waitFor(() => expect(http.apiPost).toHaveBeenCalledWith('/skills/copperchat-tools/acknowledge-update', {}));
    await userEvent.click(screen.getByRole('checkbox', { name: /enabled in this project/i }));
    await waitFor(() =>
      expect(http.apiPut).toHaveBeenCalledWith('/projects/p1/skills/copperchat-tools', { enabled: false }),
    );
  });

  it('saves an edited user skill and resets it to the original', async () => {
    render(<SkillsPanel projectId="p1" />);
    await userEvent.click(await screen.findByText('copperchat-tools'));
    const editor = await screen.findByRole('textbox', { name: /skill content/i });
    expect(editor).not.toHaveAttribute('readonly');
    await userEvent.type(editor, '!');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(http.apiPut).toHaveBeenCalledWith('/skills/user/copperchat-tools', {
        content: '---\nname: copperchat-tools\n---\nbody!',
      }),
    );
    await userEvent.click(screen.getByRole('button', { name: /reset to original/i }));
    await waitFor(() => expect(http.apiPost).toHaveBeenCalledWith('/skills/copperchat-tools/reset', {}));
  });

  it('keeps unsaved edits when toggling enable', async () => {
    render(<SkillsPanel projectId="p1" />);
    await userEvent.click(await screen.findByText('copperchat-tools'));
    const editor = await screen.findByRole('textbox', { name: /skill content/i });
    await userEvent.type(editor, ' draft');
    await userEvent.click(screen.getByRole('checkbox', { name: /enabled in this project/i }));
    await waitFor(() => expect(http.apiPut).toHaveBeenCalledWith('/projects/p1/skills/copperchat-tools', { enabled: false }));
    await waitFor(() => expect(http.apiGet).toHaveBeenCalledTimes(4)); // list, detail, list, detail
    expect(screen.getByRole('textbox', { name: /skill content/i })).toHaveValue('---\nname: copperchat-tools\n---\nbody draft');
  });

  it('drops a detail response of the previous project', async () => {
    let release: (v: SkillDetail) => void = () => {};
    http.apiGet.mockImplementation(async (path: string) => {
      if (path.startsWith('/skills?')) return all;
      return new Promise<SkillDetail>((r) => (release = r));
    });
    const { rerender } = render(<SkillsPanel projectId="p1" />);
    await userEvent.click(await screen.findByText('copperchat-tools'));
    rerender(<SkillsPanel projectId="p2" />);
    release(detail(userSkill));
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByRole('textbox', { name: /skill content/i })).not.toBeInTheDocument();
  });

  it('builtin skills are read-only and can be customised', async () => {
    render(<SkillsPanel projectId="p1" />);
    await userEvent.click(await screen.findByText('datasheet-reading'));
    const editor = await screen.findByRole('textbox', { name: /skill content/i });
    expect(editor).toHaveAttribute('readonly');
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull();
    await userEvent.click(screen.getByRole('button', { name: /customise/i }));
    await waitFor(() => expect(http.apiPost).toHaveBeenCalledWith('/skills/datasheet-reading/customise', {}));
  });

  it('project skills explain where they live', async () => {
    render(<SkillsPanel projectId="p1" />);
    await userEvent.click(await screen.findByText('house-rules'));
    expect(await screen.findByText('.kicad-agent/skills/')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /customise|reset|delete/i })).toBeNull();
  });

  it('deletes a user-only skill after confirmation', async () => {
    render(<SkillsPanel projectId="p1" />);
    await userEvent.click(await screen.findByText('mine'));
    await userEvent.click(await screen.findByRole('button', { name: 'Delete' }));
    expect(http.apiPost).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: /confirm delete/i }));
    await waitFor(() => expect(http.apiPost).toHaveBeenCalledWith('/skills/mine/reset', {}));
    await waitFor(() => expect(screen.queryByRole('textbox', { name: /skill content/i })).toBeNull());
  });

  it('shows server errors as an alert', async () => {
    http.apiPost.mockRejectedValue(new ApiError(409, { detail: 'already customised' }, 'already customised'));
    render(<SkillsPanel projectId="p1" />);
    await userEvent.click(await screen.findByText('datasheet-reading'));
    await userEvent.click(await screen.findByRole('button', { name: /customise/i }));
    expect(await screen.findByRole('alert')).toHaveTextContent('already customised');
  });
});
