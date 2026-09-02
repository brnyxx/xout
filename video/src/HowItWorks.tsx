import React from 'react';
import {AbsoluteFill, Sequence, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {COPY, Lang} from './copy';

export const FPS = 30;
export const WIDTH = 1920;
export const HEIGHT = 1080;

// 타임라인(초)
const T_INTRO = 2.4;
const T_STEP = 1.35;
const STEPS = 8;
const T_CUTS_END = T_INTRO + STEPS * T_STEP; // 13.2
const T_ZOOM = T_CUTS_END + 0.2;
const T_FLY = T_ZOOM + 1.6;
const T_CARD = T_FLY + 0.9;
const T_TOOLS = T_CARD + 2.6;
const T_END = T_TOOLS + 2.6;
const T_OUT = T_END + 2.4;
export const DURATION = Math.round(T_OUT * FPS);

const INK = '#29251f';
const MUTED = '#665f55';
const PAPER = '#f7f1e7';
const PANEL = '#fffaf2';
const LINE = '#d5c9b7';
const CRIMSON = '#9f2f25';
const GREEN = '#205f52';
const TERM = '#16140f';
const SANS = "system-ui, -apple-system, 'Segoe UI', 'Apple SD Gothic Neo', 'Hiragino Sans', 'PingFang SC', sans-serif";
const MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";

const CX = 560;
const CY = 520;
const R = 330;
const START = -90;

const sector = (a0: number, a1: number, r = R): string => {
  const rad = (d: number) => (d * Math.PI) / 180;
  const x0 = CX + r * Math.cos(rad(a0));
  const y0 = CY + r * Math.sin(rad(a0));
  const x1 = CX + r * Math.cos(rad(a1));
  const y1 = CY + r * Math.sin(rad(a1));
  const large = a1 - a0 > 180 ? 1 : 0;
  return `M${CX} ${CY} L${x0} ${y0} A${r} ${r} 0 ${large} 1 ${x1} ${y1} Z`;
};

const clamp = {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'} as const;

type Props = {
  lang: Lang;
};

export const HowItWorks: React.FC<Props> = ({lang}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const t = frame / fps;
  const c = COPY[lang];

  // 원 등장
  const appear = spring({frame, fps, config: {damping: 14, stiffness: 90}});

  // 절단 단계 계산
  const removed: React.ReactNode[] = [];
  let span = 360;
  let cutLine: React.ReactNode = null;
  let cutMark: React.ReactNode = null;
  let axisLabel = '';
  let remaining: React.ReactNode = null;
  for (let k = 0; k < STEPS; k += 1) {
    const s = T_INTRO + k * T_STEP;
    const half = span / 2;
    const aCut = START + half;
    const local = (t - s) * fps; // 단계 안의 프레임
    const sweep = spring({frame: local, fps, config: {damping: 18, stiffness: 160}});
    const drop = spring({frame: local - 0.45 * fps, fps, config: {damping: 16, stiffness: 70}});
    const mid = ((aCut + half / 2) * Math.PI) / 180;
    const dx = 120 * Math.cos(mid) * drop;
    const dy = 120 * Math.sin(mid) * drop + 40 * drop * drop;
    const rot = 14 * drop * (k % 2 ? -1 : 1);
    const active = t >= s && t < s + T_STEP;
    if (t < s) {
      // 이 단계 전: 남은 영역 전체를 단일 경로로 (이음선 없음)
      remaining = span >= 360 ? <circle cx={CX} cy={CY} r={R} fill={INK} /> : <path d={sector(START, START + span)} fill={INK} />;
      break;
    }
    if (active) {
      const opacity = interpolate(drop, [0, 1], [1, 0], clamp);
      const d = span >= 360 ? sector(aCut, START + span - 0.001) : sector(aCut, START + span);
      removed.push(
        <g key={k} transform={`translate(${dx} ${dy}) rotate(${rot} ${CX} ${CY})`} opacity={opacity}>
          <path d={d} fill={INK} />
        </g>,
      );
      // 남는 절반 (마지막 단계면 last 경로가 대신 그린다)
      if (k < STEPS - 1) {
        remaining = <path d={sector(START, START + half)} fill={INK} />;
      }
    }
    if (active) {
      const len = (R + 26) * Math.min(1, sweep);
      const ex = CX + len * Math.cos((aCut * Math.PI) / 180);
      const ey = CY + len * Math.sin((aCut * Math.PI) / 180);
      const lineOpacity = interpolate(t - s, [0, 0.05, T_STEP - 0.25, T_STEP - 0.05], [0, 1, 1, 0], clamp);
      cutLine = <line x1={CX} y1={CY} x2={ex} y2={ey} stroke={CRIMSON} strokeWidth={10} strokeLinecap="round" opacity={lineOpacity} />;
      const size = Math.max(14, Math.min(34, half * 0.28));
      const xr = CX + R * 0.62 * Math.cos(mid) + dx;
      const yr = CY + R * 0.62 * Math.sin(mid) + dy;
      const markOpacity = interpolate(t - s, [0.35, 0.45, 0.85, 1.0], [0, 1, 1, 0], clamp);
      cutMark = (
        <path
          d={`M${xr - size} ${yr - size} L${xr + size} ${yr + size} M${xr + size} ${yr - size} L${xr - size} ${yr + size}`}
          stroke={CRIMSON}
          strokeWidth={Math.max(6, size * 0.45)}
          strokeLinecap="round"
          opacity={markOpacity}
        />
      );
      axisLabel = `${k + 1}/8 · ${c.axes[k]}`;
    }
    span = half;
  }
  const lastSpan = span;

  // 마지막 조각: 확대 → 초록 → 카드로 비행
  const zoom = spring({frame: (t - T_ZOOM) * fps, fps, config: {damping: 12, stiffness: 80}});
  const fly = spring({frame: (t - T_FLY) * fps, fps, config: {damping: 20, stiffness: 60}});
  const lastScale = t < T_ZOOM ? 1 : interpolate(zoom, [0, 1], [1, 2.0], clamp) * interpolate(fly, [0, 1], [1, 0.35], clamp);
  const lastX = interpolate(fly, [0, 1], [0, 960 - CX], clamp);
  const lastOpacity = t < T_CARD ? 1 : interpolate(t, [T_CARD, T_CARD + 0.3], [1, 0], clamp);
  const lastFill = t < T_ZOOM ? INK : GREEN;

  // 카드와 규칙 줄
  const cardIn = spring({frame: (t - T_CARD) * fps, fps, config: {damping: 16, stiffness: 90}});
  const toolsPhase = t >= T_TOOLS;
  const endIn = spring({frame: (t - T_END) * fps, fps, config: {damping: 16, stiffness: 90}});
  const fadeOut = interpolate(t, [T_OUT - 0.6, T_OUT - 0.05], [1, 0], clamp);
  const cardOut = interpolate(t, [T_END - 0.5, T_END - 0.05], [1, 0], clamp);

  // 자막
  const caption = t < T_INTRO ? c.space : t < T_CUTS_END ? c.cut : t < T_CARD ? c.one : t < T_TOOLS ? c.rules : t < T_END ? c.plug : c.tag;
  const captionColor = t < T_INTRO ? INK : t < T_CUTS_END ? CRIMSON : t < T_CARD ? GREEN : INK;
  const captionKey = t < T_INTRO ? 0 : t < T_CUTS_END ? 1 : t < T_CARD ? 2 : t < T_TOOLS ? 3 : t < T_END ? 4 : 5;
  const captionStart = [0, T_INTRO, T_CUTS_END, T_CARD, T_TOOLS, T_END][captionKey];
  const captionIn = spring({frame: (t - captionStart) * fps, fps, config: {damping: 18, stiffness: 120}});

  const showPie = t < T_END;
  const pieOpacity = t < T_END - 0.6 ? 1 : interpolate(t, [T_END - 0.6, T_END], [1, 0], clamp);

  return (
    <AbsoluteFill style={{backgroundColor: PAPER, fontFamily: SANS, opacity: fadeOut}}>
      {/* 상단 안내 */}
      <div style={{position: 'absolute', top: 56, left: 96, fontSize: 30, fontWeight: 700, color: MUTED, letterSpacing: 0.5}}>{c.x15}</div>
      <div style={{position: 'absolute', top: 52, right: 96, fontSize: 34, fontWeight: 800, color: INK, fontFamily: MONO}}>xout</div>

      {showPie ? (
        <svg width={WIDTH} height={HEIGHT} style={{position: 'absolute', inset: 0, opacity: pieOpacity}}>
          <g transform={`translate(${CX} ${CY}) scale(${appear}) translate(${-CX} ${-CY})`}>
            {remaining}
            {removed}
            {t >= T_INTRO + (STEPS - 1) * T_STEP ? (
              <g transform={`translate(${lastX} 0) translate(${CX} ${CY - R / 2}) scale(${lastScale}) translate(${-CX} ${-(CY - R / 2)})`} opacity={lastOpacity}>
                <path d={sector(START, START + lastSpan)} fill={lastFill} stroke={t < T_ZOOM ? 'none' : GREEN} strokeWidth={t < T_ZOOM ? 0 : 4} strokeLinejoin="round" />
              </g>
            ) : null}
            {cutLine}
            {cutMark}
          </g>
        </svg>
      ) : null}

      {/* 카운터/축 라벨 */}
      {showPie && t < T_CARD ? (
        <div style={{position: 'absolute', top: CY + R + 36, left: CX - 400, width: 800, textAlign: 'center', fontSize: 42, fontWeight: 800, color: t < T_INTRO ? INK : t < T_CUTS_END ? INK : GREEN, fontFamily: t < T_INTRO || t >= T_CUTS_END ? MONO : SANS, opacity: pieOpacity}}>
          {t < T_INTRO ? '6,561' : t < T_CUTS_END ? axisLabel : '1'}
        </div>
      ) : null}

      {/* 카드 */}
      <Sequence from={Math.round(T_CARD * fps)} durationInFrames={Math.round((T_OUT - T_CARD) * fps)}>
        <div style={{position: 'absolute', left: 600, top: 190, width: 720, transform: `translateY(${(1 - cardIn) * 40}px)`, opacity: cardIn * cardOut}}>
          <div style={{background: TERM, borderRadius: 24, padding: '28px 36px', boxShadow: '0 40px 80px -30px rgba(20,16,10,.55)'}}>
            <div style={{fontFamily: MONO, color: '#a8a193', fontSize: 24, fontWeight: 800, letterSpacing: 2, marginBottom: 18}}>XOUT.md</div>
            {c.rules8.map((line, i) => {
              const lineIn = spring({frame: (t - T_CARD - 0.25 - i * 0.18) * fps, fps, config: {damping: 18, stiffness: 140}});
              return (
                <div key={line} style={{fontFamily: MONO, color: '#f4efe4', fontSize: 28, lineHeight: 1.6, opacity: lineIn, transform: `translateX(${(1 - lineIn) * 24}px)`}}>
                  - {line}
                </div>
              );
            })}
          </div>
        </div>
      </Sequence>

      {/* 도구 칩: 카드 아래 두 줄 */}
      {toolsPhase ? (
        <div style={{position: 'absolute', left: 600, top: 760, width: 720, display: 'flex', flexWrap: 'wrap', gap: 12, opacity: cardOut}}>
          {c.tools.map((tool, i) => {
            const chipIn = spring({frame: (t - T_TOOLS - i * 0.16) * fps, fps, config: {damping: 14, stiffness: 160}});
            return (
              <div key={tool} style={{fontFamily: MONO, fontSize: 22, fontWeight: 700, color: INK, background: PANEL, border: `2px solid ${LINE}`, borderRadius: 14, padding: '8px 16px', opacity: chipIn, transform: `scale(${0.85 + 0.15 * chipIn})`}}>
                {tool}
              </div>
            );
          })}
        </div>
      ) : null}

      {/* 엔드 카드 */}
      {t >= T_END ? (
        <div style={{position: 'absolute', left: 0, top: 360, width: WIDTH, textAlign: 'center', opacity: endIn, transform: `translateY(${(1 - endIn) * 30}px)`}}>
          <div style={{fontFamily: MONO, fontSize: 64, fontWeight: 800, color: INK, background: PANEL, border: `3px solid ${LINE}`, borderRadius: 20, padding: '26px 38px', display: 'inline-block'}}>
            $ {c.cmd}
          </div>
          <div style={{marginTop: 34, fontSize: 34, fontWeight: 700, color: MUTED}}>{c.undo}</div>
        </div>
      ) : null}

      {/* 자막 */}
      <div style={{position: 'absolute', bottom: 44, left: 0, width: WIDTH, textAlign: 'center', fontSize: 44, fontWeight: 800, color: captionColor, opacity: captionIn, transform: `translateY(${(1 - captionIn) * 16}px)`}}>
        {caption}
      </div>
    </AbsoluteFill>
  );
};
