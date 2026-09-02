import React from 'react';
import {AbsoluteFill} from 'remotion';
import {COPY, Lang} from './copy';

// 1200x630 소셜 카드 - 언어별 정적 스틸. og:image 로 쓰인다.
const PAPER = '#f7f3ea';
const INK = '#171717';
const CRIMSON = '#d92332';
const MUTED = '#6e6a63';
const SURFACE = '#d9d4c9';
const SANS = "system-ui, -apple-system, 'Segoe UI', 'Apple SD Gothic Neo', 'Hiragino Sans', 'PingFang SC', sans-serif";
const MONO = "ui-monospace, SFMono-Regular, Menlo, 'Apple SD Gothic Neo', 'Hiragino Sans', 'PingFang SC', monospace";

const CARD: Record<Lang, {ask: string; wrong: string; kept: string; ruleLabel: string; rule1: string; rule2: string; footer: string}> = {
  en: {ask: 'Fix the bug.', wrong: 'Should I start?', kept: 'Fixed. Tests pass.', ruleLabel: 'GENERATED RULE', rule1: 'Act first.', rule2: 'Report after.', footer: 'Cross out the wrong behavior. Keep the rule.'},
  ko: {ask: '버그 고쳐줘.', wrong: '시작할까요?', kept: '고쳤고 테스트 통과.', ruleLabel: '만들어진 규칙', rule1: '먼저 실행한다.', rule2: '그다음 보고한다.', footer: '아닌 행동에 X를 치고 규칙을 남긴다.'},
  ja: {ask: 'バグを直して。', wrong: '始めていい？', kept: '直した。テスト通過。', ruleLabel: '生成されたルール', rule1: '先に実行する。', rule2: 'あとで報告する。', footer: '違う振る舞いを X で消し、ルールを残す。'},
  zh: {ask: '修一下这个 bug。', wrong: '我可以开始吗？', kept: '修好了。测试通过。', ruleLabel: '生成的规则', rule1: '先执行。', rule2: '再汇报。', footer: '划掉错的行为，留下规则。'},
};

export const SocialCard: React.FC<{lang: Lang}> = ({lang}) => {
  const c = CARD[lang];
  const tag = COPY[lang].tag;
  return (
    <AbsoluteFill style={{backgroundColor: PAPER, fontFamily: SANS, color: INK}}>
      <div style={{position: 'absolute', left: 64, top: 44, fontSize: 56, fontWeight: 800, letterSpacing: 6}}>XOUT</div>
      <div style={{position: 'absolute', left: 64, top: 118, fontSize: 26, fontWeight: 700, color: MUTED}}>{tag}</div>

      <div style={{position: 'absolute', left: 64, top: 190, width: 420, height: 78, background: SURFACE, border: `5px solid ${INK}`, borderRadius: 14, display: 'flex', alignItems: 'center', paddingLeft: 24, fontFamily: MONO, fontSize: 32, fontWeight: 800}}>{c.ask}</div>

      <div style={{position: 'absolute', left: 64, top: 300, width: 500, height: 74, border: `5px solid ${INK}`, borderRadius: 14, display: 'flex', alignItems: 'center', paddingLeft: 24, fontFamily: MONO, fontSize: 28, fontWeight: 800, color: MUTED}}>
        {c.wrong}
        <svg width="500" height="74" style={{position: 'absolute', left: -5, top: -5}}>
          <path d="M18 12 L482 62 M482 12 L18 62" stroke={CRIMSON} strokeWidth={12} strokeLinecap="round" />
        </svg>
      </div>
      <div style={{position: 'absolute', left: 64, top: 396, width: 500, height: 74, background: SURFACE, border: `5px solid ${INK}`, borderRadius: 14, display: 'flex', alignItems: 'center', paddingLeft: 24, fontFamily: MONO, fontSize: 28, fontWeight: 800}}>{c.kept}</div>

      <div style={{position: 'absolute', left: 640, top: 270, fontFamily: MONO, fontSize: 20, fontWeight: 700, color: MUTED, letterSpacing: lang === 'en' ? 2 : 0}}>{c.ruleLabel}</div>
      <div style={{position: 'absolute', left: 640, top: 300, width: 496, height: 170, border: `6px solid ${INK}`, borderRadius: 18, padding: '22px 28px', fontFamily: MONO, fontWeight: 800}}>
        <div style={{height: 6, background: INK, borderRadius: 3, marginBottom: 22}} />
        <div style={{fontSize: 34, lineHeight: 1.35}}>{c.rule1}</div>
        <div style={{fontSize: 34, lineHeight: 1.35}}>{c.rule2}</div>
      </div>

      <div style={{position: 'absolute', left: 64, top: 548, fontSize: 24, fontWeight: 800, letterSpacing: lang === 'en' ? 1 : 0}}>{c.footer}</div>
    </AbsoluteFill>
  );
};
