export type Lang = 'en' | 'ko' | 'ja' | 'zh';

export interface Copy {
  space: string;
  cut: string;
  one: string;
  rules: string;
  plug: string;
  x15: string;
  cmd: string;
  undo: string;
  tag: string;
  axes: string[];
  rules8: string[];
  tools: string[];
}

export const COPY: Record<Lang, Copy> = {
  en: {
    space: '6,561 ways your coding agent could behave',
    cut: 'Every X cuts the rest in half and drops the half you never want',
    one: 'The thinnest slice left is your agent',
    rules: 'Written down as 8 plain rules',
    plug: 'Plugged into the tool you actually use',
    x15: "15 X's · about 2 minutes",
    tag: 'X out the AI behavior you never want again.',
    cmd: 'uvx xout',
    undo: 'xout undo takes all of it back',
    axes: ['Autonomy', 'Commit style', 'Test discipline', 'Comments & docs', 'Errors', 'Scope', 'Verification', 'Dependencies'],
    rules8: ['Act first, then report.', 'Never commit unless asked.', 'Tests before done.', 'Comments only for why.', 'Stop on error, show the log.', 'Stay in scope.', 'Run the build before done.', 'Ask before adding a dependency.'],
    tools: ['Claude Code', 'Codex', 'OpenCode', 'Gemini CLI', 'Copilot', 'pi · oh-my-pi', 'Kiro', 'AGENTS.md'],
  },
  ko: {
    space: '코딩 에이전트가 할 수 있는 행동 6,561가지',
    cut: 'X 하나가 남은 것을 반으로 갈라 싫은 반을 버린다',
    one: '가장 얇게 남은 조각이 당신의 에이전트',
    rules: '규칙 8줄로 적힌다',
    plug: '실제로 쓰는 도구에 꽂힌다',
    x15: 'X 15번 · 약 2분',
    tag: '다시 보고 싶지 않은 AI 행동에 X를 치세요.',
    cmd: 'uvx xout',
    undo: 'xout undo 하나로 전부 되돌린다',
    axes: ['자율성', '커밋 방식', '테스트 규율', '주석과 문서', '에러 시 행동', '범위 준수', '완료 전 검증', '의존성'],
    rules8: ['먼저 실행하고 보고한다.', '요청 없이는 커밋하지 않는다.', '완료 전에 테스트를 돌린다.', '주석은 이유에만.', '에러 시 멈추고 로그를 보인다.', '범위를 지킨다.', '완료 전에 빌드를 돌린다.', '의존성은 묻고 추가한다.'],
    tools: ['Claude Code', 'Codex', 'OpenCode', 'Gemini CLI', 'Copilot', 'pi · oh-my-pi', 'Kiro', 'AGENTS.md'],
  },
  ja: {
    space: 'コーディングエージェントがとりうる 6,561 通りの振る舞い',
    cut: 'X をひとつつけるたびに残りが半分に割れ、要らない半分が消える',
    one: '最後に残ったいちばん薄いひと切れが、あなたのエージェント',
    rules: '8 本のルールとして書き出される',
    plug: '普段使っているツールに差し込まれる',
    x15: 'X 15回 · 約2分',
    tag: '二度と要らない AI の振る舞いを、X で消す。',
    cmd: 'uvx xout --lang ja',
    undo: 'xout undo 一発で全部元に戻る',
    axes: ['自律性', 'コミット方針', 'テスト規律', 'コメントとドキュメント', 'エラー時の行動', '範囲の遵守', '完了前の検証', '依存関係'],
    rules8: ['先に実行して、あとで報告する。', '頼まれない限りコミットしない。', '完了前にテストを回す。', 'コメントは理由だけ。', 'エラーが出たら止めてログを見せる。', '範囲を守る。', '完了前にビルドを回す。', '依存関係は聞いてから足す。'],
    tools: ['Claude Code', 'Codex', 'OpenCode', 'Gemini CLI', 'Copilot', 'pi · oh-my-pi', 'Kiro', 'AGENTS.md'],
  },
  zh: {
    space: '你的编码智能体有 6,561 种可能的做法',
    cut: '每个 X 都把剩下的对半切开，扔掉你不要的那一半',
    one: '最后剩下的那一薄片，就是你的智能体',
    rules: '写成 8 条大白话规则',
    plug: '接入你真正在用的工具',
    x15: '15 个 X · 约 2 分钟',
    tag: '把你再也不想看到的 AI 行为，一笔划掉。',
    cmd: 'uvx xout --lang zh',
    undo: '一句 xout undo 全部撤回',
    axes: ['自主性', '提交方式', '测试纪律', '注释与文档', '出错时的行为', '改动范围', '完成前验证', '依赖策略'],
    rules8: ['先做，再汇报。', '没人要求就不提交。', '完成前先跑测试。', '注释只写为什么。', '出错就停，亮出日志。', '不越界。', '完成前先跑构建。', '加依赖先问一声。'],
    tools: ['Claude Code', 'Codex', 'OpenCode', 'Gemini CLI', 'Copilot', 'pi · oh-my-pi', 'Kiro', 'AGENTS.md'],
  },
};
