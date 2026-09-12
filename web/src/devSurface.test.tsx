/**
 * The user surface says nothing about engineering. The developer surface says
 * all of it. This test renders both to static markup and greps the DOM.
 *
 * The rule (docs/SPEC.md, product rules): a user surface shows no frame names,
 * no millisecond numbers and no persona name. Those live behind the Developer
 * toggle. A rule nobody greps is a rule that rots, so the grep is here, over
 * the real components, with a turn whose data carries every forbidden string.
 */

import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { TranscriptStream, type StreamLine } from "./components/TranscriptStream";
import { LatencyBar } from "./components/LatencyBar";
import { DeveloperRail, type LoggedFrame } from "./components/DeveloperRail";
import { MemoryDrawer, type MemoryTurn } from "./components/MemoryDrawer";
import { PromptComposer } from "./components/PromptComposer";
import { EyesBlock, type EyesEntry } from "./components/EyesAttach";
import { SettingsModal, type RuntimeSettings, type ActiveProviders } from "./components/SettingsModal";
import { plainReason } from "./reasonText";
import { STALL_BUDGET_MS } from "./latency";
import type { ReceiptFrame } from "./components/ReceiptChip";
import en from "./i18n/en.json";
import hi from "./i18n/hi.json";

const PERSONA = "donna";

const RECEIPT: ReceiptFrame = {
  type: "agent.receipt",
  turn_id: "t-1",
  claim: "The provider fallback test passes.",
  source: { kind: "session", id: "3c91aa02bb14", repo: "pet-talk", path: "qa/test_providers.py" },
  tokens: 18400,
  cost_usd: null,
  index_age_s: 12,
  fresh: true,
};

const LINES: StreamLine[] = [
  { id: "u1", who: "user", text: "Did the provider tests pass?", frame: "transcript.user" },
  { id: "s1", who: "thinking", text: "On it — one sec.", frame: "agent.stall" },
  {
    id: "a1",
    who: "agent",
    phase: "speaking",
    seq: 0,
    frame: "agent.sentence",
    text: "The provider fallback test passes.",
    receipt: RECEIPT,
  },
  {
    id: "a2",
    who: "agent",
    phase: "buffered",
    seq: 1,
    frame: "agent.sentence",
    text: "Nothing else changed in that suite.",
  },
];

const FRAMES: LoggedFrame[] = [
  {
    id: "f1",
    clock: "14:22:01",
    direction: "in",
    name: "agent.sentence",
    payload: { type: "agent.sentence", seq: 0, text: "The provider fallback test passes." },
  },
];

const TIMING = { stallMs: STALL_BUDGET_MS + 120, firstAudioMs: 640 };

function userSurface(): string {
  return renderToStaticMarkup(
    <>
      <TranscriptStream lines={LINES} developer={false} />
      <LatencyBar timing={TIMING} developer={false} turnKey="t-1" />
    </>,
  );
}

/** Every engineering string the surfaces are allowed to differ on. */
const FORBIDDEN: { name: string; re: RegExp }[] = [
  { name: "frame names (agent.*)", re: /agent\./ },
  { name: "frame names (state.*/transcript.*)", re: /\b(state|transcript)\.\w+/ },
  { name: "millisecond numbers", re: /\d+\s*ms\b/ },
  { name: "the unit ms on its own", re: /\bms\b/ },
  { name: "the word stall", re: /stall/i },
  { name: "the persona name", re: new RegExp(PERSONA, "i") },
];

describe("the Developer toggle is what reveals engineering", () => {
  const markup = userSurface();

  for (const { name, re } of FORBIDDEN) {
    it(`user surface carries no ${name}`, () => {
      expect(markup).not.toMatch(re);
    });
  }

  it("user surface still shows the spoken line and its receipt", () => {
    expect(markup).toContain("The provider fallback test passes.");
    expect(markup).toContain("qa/test_providers.py");
    expect(markup).toContain('data-testid="receipt-chip"');
  });

  it("user surface marks a buffered sentence dim rather than hiding it", () => {
    expect(markup).toContain('data-phase="buffered"');
    expect(markup).toContain("Nothing else changed in that suite.");
  });

  it("user surface draws the latency bar with no number on it", () => {
    expect(markup).toContain('data-testid="latency-bar"');
    expect(markup).toContain('data-breach="true"');
    expect(markup).not.toMatch(/\d+\s*\/\s*\d+/);
  });

  it("developer surface shows every string the user surface withheld", () => {
    const dev = renderToStaticMarkup(
      <>
        <TranscriptStream lines={LINES} developer={true} />
        <LatencyBar timing={TIMING} developer={true} turnKey="t-1" />
        <DeveloperRail
          frames={FRAMES}
          timing={TIMING}
          personaName={PERSONA}
          providers={{ stt: "FasterWhisperSTT", llm: "OpenAICompatibleLLM", tts: "KokoroLocalTTS" }}
          queueLength={1}
          bufferedCount={1}
          lastReceipt={RECEIPT}
        />
      </>,
    );
    expect(dev).toMatch(/agent\.sentence/);
    expect(dev).toMatch(/\bms\b/);
    expect(dev).toMatch(/stall/);
    expect(dev).toContain(PERSONA);
    expect(dev).toContain(String(STALL_BUDGET_MS));
  });
});

describe("developer-off hides engineering strings in the other cockpit surfaces (Finding 8)", () => {
  const memoryTurns: MemoryTurn[] = [
    { turn_id: "t-1", timestamp: 1710000000, persona: "donna", user: "hi", agent: "hello" },
  ];

  const eyesEntry: EyesEntry = {
    ref: "att-1",
    kind: "image",
    filename: "shot.png",
    status: "error",
    reason: "stt_empty_audio",
    detail: "no audio bytes",
    time: "10:00:00",
  };

  const settingsBaseProps = {
    isOpen: true,
    onClose: () => undefined,
    settings: {
      stt_provider: "sensevoice",
      llm_provider: "litellm",
      llm_base_url: "",
      llm_model: "claude-3-7-sonnet",
      tts_provider: "kokoro",
      kokoro_base_url: "http://127.0.0.1:8088",
      vad_silence_ms: 600,
    } as RuntimeSettings,
    activeProviders: { stt: "StubSTT", llm: "StubLLM", tts: "StubTTS" } as ActiveProviders,
    secretsSet: {},
    onApplySettings: async () => undefined,
  };

  function renderSurface(developer: boolean): string {
    return renderToStaticMarkup(
      <>
        <MemoryDrawer
          isOpen
          onClose={() => undefined}
          turns={memoryTurns}
          onClearMemory={async () => undefined}
          developer={developer}
          t={en}
        />
        {/* Empty `t` exercises the code-level fallback placeholder, not just
         * the i18n bundle, since that is what Finding 8 named the leak in. */}
        <PromptComposer onSend={() => undefined} t={{}} />
        <EyesBlock entry={eyesEntry} developer={developer} />
        <SettingsModal {...settingsBaseProps} developer={developer} t={en} />
      </>,
    );
  }

  const markupOff = renderSurface(false);
  const markupOn = renderSurface(true);

  for (const { name, re } of FORBIDDEN) {
    it(`developer-off carries no ${name}`, () => {
      expect(markupOff).not.toMatch(re);
    });
  }

  // The "Active" providers block, isolated from the STT/LLM/TTS <select>
  // dropdowns just below it — those necessarily name every engine so the
  // user can pick one, developer or not; Finding 8 is about the *active*
  // provider readout, not the picker.
  function activeProvidersBlock(markup: string): string {
    const match = markup.match(/<dl class="pt-kv">[\s\S]*?<\/dl>/);
    if (!match) throw new Error("Active providers <dl> not found in markup");
    return match[0];
  }

  it("developer-off carries no persona name or raw wire reason", () => {
    expect(markupOff).not.toMatch(/donna/i);
    expect(markupOff).not.toMatch(/stt_empty_audio/);
    expect(markupOff).not.toMatch(/no audio bytes/);
  });

  it("developer-off's Active providers block carries no provider class name", () => {
    const block = activeProvidersBlock(markupOff);
    expect(block).not.toContain("StubSTT");
    expect(block).not.toContain("StubLLM");
    expect(block).not.toContain("StubTTS");
  });

  it("developer-off still shows the plain-language reason for the failed attachment", () => {
    // renderToStaticMarkup HTML-escapes the apostrophe as an entity.
    const expected = plainReason("stt_empty_audio").replace("'", "&#x27;");
    expect(markupOff).toContain(expected);
  });

  it("developer-on reveals the persona, active-provider class name, raw reason and ms unit", () => {
    expect(markupOn).toMatch(/donna/i);
    expect(activeProvidersBlock(markupOn)).toContain("StubSTT");
    expect(markupOn).toMatch(/stt_empty_audio/);
    expect(markupOn).toMatch(/\d+\s*ms\b/);
  });
});

describe("no persona name reaches a user-facing string", () => {
  // The persona is chosen on the server and can be renamed there; these three
  // ship in the repo, so a placeholder that names one is a leak we can catch.
  const SHIPPED = ["donna", "jarvis", "zuck", "डोना"];

  for (const [name, bundle] of [["en", en], ["hi", hi]] as const) {
    it(`${name}.json names no persona`, () => {
      const blob = JSON.stringify(bundle).toLowerCase();
      for (const persona of SHIPPED) {
        expect(blob).not.toContain(persona);
      }
    });
  }
});
