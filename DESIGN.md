# Myink Design System

## Design Read

Myink is a focused long-form fiction workbench for authors. The interface should feel precise, quiet, and dependable: a dark application shell for project navigation and system feedback, with a calm reading surface for writing. It is a product UI, not a marketing dashboard.

## Direction

- Product language: focused, editorial, technical, calm.
- Visual density: medium. Keep the writing surface dominant and tool chrome compact.
- Motion: restrained. Animate state changes and progress, never decorate empty space.
- Shape language: 4px for controls, 8px for panels, 12px only for large framed surfaces.
- Use one accent color consistently. Do not use gradients, glassmorphism, or decorative glow.
- Prefer separators, spacing, and surface contrast over floating card stacks.

## Color Tokens

```css
:root {
  --canvas: #0d0f11;
  --surface-1: #14171a;
  --surface-2: #1b1f23;
  --surface-3: #23282d;
  --line: #2b3137;
  --line-strong: #3a424a;
  --ink: #f1f3f4;
  --ink-muted: #c2c8ce;
  --ink-subtle: #8d969f;
  --ink-faint: #626b74;
  --accent: #8cc8b4;
  --accent-hover: #a7e4d0;
  --accent-pressed: #6fb39b;
  --focus: #a7e4d0;
  --success: #78c69b;
  --warning: #e1b66b;
  --error: #e48686;
  --editor: #f2f0eb;
  --editor-ink: #252623;
  --editor-muted: #747773;
}
```

The dark shell communicates system hierarchy. The editor surface is intentionally paper-like for sustained reading and writing. Keep the editor surface neutral and avoid adding a second accent family.

## Typography

- UI and headings: Geist, Inter, or the existing project sans-serif fallback.
- Long-form content: a readable system serif or the project's chosen Chinese serif fallback. Use it only inside the editor and preview surfaces.
- Display heading: 40px, weight 600, line-height 1.12, letter-spacing -1px.
- Page heading: 28px, weight 600, line-height 1.2.
- Panel heading: 18px, weight 600, line-height 1.35.
- Body UI: 14px, weight 400, line-height 1.5.
- Small metadata: 12px, weight 400, line-height 1.4.
- Editor body: 18px desktop, 16px mobile, line-height 1.8, no negative tracking.
- Do not use all-caps labels as decoration. Use them only for genuine system metadata.

## Layout

- Application shell: fixed left project rail, optional secondary navigation, flexible main workspace.
- Desktop content max width: 1280px. Main page gutters: 32px desktop, 20px tablet, 16px mobile.
- Base spacing unit: 4px. Common gaps: 8px, 12px, 16px, 24px, 32px, 48px.
- Keep the top bar between 52px and 60px high.
- The writing editor should occupy the largest uninterrupted region of the viewport.
- Use CSS Grid for page-level layouts. Avoid percentage-based flexbox math.
- On mobile, collapse navigation into a drawer and keep editor actions reachable with a compact bottom or top action bar.

## Core Surfaces

### Project Rail

The rail provides project switching and global navigation. It uses `--surface-1`, subtle dividers, 40px minimum interactive targets, and a single accent treatment for the active project or route. Do not turn every navigation item into a card.

### Chapter Workspace

The chapter view has three clear zones: chapter navigation, the writing surface, and contextual inspection. The editor wins visual priority. Contextual panels can show outline, recalled facts, character links, validation findings, and generation metadata.

### Writing Surface

Use `--editor` as the background and `--editor-ink` for text. Keep the column readable at roughly 680px to 760px. Headings, paragraphs, selection, cursor, autosave state, and inline review markers must have distinct states. Avoid placing dense controls over the text.

### Review and Audit

Findings are shown inline near the affected chapter content and summarized in a side panel. Severity uses text, icon, and color together. Never communicate a validation result by color alone.

### Generation Progress

Represent the workflow as a vertical event timeline: queued, running, waiting for review, completed, or failed. Each event can show node name, elapsed time, token/cost metadata, and a concise result. Use skeleton blocks for loading states and preserve layout dimensions.

## Components

- Primary button: `--accent` background, dark text, 4px radius, 40px minimum height.
- Secondary button: `--surface-2` background, `--ink` text, 1px `--line` border, 4px radius.
- Quiet action: transparent background, `--ink-muted` text, visible hover surface.
- Panel: `--surface-1` or `--surface-2`, 1px `--line` border, 8px radius.
- Input: `--surface-1`, 1px `--line-strong` border, 4px radius, clear `--focus` outline.
- Status badge: compact pill only for status or severity, never for ordinary labels.
- Tabs: text-first segmented control with one visible active indicator.
- Tables and timelines: use dividers and alignment; do not wrap every row in a separate card.
- Icon buttons: use the project's existing icon library, include an accessible label, and keep a stable 40px box.

## Interaction States

Every async workflow must support queued, loading, progress, success, empty, failure, retry, and interrupted/reconnect states. Buttons need hover, focus, pressed, disabled, and pending states. Preserve user text during failures and show errors next to the relevant action.

Use motion only for hierarchy, feedback, or state transition. Respect `prefers-reduced-motion`. Do not use continuous background animations.

## Product Screens

The first implementation should cover:

1. Project library and project switcher.
2. Work dashboard showing recent chapters, pending reviews, and generation status.
3. Chapter editor with outline, editor, and contextual inspection.
4. Generation progress timeline with reconnect and failure states.
5. Audit report with evidence, severity, and chapter navigation.
6. Characters, world rules, facts, relationships, and foreshadowing views.

## Quality Rules

- The primary action is obvious without relying on color alone.
- Text never overlaps, clips, or shifts the layout when state changes.
- All controls have keyboard focus states and mobile touch targets of at least 44px.
- Avoid generic three-card feature grids, excessive rounded containers, purple AI gradients, and decorative metrics.
- Use real workflow data in previews. Do not use placeholder names such as `Acme` or `Jane Doe`.
- Verify desktop and mobile layouts, loading states, error states, and long Chinese text before shipping.
