import { readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { ALL_FRAME_TYPES, AnyFrame, ClientFrame, PlanDTO, ServerFrame } from './frames';

// String form on purpose: jsdom's global URL class is rejected by Node's fileURLToPath.
const dir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../protocol-fixtures');
const files = readdirSync(dir).filter((f) => f.endsWith('.json')).sort();

/** protocol-fixtures/<type>.json, shared with the backend's pydantic tests. */
interface Fixture {
  type: string;
  directions: Array<'c2s' | 's2c'>;
  valid: unknown[];
  invalid: unknown[];
}

const schemaFor = { c2s: ClientFrame, s2c: ServerFrame } as const;

function load(file: string): Fixture {
  return JSON.parse(readFileSync(path.join(dir, file), 'utf8')) as Fixture;
}

describe('protocol fixtures (shared with backend)', () => {
  it('has a fixture for every C7 frame type and no fixture for an unknown type', () => {
    const stems = new Set(files.map((f) => path.basename(f, '.json')));
    for (const type of ALL_FRAME_TYPES) expect(stems, `missing protocol-fixtures/${type}.json`).toContain(type);
    for (const stem of stems) expect(ALL_FRAME_TYPES as readonly string[]).toContain(stem);
  });

  it.each(files)('%s: valid frames parse and invalid frames fail in every direction', (file) => {
    const fixture = load(file);
    const stem = path.basename(file, '.json');
    expect(fixture.type).toBe(stem);
    expect(fixture.directions.length).toBeGreaterThan(0);
    expect(fixture.valid.length).toBeGreaterThan(0);
    for (const direction of fixture.directions) {
      const schema = schemaFor[direction];
      expect(schema, `unknown direction ${direction}`).toBeDefined();
      for (const frame of fixture.valid) {
        const result = schema.safeParse(frame);
        expect(result.success, `${direction} ${JSON.stringify(frame)}: ${JSON.stringify(result.error?.issues)}`).toBe(true);
        expect((frame as { type: string }).type).toBe(stem);
      }
      for (const frame of fixture.invalid) {
        expect(schema.safeParse(frame).success, `${direction} accepted invalid ${JSON.stringify(frame)}`).toBe(false);
      }
    }
  });
});

describe('frame schemas', () => {
  it('rejects a server frame without projectId', () => {
    expect(ServerFrame.safeParse({ type: 'run-started', runId: 'r1' }).success).toBe(false);
  });
  it('rejects unknown frame types', () => {
    expect(AnyFrame.safeParse({ type: 'nope', projectId: 'a1b2c3d4e5f6' }).success).toBe(false);
  });
  it('ping/pong carry projectId null', () => {
    expect(ClientFrame.safeParse({ type: 'ping', projectId: null }).success).toBe(true);
    expect(ServerFrame.safeParse({ type: 'ping', projectId: null }).success).toBe(true);
    expect(ServerFrame.safeParse({ type: 'pong', projectId: null }).success).toBe(true);
    expect(ServerFrame.safeParse({ type: 'pong' }).success).toBe(false);
  });
  it('project ids are 12 lowercase hex characters like the backend', () => {
    expect(ClientFrame.safeParse({ type: 'subscribe', projectId: 'a1b2c3d4e5f6', conversationId: 'c'.repeat(32), sinceMessageId: 0 }).success).toBe(true);
    expect(ClientFrame.safeParse({ type: 'subscribe', projectId: 'p1', conversationId: 'c'.repeat(32), sinceMessageId: 0 }).success).toBe(false);
  });
});

it('knows the visibility frame', () => {
  expect(ALL_FRAME_TYPES).toContain('visibility');
  expect(ClientFrame.safeParse({ type: 'visibility', projectId: 'a1b2c3d4e5f6', visible: true }).success).toBe(true);
  expect(ClientFrame.safeParse({ type: 'visibility', projectId: 'a1b2c3d4e5f6', visible: 1 }).success).toBe(false);
});

it('plan DTO: revise fields and step usage default like the backend, paused step parses', () => {
  const step = {
    idx: 1, title: 'S', intent: '', expectedChanges: 2, touches: { refs: [], nets: [] }, verification: '',
    status: 'paused', changeCount: 0, checkpointId: null, summary: null, failure: null, erc: null,
  };
  const plan = {
    id: 'f'.repeat(32), projectId: 'a1b2c3d4e5f6', conversationId: 'c'.repeat(32), title: 'T', summary: '',
    status: 'proposed', revision: 2, pauseReason: null, steps: [step], createdAt: 'x', updatedAt: 'x',
  };
  const parsed = PlanDTO.parse(plan);
  expect(parsed).toMatchObject({ reviseReason: null, previousRevision: null, steps: [{ status: 'paused', used: 0 }] });
  expect(PlanDTO.parse({ ...plan, reviseReason: 'r', previousRevision: 1, steps: [{ ...step, used: 3 }] })).toMatchObject({
    reviseReason: 'r', previousRevision: 1, steps: [{ used: 3 }],
  });
  expect(PlanDTO.safeParse({ ...plan, previousRevision: 0 }).success).toBe(false);
  expect(PlanDTO.safeParse({ ...plan, steps: [{ ...step, used: -1 }] }).success).toBe(false);
});
