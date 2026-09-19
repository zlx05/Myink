// 建书设定草稿纯函数单测：分区解析兜底 / 空态判断 / 确认落库 SetupBody（realm_order 折入）。
import { describe, expect, it } from 'vitest'
import { emptySection, isSectionEmpty, sectionToBody, splitDraft } from './bookDraft'

const DRAFT = {
  realm_order: ['炼气', '筑基', '金丹'],
  world_rules: { 灵气: '天地灵气充盈', 修炼: '突破需心境圆满' },
  hard_constraints: ['金丹期不得无敌', '凡人不可御剑飞行'],
  forces: [{ name: '青云宗', stance: '正道之首', resources: ['护山大阵'] }],
  characters: [{ name: '林砚', role: '主角', race: '人族', origin: '青云镇', realm_cap: '金丹', personality: '坚忍' }],
  locations: [{ name: '青云山' }],
}

describe('splitDraft', () => {
  it('restores confirmed realm order without converting it to a comma-separated rule', () => {
    const confirmed = sectionToBody(splitDraft(DRAFT))
    const restored = splitDraft(confirmed as unknown as Record<string, unknown>)
    expect(restored.realm_order).toEqual(DRAFT.realm_order)
    expect(restored.world_rules).toEqual(DRAFT.world_rules)
    expect(sectionToBody(restored)).toEqual(confirmed)
  })
  it('合法草稿各分区完整解析', () => {
    const s = splitDraft(DRAFT)
    expect(s.realm_order).toEqual(['炼气', '筑基', '金丹'])
    expect(s.world_rules).toEqual({ 灵气: '天地灵气充盈', 修炼: '突破需心境圆满' })
    expect(s.hard_constraints).toEqual(['金丹期不得无敌', '凡人不可御剑飞行'])
    expect(s.forces).toEqual([{ name: '青云宗', stance: '正道之首', resources: ['护山大阵'] }])
    expect(s.characters).toEqual([
      { name: '林砚', role: '主角', race: '人族', origin: '青云镇', realm_cap: '金丹', personality: '坚忍' },
    ])
    expect(s.locations).toEqual([{ name: '青云山' }])
  })

  it('缺失/类型不符分区回退空默认，不崩', () => {
    const s = splitDraft({})
    expect(s).toEqual(emptySection())
    const s2 = splitDraft({ world_rules: 'not-a-dict', forces: 'x', realm_order: '炼气' })
    expect(s2.world_rules).toEqual({})
    expect(s2.forces).toEqual([])
    expect(s2.realm_order).toEqual([])
  })

  it('空名角色/势力/地点被过滤', () => {
    const s = splitDraft({
      characters: [{ name: '', race: '人' }, { name: '苏晚' }],
      forces: [{ name: ' ' }],
      locations: [{ name: '' }],
    })
    expect(s.characters).toEqual([{ name: '苏晚', role: '', race: '', origin: '', realm_cap: '', personality: '' }])
    expect(s.forces).toEqual([])
    expect(s.locations).toEqual([])
  })
})

describe('isSectionEmpty', () => {
  it('空分区判定（降级草稿 → 手填提示）', () => {
    expect(isSectionEmpty(emptySection())).toBe(true)
    expect(isSectionEmpty({ ...emptySection(), realm_order: ['炼气'] })).toBe(false)
    expect(isSectionEmpty({ ...emptySection(), hard_constraints: ['x'] })).toBe(false)
  })
})

describe('sectionToBody', () => {
  it('realm_order 折入 world_rules，其余字段透传清洗', () => {
    const body = sectionToBody(splitDraft(DRAFT))
    expect(body.world_rules.realm_order).toEqual(['炼气', '筑基', '金丹'])
    expect(body.world_rules['灵气']).toBe('天地灵气充盈')
    expect(body.hard_constraints).toEqual(['金丹期不得无敌', '凡人不可御剑飞行'])
    expect(body.characters).toEqual([
      { name: '林砚', race: '人族', origin: '青云镇', realm_cap: '金丹', personality: '坚忍' },
    ])
    expect(body.forces).toEqual([{ name: '青云宗', stance: '正道之首', resources: ['护山大阵'] }])
    expect(body.locations).toEqual([{ name: '青云山' }])
  })

  it('无 realm_order 时不注入空列表键', () => {
    const body = sectionToBody(emptySection())
    expect('realm_order' in body.world_rules).toBe(false)
    expect(body.characters).toEqual([])
  })
})
