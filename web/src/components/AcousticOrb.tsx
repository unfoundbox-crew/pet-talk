import React, { useEffect, useRef } from "react";
import { AgentState } from "../ws";

interface AcousticOrbProps {
  state: AgentState;
  analyser: AnalyserNode | null;
  talking: boolean;
  size?: number;
}

/** A level past this fraction of full scale reads as "pegged" — the meter's
 *  own --pt-listening-peg colour, not a softer warning shade of the signal. */
const PEG_THRESHOLD = 0.85;

/** Canvas 2D cannot resolve `var(--x)` in a fillStyle/strokeStyle string, so
 *  the acoustic tokens (and the couple of neutrals the core gradient rests
 *  on) are read off the root's computed style. This is colour resolution,
 *  not a literal — the value only exists once the browser reads the token. */
function readToken(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** Alpha-blend a resolved token for a gradient stop, via CSS `color-mix()`
 *  rather than building an rgba string — keeps this file literal-free
 *  regardless of what format a token resolves to. */
function withAlpha(resolved: string, alpha: number): string {
  return `color-mix(in srgb, ${resolved} ${Math.round(alpha * 100)}%, transparent)`;
}

export const AcousticOrb: React.FC<AcousticOrbProps> = ({
  state,
  analyser,
  talking,
  size = 220,
}) => {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const animRef = useRef<number | null>(null);
  const phaseRef = useRef<number>(0);
  const reducedRef = useRef(false);

  // Reduced motion only zeroes the DECORATIVE time-phase wobble below; the
  // live level itself (volume, per-bin frequency data) keeps updating every
  // frame regardless — it's data, not an animation.
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    reducedRef.current = mq.matches;
    const onChange = () => {
      reducedRef.current = mq.matches;
    };
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dataArray = new Uint8Array(analyser ? analyser.frequencyBinCount : 32);

    const render = () => {
      phaseRef.current += 0.03;
      const t = phaseRef.current;
      const reduced = reducedRef.current;

      let volume = 0;
      if (analyser) {
        analyser.getByteFrequencyData(dataArray);
        let sum = 0;
        for (let i = 0; i < dataArray.length; i++) {
          sum += dataArray[i];
        }
        volume = sum / dataArray.length / 255;
      }

      ctx.clearRect(0, 0, size, size);
      const cx = size / 2;
      const cy = size / 2;
      const baseRadius = size * 0.28;

      // Colour: the live level always reads as --pt-listening-signal; a
      // pegged (near-clipping) level switches to --pt-listening-peg. Idle
      // (no state, no level) falls back to the neutral graticule colour —
      // those are the only two acoustic tokens this component is allowed.
      const signal = readToken("--pt-listening-signal");
      const peg = readToken("--pt-listening-peg");
      const idle = readToken("--mv-faint");
      const ground = readToken("--mv-ground");
      const ink = readToken("--mv-ink");

      const active = state === "listening" || state === "thinking" || state === "speaking" || talking;
      const pegged = active && volume > PEG_THRESHOLD;
      const baseHex = pegged ? peg : active ? signal : idle;

      let rings = 3;
      if (state === "listening" || state === "thinking" || talking) rings = 4;
      if (state === "speaking") rings = 5;

      const primaryAlpha = active ? (pegged ? 0.95 : 0.85) : 0.4;
      const glowAlpha = active ? (pegged ? 0.45 : 0.35) : 0.2;
      const primaryColor = withAlpha(baseHex, primaryAlpha);
      const glowColor = withAlpha(baseHex, glowAlpha);

      // 1. Ambient Glow
      const grad = ctx.createRadialGradient(
        cx,
        cy,
        baseRadius * 0.4,
        cx,
        cy,
        baseRadius * (1.6 + volume * 0.8),
      );
      grad.addColorStop(0, primaryColor);
      grad.addColorStop(0.6, glowColor);
      grad.addColorStop(1, "transparent");

      ctx.beginPath();
      ctx.arc(cx, cy, baseRadius * (1.6 + volume * 0.8), 0, Math.PI * 2);
      ctx.fillStyle = grad;
      ctx.fill();

      // 2. Dynamic Concentric Harmonic Rings
      for (let r = 0; r < rings; r++) {
        const wobble = reduced ? 0 : Math.sin(t + r) * 0.05;
        const ringScale = 1 + (r + 1) * 0.18 + wobble;
        const currentR = baseRadius * ringScale * (1 + volume * 0.4);

        ctx.beginPath();
        const steps = 64;
        for (let i = 0; i <= steps; i++) {
          const angle = (i / steps) * Math.PI * 2;
          let wave = 0;
          if (state === "thinking") {
            wave = reduced ? 0 : Math.sin(angle * 4 + t * 2) * 4;
          } else if (state === "speaking" || state === "listening" || talking) {
            const binIdx = Math.floor((i / steps) * (dataArray.length / 2));
            const freqVal = dataArray[binIdx] ? dataArray[binIdx] / 255 : 0;
            const spin = reduced ? angle * 6 : angle * 6 + t;
            wave = freqVal * 16 * Math.sin(spin);
          } else {
            wave = reduced ? 0 : Math.sin(angle * 3 + t) * 2;
          }

          const x = cx + (currentR + wave) * Math.cos(angle);
          const y = cy + (currentR + wave) * Math.sin(angle);
          if (i === 0) {
            ctx.moveTo(x, y);
          } else {
            ctx.lineTo(x, y);
          }
        }
        ctx.closePath();
        ctx.strokeStyle = primaryColor;
        ctx.lineWidth = r === 0 ? 2 : 1;
        ctx.globalAlpha = Math.max(0.1, 1 - r * 0.22);
        ctx.stroke();
      }

      // 3. Central Core Orb
      ctx.globalAlpha = 1.0;
      const corePulse = reduced ? 0 : Math.sin(t * 1.2) * 0.05;
      const coreR = baseRadius * (0.85 + corePulse + volume * 0.2);
      const coreGrad = ctx.createRadialGradient(
        cx - coreR * 0.25,
        cy - coreR * 0.25,
        coreR * 0.1,
        cx,
        cy,
        coreR,
      );
      coreGrad.addColorStop(0, ink);
      coreGrad.addColorStop(0.4, primaryColor);
      coreGrad.addColorStop(1, ground);

      ctx.beginPath();
      ctx.arc(cx, cy, coreR, 0, Math.PI * 2);
      ctx.fillStyle = coreGrad;
      ctx.shadowBlur = 18;
      ctx.shadowColor = baseHex;
      ctx.fill();
      ctx.shadowBlur = 0;

      animRef.current = requestAnimationFrame(render);
    };

    animRef.current = requestAnimationFrame(render);
    return () => {
      if (animRef.current) cancelAnimationFrame(animRef.current);
    };
  }, [state, analyser, talking, size]);

  return (
    <div style={{ margin: "var(--pt-s4) 0" }}>
      <canvas
        ref={canvasRef}
        width={size}
        height={size}
        style={{
          width: size,
          height: size,
          display: "block",
          filter: "drop-shadow(0 4px 16px var(--mv-border))",
        }}
      />
    </div>
  );
};
