import { rmSync } from "node:fs";
import { RESULTS } from "./helpers";

/** One results.jsonl per run: drop rows from earlier runs before any test records. */
export default function globalSetup(): void {
  rmSync(`${RESULTS}results.jsonl`, { force: true });
}
