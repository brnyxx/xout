import React from 'react';
import {Composition} from 'remotion';
import {HowItWorks, FPS, DURATION, WIDTH, HEIGHT} from './HowItWorks';
import {SocialCard} from './SocialCard';

export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition id="HowItWorks-en" component={HowItWorks} durationInFrames={DURATION} fps={FPS} width={WIDTH} height={HEIGHT} defaultProps={{lang: 'en'}} />
      <Composition id="HowItWorks-ko" component={HowItWorks} durationInFrames={DURATION} fps={FPS} width={WIDTH} height={HEIGHT} defaultProps={{lang: 'ko'}} />
      <Composition id="HowItWorks-ja" component={HowItWorks} durationInFrames={DURATION} fps={FPS} width={WIDTH} height={HEIGHT} defaultProps={{lang: 'ja'}} />
      {(['en', 'ko', 'ja', 'zh'] as const).map((lang) => (
        <Composition key={lang} id={`SocialCard-${lang}`} component={SocialCard} durationInFrames={1} fps={1} width={1200} height={630} defaultProps={{lang}} />
      ))}
      <Composition id="HowItWorks-zh" component={HowItWorks} durationInFrames={DURATION} fps={FPS} width={WIDTH} height={HEIGHT} defaultProps={{lang: 'zh'}} />
    </>
  );
};
