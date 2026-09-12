/**
 * turnGuard — decides whether an incoming ServerFrame belongs to the turn
 * the client currently cares about (Finding 6).
 *
 * The server can send frames for a turn the client has already moved past —
 * most commonly a barge: `_on_barge` (server/ws.py) mints a fresh turn_id
 * and flushes the queue, but a sentence already in flight through the old
 * turn's pipeline can still land after that. Applying it anyway would
 * re-enter the read-ahead buffer and speak a line the user just interrupted.
 *
 * `state.idle` and `state.listening` are the one exception: they are how the
 * server hands the client a FRESH turn id (a barge ack, a natural idle after
 * agent.done), so they must be accepted for a newer turn too — and the
 * caller then adopts that turn id as current. Refusing them would wedge the
 * client, forever guarding a turn the server has already moved on from.
 *
 * Pure, synchronous, no React — App.tsx calls this before its frame switch;
 * vitest drives it directly.
 */

/** Turn ids are `${Date.now().toString(36)}-${rand}` (see ws.ts newTurnId).
 * Decode the timestamp prefix so "newer" has a real meaning. */
function turnTimestamp(turnId: string): number | null {
  const prefix = turnId.split("-")[0];
  const n = parseInt(prefix, 36);
  return Number.isFinite(n) ? n : null;
}

/**
 * True when `candidate` is a newer turn than `current`.
 *
 * An unparseable prefix on either side — a turn id in some shape this client
 * has never minted — is treated as newer. Failing open here means a
 * server-minted id in an unexpected shape gets ADOPTED rather than
 * permanently ignored, which would otherwise deadlock the client on a turn
 * it can never recognize as current again.
 */
export function isNewerTurn(candidate: string, current: string): boolean {
  if (candidate === current) return false;
  const c = turnTimestamp(candidate);
  const cur = turnTimestamp(current);
  if (c === null || cur === null) return true;
  return c > cur;
}

/** Frame types that may legitimately carry a freshly-minted turn id. */
const ADOPTABLE_TYPES = new Set(["state.idle", "state.listening"]);

/**
 * Should App.tsx act on this frame at all?
 *
 * Accepted when the frame's turn matches the current turn, or — for
 * `state.idle` / `state.listening` only — when the frame's turn is newer
 * than current. Everything else (any frame for an older turn, or a
 * non-adoptable frame for some other turn entirely) is dropped: it belongs
 * to a turn the client has already moved past.
 */
export function shouldApplyFrame(
  frameType: string,
  frameTurnId: string,
  currentTurnId: string,
): boolean {
  if (frameTurnId === currentTurnId) return true;
  return ADOPTABLE_TYPES.has(frameType) && isNewerTurn(frameTurnId, currentTurnId);
}

/**
 * True when accepting this frame should also make its turn id the new
 * current turn (an adoption, not just an accept-in-place for the turn
 * already active).
 */
export function shouldAdoptTurn(
  frameType: string,
  frameTurnId: string,
  currentTurnId: string,
): boolean {
  return (
    frameTurnId !== currentTurnId &&
    ADOPTABLE_TYPES.has(frameType) &&
    isNewerTurn(frameTurnId, currentTurnId)
  );
}
