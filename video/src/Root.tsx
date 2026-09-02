import React from 'react';
import {Composition} from 'remotion';
import {HowItWorks, FPS, DURATION, WIDTH, HEIGHT} from './HowItWorks';

export const RemotionRoot: React.FC = () => {
  return (
    <>
      <Composition id="HowItWorks-en" component={HowItWorks} durationInFrames={DURATION} fps={FPS} width={WIDTH} height={HEIGHT} defaultProps={{lang: 'en'}} />
      <Composition id="HowItWorks-ko" component={HowItWorks} durationInFrames={DURATION} fps={FPS} width={WIDTH} height={HEIGHT} defaultProps={{lang: 'ko'}} />
      <Composition id="HowItWorks-ja" component={HowItWorks} durationInFrames={DURATION} fps={FPS} width={WIDTH} height={HEIGHT} defaultProps={{lang: 'ja'}} />
      <Composition id="HowItWorks-zh" component={HowItWorks} durationInFrames={DURATION} fps={FPS} width={WIDTH} height={HEIGHT} defaultProps={{lang: 'zh'}} />
    </>
  );
};
