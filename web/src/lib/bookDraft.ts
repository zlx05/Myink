// 建书设定草稿（§7.11）纯函数：LLM 草稿 dict → 分区编辑态 → SetupBody 落库。
// 抽成纯函数便于单测；键松类型兜底空默认，键漂移/缺失不崩（同 candidates.ts 口径）。
// realm_order 不独立落库：确认时折入 world_rules.realm_order（对齐 seed._ensure_demo_project
// 的 world_rules={"realm_order": [...]} 口径，设定浏览页统一从 world_rules 读）。
import type { SetupBody } from '../types'

export interface DraftFaction {
  name: string
  stance: string
  resources: string[]
}

export interface DraftCharacter {
  name: string
  role: string
  race: string
  origin: string
  realm_cap: string
  personality: string
}

export interface DraftLocation {
  name: string
}

/** 草稿分区编辑态（world_rules 只放「规则键值」，realm_order 独立存，落库时再折入） */
export interface SetupSection {
  realm_order: string[]
  world_rules: Record<string, string>
  hard_constraints: string[]
  forces: DraftFaction[]
  characters: DraftCharacter[]
  locations: DraftLocation[]
}

export function emptySection(): SetupSection {
  return {
    realm_order: [],
    world_rules: {},
    hard_constraints: [],
    forces: [],
    characters: [],
    locations: [],
  }
}

function strList(v: unknown): string[] {
  return Array.isArray(v) ? v.map((x) => String(x).trim()).filter(Boolean) : []
}

function strDict(v: unknown): Record<string, string> {
  if (!v || typeof v !== 'object' || Array.isArray(v)) return {}
  const out: Record<string, string> = {}
  for (const [k, val] of Object.entries(v)) {
    if (val !== null && val !== undefined && String(val).trim()) out[k] = String(val).trim()
  }
  return out
}

function strField(obj: Record<string, unknown>, key: string): string {
  const v = obj[key]
  return v === null || v === undefined ? '' : String(v).trim()
}

function strListField(obj: Record<string, unknown>, key: string): string[] {
  return strList(obj[key])
}

/** LLM 草稿 dict → 分区编辑态；缺失/类型不符分区回退空默认（§6.12 降级可手填） */
export function splitDraft(draft: Record<string, unknown>): SetupSection {
  const rules = draft.world_rules && typeof draft.world_rules === 'object' && !Array.isArray(draft.world_rules)
    ? draft.world_rules as Record<string, unknown> : {}
  const { realm_order: savedRealmOrder, ...worldRules } = rules
  const forcesRaw = Array.isArray(draft.forces) ? draft.forces : []
  const charactersRaw = Array.isArray(draft.characters) ? draft.characters : []
  const locationsRaw = Array.isArray(draft.locations) ? draft.locations : []
  return {
    realm_order: strList(draft.realm_order ?? savedRealmOrder),
    world_rules: strDict(worldRules),
    hard_constraints: strList(draft.hard_constraints),
    forces: forcesRaw
      .filter((f): f is Record<string, unknown> => !!f && typeof f === 'object')
      .map((f) => ({
        name: strField(f, 'name'),
        stance: strField(f, 'stance'),
        resources: strListField(f, 'resources'),
      }))
      .filter((f) => f.name),
    characters: charactersRaw
      .filter((c): c is Record<string, unknown> => !!c && typeof c === 'object')
      .map((c) => ({
        name: strField(c, 'name'),
        role: strField(c, 'role'),
        race: strField(c, 'race'),
        origin: strField(c, 'origin'),
        realm_cap: strField(c, 'realm_cap'),
        personality: strField(c, 'personality'),
      }))
      .filter((c) => c.name),
    locations: locationsRaw
      .filter((l): l is Record<string, unknown> => !!l && typeof l === 'object')
      .map((l) => ({ name: strField(l, 'name') }))
      .filter((l) => l.name),
  }
}

/** 全分区为空（空草稿 / 降级草稿判断：前端显示手填提示而非骨架） */
export function isSectionEmpty(s: SetupSection): boolean {
  return (
    s.realm_order.length === 0 &&
    Object.keys(s.world_rules).length === 0 &&
    s.hard_constraints.length === 0 &&
    s.forces.length === 0 &&
    s.characters.length === 0 &&
    s.locations.length === 0
  )
}

/** 编辑态 → 确认落库 SetupBody：realm_order 折入 world_rules；空行/空名过滤 */
export function sectionToBody(s: SetupSection): SetupBody {
  const world_rules: Record<string, unknown> = { ...s.world_rules }
  if (s.realm_order.length > 0) world_rules.realm_order = s.realm_order
  return {
    world_rules,
    hard_constraints: s.hard_constraints,
    characters: s.characters
      .filter((c) => c.name.trim())
      .map((c) => ({
        name: c.name.trim(),
        race: c.race.trim() || undefined,
        origin: c.origin.trim() || undefined,
        realm_cap: c.realm_cap.trim() || undefined,
        personality: c.personality.trim() || undefined,
      })),
    forces: s.forces
      .filter((f) => f.name.trim())
      .map((f) => ({
        name: f.name.trim(),
        stance: f.stance.trim() || undefined,
        resources: f.resources.map((r) => r.trim()).filter(Boolean),
      })),
    locations: s.locations.filter((l) => l.name.trim()).map((l) => ({ name: l.name.trim() })),
  }
}
