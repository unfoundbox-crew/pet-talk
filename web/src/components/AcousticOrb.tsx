import React, { useEffect, useRef } from "react";
import { AgentState } from "../ws";

interface AcousticOrbProps {
  state: AgentState;
  analyser: AnalyserNode | null;
  talking: boolean;
  size?: number;
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

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dataArray = new Uint8Array(analyser ? analyser.frequencyBinCount : 32);

    const render = () => {
      phaseRef.current += 0.03;
      const t = phaseRef.current;

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

      // Color scheme based on Google Labs DESIGN.md
      let primaryColor = "rgba(99, 108, 132, 0.4)"; // idle slate
      let glowColor = "rgba(99, 108, 132, 0.2)";
      let rings = 3;

      if (state === "listening" || talking) {
        primaryColor = "rgba(0, 200, 83, 0.85)"; // emerald
        glowColor = "rgba(0, 200, 83, 0.35)";
        rings = 4;
      } else if (state === "thinking") {
        primaryColor = "rgba(36, 193, 224, 0.9)"; // gemini cyan
        glowColor = "rgba(161, 66, 244, 0.4)"; // gemini purple
        rings = 4;
      } else if (state === "speaking") {
        primaryColor = "rgba(255, 179, 0, 0.9)"; // amber gold
        glowColor = "rgba(255, 179, 0, 0.4)";
        rings = 5;
      }

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
      grad.addColorStop(1, "rgba(9, 10, 15, 0)");

      ctx.beginPath();
      ctx.arc(cx, cy, baseRadius * (1.6 + volume * 0.8), 0, Math.PI * 2);
      ctx.fillStyle = grad;
      ctx.fill();

      // 2. Dynamic Concentric Harmonic Rings
      for (let r = 0; r < rings; r++) {
        const ringScale = 1 + (r + 1) * 0.18 + Math.sin(t + r) * 0.05;
        const currentR = baseRadius * ringScale * (1 + volume * 0.4);

        ctx.beginPath();
        const steps = 64;
        for (let i = 0; i <= steps; i++) {
          const angle = (i / steps) * Math.PI * 2;
          let wave = 0;
          if (state === "thinking") {
            wave = Math.sin(angle * 4 + t * 2) * 4;
          } else if (state === "speaking" || state === "listening" || talking) {
            const binIdx = Math.floor((i / steps) * (dataArray.length / 2));
            const freqVal = dataArray[binIdx] ? dataArray[binIdx] / 255 : 0;
            wave = freqVal * 16 * Math.sin(angle * 6 + t);
          } else {
            wave = Math.sin(angle * 3 + t) * 2;
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
      const coreR = baseRadius * (0.85 + Math.sin(t * 1.2) * 0.05 + volume * 0.2);
      const coreGrad = ctx.createRadialGradient(
        cx - coreR * 0.25,
        cy - coreR * 0.25,
        coreR * 0.1,
        cx,
        cy,
        coreR,
      );
      coreGrad.addColorStop(0, "#ffffff");
      coreGrad.addColorStop(0.4, primaryColor);
      coreGrad.addColorStop(1, "#090a0f");

      ctx.beginPath();
      ctx.arc(cx, cy, coreR, 0, Math.PI * 2);
      ctx.fillStyle = coreGrad;
      ctx.shadowBlur = 18;
      ctx.shadowColor = primaryColor;
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
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        margin: "1rem 0",
      }}
    >
      <canvas
        ref={canvasRef}
        width={size}
        height={size}
        style={{
          width: size,
          height: size,
          display: "block",
          filter: "drop-shadow(0 4px 16px rgba(0,0,0,0.5))",
        }}
      />
    </div>
  );
};
