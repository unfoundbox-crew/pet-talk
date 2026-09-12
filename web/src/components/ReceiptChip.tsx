/**
 * ReceiptChip — the proof under a spoken claim.
 *
 * When the agent says work was done, an `agent.receipt` frame says what
 * proves it: the session or commit it came from, the tokens that session
 * burned, and how old the index was when it answered. The chip renders that
 * under the spoken line so the claim is checkable, not just audible.
 *
 * Two rules from AgentWorth's own design doc, both deliberate:
 *
 *   1. `--mv-accent` (the violet) carries the receipt's TOTAL LINE and
 *      nothing else. Every other chip here is the neutral listening signal.
 *      One accent per receipt, on the number that sums it up.
 *   2. Archie never appears on a receipt. "He is a state, not a texture" —
 *      the receipt is one of his three forbidden places, alongside the
 *      landing hero and machine-readable output. There is no mascot import
 *      in this file and qa/test_receipts.py fails if one appears.
 *
 * Every colour is a token from `web/src/styles/tokens.css` (AgentWorth
 * vendor) or `tokens.pet-talk.css` (this product's layer), both imported in
 * main.tsx. No hand-rolled hex — qa/test_design_tokens.py enforces that, and
 * tokens are the only thing that is theme-aware in both directions.
 *
 * Chip geometry follows the approved direction sheet
 * (ApplePrimitives.dc.html `.chip`): mono, 9.5px, signal fill, a 1px signal
 * border, 3px 7px padding, 3px radius.
 */

import type { CSSProperties } from "react";

/** Token references, not values. The CSS layer owns every colour. */
const T = {
  /** AgentWorth's one accent. Violet in both themes, light/dark resolved by CSS. */
  accent: "var(--mv-accent)",
  /** The chip fill: pet-talk's listening signal, amber in both themes. */
  signal: "var(--pt-listening-signal)",
  /** No signal-border token exists yet; derive it rather than hand-roll a hex. */
  signalBorder: "color-mix(in srgb, var(--pt-listening-signal) 42%, var(--mv-ground))",
  stale: "var(--mv-danger)",
  staleBorder: "color-mix(in srgb, var(--mv-danger) 42%, var(--mv-ground))",
  mono: "var(--font-mono)",
};

const CHIP: CSSProperties = {
  fontFamily: T.mono,
  fontSize: "9.5px",
  color: T.signal,
  border: `1px solid ${T.signalBorder}`,
  padding: "3px 7px",
  borderRadius: "3px",
  background: "transparent",
  lineHeight: 1.3,
  whiteSpace: "nowrap",
};

export interface ReceiptSource {
  kind: "session" | "commit" | "scan";
  id: string;
  repo: string;
  path: string;
}

export interface ReceiptFrame {
  type: "agent.receipt";
  turn_id: string;
  claim: string;
  source: ReceiptSource;
  tokens: number;
  cost_usd: number | null;
  index_age_s: number;
  fresh: boolean;
}

/**
 * Narrow an arbitrary server frame to a receipt.
 *
 * Lives here rather than in ws.ts so the receipt lane owns its own frame
 * shape and the audio lane's discriminated union stays untouched.
 */
export function asReceiptFrame(frame: unknown): ReceiptFrame | null {
  if (typeof frame !== "object" || frame === null) return null;
  const f = frame as Record<string, unknown>;
  if (f.type !== "agent.receipt") return null;
  const source = f.source as Record<string, unknown> | undefined;
  if (!source || typeof source.kind !== "string" || typeof source.id !== "string") return null;
  return {
    type: "agent.receipt",
    turn_id: String(f.turn_id ?? ""),
    claim: String(f.claim ?? ""),
    source: {
      kind: source.kind as ReceiptSource["kind"],
      id: source.id,
      repo: String(source.repo ?? ""),
      path: String(source.path ?? ""),
    },
    tokens: Number(f.tokens ?? 0),
    cost_usd: typeof f.cost_usd === "number" ? f.cost_usd : null,
    index_age_s: Number(f.index_age_s ?? 0),
    fresh: Boolean(f.fresh),
  };
}

/** 18400 -> "18.4k", 17350767 -> "17.4M". Never a rounded-to-zero figure. */
export function formatTokens(tokens: number): string {
  const n = Math.max(0, Math.round(tokens || 0));
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

/** Seconds -> the coarsest honest unit. 0s reads "just now", never "0m". */
export function formatIndexAge(seconds: number): string {
  const s = Math.max(0, Math.round(seconds || 0));
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

function shortId(id: string): string {
  const trimmed = (id || "").trim();
  // A UUID or an `agent-…` id is unreadable in full at 9.5px; the copy
  // button hands over the whole thing, so the label can be the short form.
  return trimmed.length > 12 ? `${trimmed.slice(0, 8)}…` : trimmed;
}

export function ReceiptChip({ receipt }: { receipt: ReceiptFrame }) {
  const { source, tokens, index_age_s, fresh } = receipt;
  const sourceLabel =
    source.kind === "commit"
      ? `commit ${shortId(source.id)}`
      : source.kind === "scan"
        ? `scan ${shortId(source.id)}`
        : `session ${shortId(source.id)}`;

  const copyId = () => {
    const write = navigator.clipboard?.writeText?.bind(navigator.clipboard);
    if (write) void write(source.id);
  };

  return (
    <div
      data-testid="receipt-chip"
      data-receipt-kind={source.kind}
      data-receipt-fresh={fresh ? "true" : "false"}
      style={{
        display: "flex",
        alignItems: "center",
        flexWrap: "wrap",
        gap: "7px",
        marginTop: "0.45rem",
      }}
    >
      <button
        type="button"
        onClick={copyId}
        title={`Copy ${source.kind} id: ${source.id}`}
        aria-label={`Copy ${source.kind} id ${source.id}`}
        style={{ ...CHIP, cursor: "pointer" }}
      >
        {sourceLabel}
      </button>

      {source.path ? <span style={CHIP}>{source.path}</span> : null}

      <span
        data-testid="receipt-index-age"
        title={fresh ? "Index scanned after the last commit" : "Index has not seen the last commit"}
        style={
          fresh
            ? CHIP
            : { ...CHIP, color: T.stale, borderColor: T.staleBorder }
        }
      >
        {fresh ? `index ${formatIndexAge(index_age_s)}` : `index behind · ${formatIndexAge(index_age_s)}`}
      </span>

      {/* The total line. The one place the accent is allowed. */}
      <span
        data-testid="receipt-total"
        style={{
          fontFamily: T.mono,
          fontSize: "9.5px",
          color: T.accent,
          fontVariantNumeric: "tabular-nums",
          marginLeft: "auto",
          paddingLeft: "8px",
        }}
      >
        {formatTokens(tokens)} tok
      </span>
    </div>
  );
}
