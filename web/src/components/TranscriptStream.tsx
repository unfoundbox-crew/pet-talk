/**
 * TranscriptStream — the spoken record, with read-ahead.
 *
 * Receipts Over Prose: a spoken line is the claim, and the chip under it is
 * the proof. Both live in the same column, left-aligned on one grid, so the
 * eye reads claim → proof without travelling.
 *
 * Read-ahead is the other half. The server hands over sentences faster than
 * they can be said, so a sentence that has arrived but is not yet spoken
 * renders DIM (`data-phase="buffered"`). You can read ahead of the voice, and
 * the right arrow skips to the next one — the play head moves, the sentence
 * stays (see readAhead.ts).
 *
 * User surface vs developer surface, the hard rule this component enforces:
 * with `developer={false}` it prints NO frame name, NO millisecond number and
 * NO persona name. The role gutter says "you" and "reply", never "donna" and
 * never "agent.sentence". `developer={true}` adds the seq and the frame name.
 * `qa`-side proof: web/src/devSurface.test.tsx renders this component both
 * ways and greps the markup.
 *
 * Pure presentation: every colour is a token class in styles/app.css, and no
 * effect, timer or socket touches this file. That is what makes the grep test
 * possible with renderToStaticMarkup.
 */

import type { ReactNode } from "react";
import { ReceiptChip, type ReceiptFrame } from "./ReceiptChip";
import type { SentencePhase } from "../readAhead";

export type StreamWho = "user" | "agent" | "thinking" | "eyes" | "notice";

export interface StreamLine {
  id: string;
  who: StreamWho;
  text: string;
  /** Only set for agent sentences — where this line sits against the voice. */
  phase?: SentencePhase;
  /** Sentence ordinal on the wire. Developer surface only. */
  seq?: number;
  /** The frame that produced this line. Developer surface only. */
  frame?: string;
  receipt?: ReceiptFrame;
  /** Rendered in place of text — the eyes block owns its own layout. */
  slot?: ReactNode;
}

/**
 * What the gutter says. Never the persona's name — that is engineering.
 *
 * The keys avoid engineering vocabulary too ("thinking", not "stall"), because
 * `data-who` lands in the DOM and devSurface.test.tsx greps the whole markup,
 * not just its text nodes. A class name the user can read in devtools counts.
 */
const ROLE_LABEL: Record<StreamWho, string> = {
  user: "you",
  agent: "reply",
  thinking: "thinking",
  eyes: "reading",
  notice: "note",
};

export function TranscriptStream({
  lines,
  developer,
  emptyHint,
}: {
  lines: StreamLine[];
  developer: boolean;
  emptyHint?: string;
}) {
  if (lines.length === 0 && emptyHint) {
    return (
      <div className="pt-stream">
        <p className="pt-note">{emptyHint}</p>
      </div>
    );
  }

  return (
    <div className="pt-stream" data-testid="transcript-stream">
      {lines.map((line) => {
        const phase = line.who === "agent" ? (line.phase ?? "spoken") : undefined;
        return (
          <div
            key={line.id}
            className="pt-turn pt-arrive"
            data-phase={phase}
            data-who={line.who}
          >
            <div className="pt-role">
              {ROLE_LABEL[line.who]}
              {developer && line.seq !== undefined ? ` ${line.seq}` : ""}
              {developer && line.frame ? (
                <div className="pt-log-payload">{line.frame}</div>
              ) : null}
            </div>
            <div>
              {line.slot ?? (
                <p
                  className={
                    line.who === "thinking"
                      ? "pt-said pt-said--thinking"
                      : line.who === "user"
                        ? "pt-said pt-said--user"
                        : "pt-said"
                  }
                >
                  {line.text}
                </p>
              )}
              {line.receipt ? (
                <div className="pt-arrive pt-arrive--receipt">
                  <ReceiptChip receipt={line.receipt} />
                </div>
              ) : null}
            </div>
          </div>
        );
      })}
    </div>
  );
}
