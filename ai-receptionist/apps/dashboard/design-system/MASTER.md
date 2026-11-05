# Design system: Receptionist dashboard (MASTER)

Global source of truth for the dashboard's look and feel. Page-specific
deviations go in `design-system/pages/<page>.md` (ui-ux-pro-max "Master +
Overrides" pattern). Implemented as CSS variables in `src/app/globals.css`.

## How it was derived

1. **ui-ux-pro-max** `--design-system` for "auto repair shop service
   scheduling operations dashboard", re-run with Apple-oriented keywords when
   the first match ("Organic Biophilic", a wellness style) didn't fit.
   Kept: the **Bento Box Grid** style (the skill's Apple-style match, rated
   WCAG AA / excellent performance) for the overview; the **data-dense
   operations** pattern for lists; status colours green / amber / red; the
   pre-delivery checklist.
2. **Apple Human Interface Guidelines** for everything the request named as
   "Apple design": the San Francisco system font, semantic label/fill/
   separator colours, inset grouped lists, segmented controls, large titles,
   capsule badges, filled / tinted / plain buttons, a translucent sidebar, and
   automatic light and dark appearance.
3. **dataviz** method for charts: one hue for single-series charts, thin
   rounded columns with a surface gap, hover + keyboard tooltips, a table view,
   and colours run through its validator in both modes.

Deliberate deviations from the skill output:
- Typography: the skill suggested Fira Code / Fira Sans. Apple design means the
  system font, so SF Pro is used on Apple devices with Geist as the fallback
  elsewhere (both neutral grotesques; no web-font download on Apple devices).
- Liquid Glass / Spatial UI were rejected for content: the skill itself flags
  them "moderate-poor performance, contrast risk". Translucency is used only
  for chrome (the sidebar), never behind text-heavy content.

## Colour tokens

Every text pairing below was measured (WCAG 2.x); every chart colour passed the
dataviz validator against its surface. Apple's stock system colours fail as
text on tinted chips in light mode (green 3.7:1, red 4.2:1), so status *text*
uses the nearest passing shade of the same hue; the chip *fill* keeps Apple's
hue at low opacity.

| Token | Light | Dark | Use |
|---|---|---|---|
| `--bg` | `#F5F5F7` | `#000000` | page background (grouped background) |
| `--surface` | `#FFFFFF` | `#1C1C1E` | grouped lists, tiles, panels |
| `--surface-2` | `#F5F5F7` | `#2C2C2E` | wells, inputs, nested rows |
| `--fill` | `rgba(120,120,128,.12)` | `rgba(118,118,128,.24)` | segmented control track, hover |
| `--separator` | `rgba(60,60,67,.18)` | `rgba(84,84,88,.6)` | hairlines between rows |
| `--label` | `#1D1D1F` (16.8:1) | `#F5F5F7` (15.6:1) | primary text |
| `--label-2` | `#6E6E73` (4.7:1 on page) | `#AEAEB2` (7.7:1) | secondary text |
| `--label-3` | `#8E8E93` | `#8E8E93` | icons, placeholders only, never body text |
| `--tint` | `#0071E3` | `#0071E3` | filled buttons (white text 4.7:1; Apple's dark `#0A84FF` is 3.65:1) |
| `--tint-text` | `#0067CF` | `#389BFF` | links, tinted buttons, selected text |
| `--chart` | `#0071E3` | `#0A84FF` | chart marks (validator: pass, both modes) |

Status (capsule badge: text on the hue at 12% / 20% over the surface; all >= 4.6:1):

| Status | Light text | Dark text | Hue (fill) |
|---|---|---|---|
| good | `#217E38` | `#30D158` | `#34C759` / `#30D158` |
| warning | `#A15E00` | `#FF9F0A` | `#FF9500` / `#FF9F0A` |
| critical | `#D30B00` | `#FF645B` | `#FF3B30` / `#FF453A` |
| info | `#0067CF` | `#389BFF` | `#0071E3` / `#0A84FF` |
| neutral | `#6C6C71` | `#9C9CA1` | `#8E8E93` |

Status colours are reserved for state and always ship with a text label.

## Typography

Stack: `-apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display",
"Helvetica Neue", Geist, system-ui, sans-serif`. Numbers use
`font-variant-numeric: tabular-nums` (SF's tabular figures) instead of a
monospace face.

| Role | Size / line | Weight |
|---|---|---|
| Large title (page) | 28/34 mobile, 34/41 desktop | 700, tracking -0.02em |
| Title 2 (section) | 20/25 | 600 |
| Headline | 15/20 | 600 |
| Body | 15/20 | 400 |
| Subheadline | 14/19 | 400 |
| Footnote / table header | 13/18 | 400, secondary |
| Caption | 12/16 | 500 |

## Shape, space, motion

- 8-point grid. Page gutters 16px mobile, 32px desktop. Max content 1200px.
- Radii: controls 8px, grouped lists 14px, bento tiles 20px, capsules full.
- Shadows: none on grouped lists (separation comes from `--bg` vs `--surface`);
  tiles get `0 1px 2px rgba(0,0,0,.04)` in light mode only.
- Motion: 200ms `cubic-bezier(.25,.1,.25,1)`; press = `scale(.98)`; all motion
  disabled under `prefers-reduced-motion`.
- Icons: Phosphor (SF Symbols-like line icons), 18px in lists, 20px in the
  sidebar, regular weight, fill weight for the selected item.

## Components

- **Sidebar** (macOS source list): translucent `--surface` at 72% with
  `backdrop-filter: blur(20px) saturate(180%)`; selected row = tint-filled pill.
- **Large-title header**: title + one-line description, actions on the right.
- **Inset grouped list / table**: `--surface`, 14px radius, hairline row
  separators inset from the leading edge, footnote-size uppercase headers.
- **Segmented control**: `--fill` track, selected segment = `--surface` with a
  subtle shadow; used for every filter (status, outcome, time window).
- **Buttons**: filled (tint, white text), tinted (tint at 12% + tint text),
  gray (fill + label), destructive (critical text). 32px tall, 8px radius.
- **Capsule badge**: status table above.
- **Bento tiles** (overview): asymmetric spans on a 4-column grid, 20px radius.
- **Day board** (test lab): one lane per bay, appointments as rounded blocks
  positioned by time; simulated bookings carry a hatch pattern + "Test" label so
  the distinction never depends on colour.

## Pre-delivery checklist (ui-ux-pro-max + dataviz)

Last verified against a production build in Chromium, light and dark, at 1440px
and 390px, on every page. No console or CSP errors and no horizontal page
scroll.

- [x] No emojis as icons; one icon family (Phosphor)
- [x] `cursor-pointer` + hover (150–300ms) on every clickable element
- [x] Visible focus ring on every control (keyboard)
- [x] Text contrast >= 4.5:1 in light AND dark (tokens above). The test-booking
      hatch uses *darker* stripes, so white text on it never drops below the
      tint's 4.7:1
- [x] `prefers-reduced-motion` respected
- [x] Responsive at 390 and 1440 with no horizontal page scroll. Wide tables
      and the day board scroll inside their own surface
- [x] Loading skeletons, empty states, inline errors on every data view
- [x] Charts: one hue per single series, legend for >= 2 series, tooltips also on
      keyboard focus, a table view, validated colours in both modes
