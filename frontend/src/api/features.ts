/** Server feature flags the UI needs: `GET /api/features` (board P1c). */
import { z } from 'zod';
import { apiGet } from './http';

export const Features = z.object({
  /** COPPERCHAT_BOARD_TOOLS: the Board session type can be created. */
  boardTools: z.boolean(),
});
export type Features = z.infer<typeof Features>;

export async function getFeatures(): Promise<Features> {
  return Features.parse(await apiGet('/features'));
}
