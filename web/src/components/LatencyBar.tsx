/**
 * LatencyBar — one turn's stall and first-audio, against their budgets.
 *
 * Lands after the answer, holds for `LATENCY_BAR_HOLD_MS` (4s), then fades.
 * The fill is the listening signal while inside budget and `--mv-danger` when
 * outside it; a breach pegs the fill at 100% because "how far outside" is not
 * the point (latency.ts owns that arithmetic and reads every budget from
 * qa/budgets.json).
 *
 * On the user surface the same bar draws with NO numbers, NO stage names and
 * no "ms" anywhere — just a shape that says the answer came quickly or it did
 * not. The numbers are a developer fact.
 */

import { useEffect, useState } from "react";
import {
  LATENCY_BAR_HOLD_MS,
  type TurnTiming,
  segmentsFor,
  turnBreached,
} from "../latency";

export function LatencyBar({
  timing,
  developer,
  /** Bumped once per finished turn; restarts the 4s hold. */
  turnKey,
}: {
  timing: TurnTiming;
  developer: boolean;
  turnKey: string;
}) {
  const [fading, setFading] = useState(false);
  const segments = segmentsFor(timing);

  useEffect(() => {
    if (segments.length === 0) return;
    setFading(false);
    const id = window.setTimeout(() => setFading(true), LATENCY_BAR_HOLD_MS);
    return () => window.clearTimeout(id);
    // turnKey, not `timing`: a second stage landing inside one turn must not
    // restart the hold.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [turnKey, segments.length]);

  if (segments.length === 0) return null;

  return (
    <div
      className={
        developer
          ? `pt-lat${fading ? " is-fading" : ""}`
          : `pt-lat pt-lat--plain${fading ? " is-fading" : ""}`
      }
      data-testid="latency-bar"
      data-breach={turnBreached(timing) ? "true" : "false"}
      aria-hidden={developer ? undefined : true}
    >
      {segments.map((seg, i) => (
        <div className="pt-lat-row" key={seg.key}>
          {developer ? <span className="pt-lbl">{seg.label}</span> : null}
          <div className="pt-lat-track">
            <div
              className={seg.className}
              // The stage's name is itself an engineering string, so the user
              // surface's hook is its position, not `stall` / `first_audio`.
              data-testid={developer ? `latency-fill-${seg.key}` : `latency-fill-${i}`}
              style={{ width: `${seg.pct}%` }}
            />
          </div>
          {developer ? (
            <span
              className={
                seg.breach ? "pt-lat-status is-breach" : "pt-lat-status"
              }
            >
              {`${Math.round(seg.ms)} / ${seg.budgetMs} ms`}
            </span>
          ) : null}
        </div>
      ))}
    </div>
  );
}
