// 4개 언어 × (mp4, gif) 렌더. GIF는 README/사이트용으로 960px·15fps.
import {execSync} from 'node:child_process';

const langs = process.argv.slice(2).length ? process.argv.slice(2) : ['en', 'ko', 'ja', 'zh'];
const stillsOnly = process.env.STILLS_ONLY === '1';
for (const lang of langs) {
  execSync(`npx remotion still src/index.ts SocialCard-${lang} out/social-card.${lang}.png --overwrite`, {stdio: 'inherit'});
  if (stillsOnly) continue;
  const id = `HowItWorks-${lang}`;
  execSync(`npx remotion render src/index.ts ${id} out/how-it-works.${lang}.mp4 --overwrite --codec=h264 --crf=20`, {stdio: 'inherit'});
  execSync(`npx remotion render src/index.ts ${id} out/how-it-works.${lang}.gif --overwrite --codec=gif --every-nth-frame=2 --scale=0.5`, {stdio: 'inherit'});
}
