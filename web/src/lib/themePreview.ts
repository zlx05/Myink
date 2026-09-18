import type { CustomThemeTokens, ThemeId } from './theme'

export const OFFICIAL_THEME_TOKENS: Record<Exclude<ThemeId, 'custom'>, CustomThemeTokens> = {
  paper: {
    canvas: '#f6f3e9',
    ink: '#3f3a36',
    editor: '#fffdf7',
    editorInk: '#514a43',
    accent: '#9fd8ad',
  },
  night: {
    canvas: '#121212',
    ink: '#e6e6e6',
    editor: '#181818',
    editorInk: '#e6e6e6',
    accent: '#7dba90',
  },
  contrast: {
    canvas: '#ffffff',
    ink: '#141414',
    editor: '#ffffff',
    editorInk: '#111111',
    accent: '#1f7a3a',
  },
}

export const WORKSPACE_PREVIEW_SIZE = { width: 1320, height: 540 }

// 外观预览钉死这一套：万古魔尊第一章。不读用户书库。
export const PINNED_WORKSPACE_PREVIEW = {
  brand: 'Myink',
  user: 'demo',
  book: '万古魔尊',
  books: ['长安夜行', '星舰远征', '都市医馆', '九州问天', '渊薮纪', '卷规划验证', '深渊天梯', '万古魔尊', '暗火'],
  bookLinks: ['设定', '创作设置', '全局审计'],
  chapters: [
    { seq: 1, words: '3,105', status: '已确认', active: true },
    { seq: 2, words: '2,920', status: '已确认', active: false },
  ],
  title: '第 1 章',
  badge: '已确认',
  version: 'v2',
  body: '那道裂痕弯得太规整了。\n\n沈砚的指尖停在灵脉修补井潮湿的石壁上，指腹下三寸处，一道细如发丝的裂隙正渗出淡淡的青灰色灵光。他做了七年的修补营生，经手的裂痕没有一千也有八百，地动造成的裂隙走向、灵脉自然衰竭的纹路，他闭着眼都能分辨。可眼前这条不一样。\n\n它弯。弯得匀称，弧线圆滑，像用圆规比着画出来的半圆，一路蜿蜒向深处。更要紧的是，当他把一缕灵力探进去探脉时，深处传来的脉动沉了一拍。\n\n不是天灾。',
}
