# Session Logbook — Design System

Philosophy: a Notion / Linear feel, light theme, readability over aesthetics, with CSS centralized through custom properties. **Read this doc before touching any UI.**

CLAUDE.md already inlines a quick reference for the core tokens; this doc is the detailed spec plus a library of anti-examples.

---

## 1. Color tokens (`:root`)

```css
--bg: #f7f7f5;          /* page background */
--bg-card: #ffffff;     /* card / modal */
--bg-hover: #f2f1ee;    /* row / button hover */
--border: #e4e4e0;
--border-light: #eeeee9;

--text-1: #1a1a1a;      /* heading / body */
--text-2: #5a5a57;      /* secondary text */
--text-3: #8c8c88;      /* tertiary / hint */
--text-4: #b0b0ab;      /* muted / placeholder */

--accent: #2f6feb;      /* blue — action / link / selected */
--gold:   #d97706;      /* amber-gold — star / heading emphasis */
--rust:   #dc2626;      /* red — danger / archive / delete */
--sage:   #059669;      /* green — git add / success (note: the conversation "user turn" bubble is a separate semantic, see §1.2) */

/* interaction-highlight tokens (defined in one place to avoid scattered alpha drift) */
--focus-ring:     rgba(47,111,235,.15);  /* 2px outer ring on input/cell focus (= --accent at reduced opacity) */
--mark-bg:        rgba(251,191,36,.30);  /* search-hit highlight (yellow) — shared by card snippets + conv-find */
--mark-active-bg: rgba(234,88,12,.85);   /* conv-find current hit (orange), more vivid than the snippet yellow */
```

**Before adding a new color**: first check whether one of the four existing semantic colors already covers the case. Only add a new one if none does, and update this doc once you do.
**Green is not a single meaning**: `--sage` is the "git add / success" state color; "what you said" (the user turn) in a conversation uses the same hue but a **different semantic** (see §1.2). Don't conflate the two, and don't borrow one for the other just because "they're both green."

### 1.1 Source identity colors (Claude warm / Codex cool)

The identity markers for a session's source form **their own palette**, deliberately steering clear of the three semantic colors `--accent` (action) / `--gold` (star) / `--rust` (destructive), so that "this is a source" is never misread as "this is an action / star / dangerous button."

```css
--source-claude-chip-bg/-fg   /* apricot-cream background + dark-gold text: card chip, modal pill, switcher badge fill */
--source-codex-chip-bg/-fg    /* slate-blue background + deep-blue text: same as above */
--source-antigravity-chip-bg/-fg /* violet background + deep-purple text: third source, Antigravity */
--source-*-glow-1/-2          /* the two-layer glow of the modal's rectangular light band (rgba) */
--source-*-solid              /* opaque version of the glow (claude #d9a550 / codex #6491af / antigravity #8b6fc4): color dots / badge outlines, more vivid than the chip */
```

Where it lands: the card `.source-chip`, the conversation modal's `.conv-source-pill` + glow, and the top-left `.source-filter` switcher.
**Iron rule**: a given source uses this same palette in every component — warm = Claude / cool = Codex / purple = Antigravity, never swapped. For a stronger statement use `-solid` (filled); don't reach for `--gold` / `--accent`.
**source-filter trigger**: the All state is **neutral chrome** (plain text, hover → `--text-1`, **does not flip to gold** — gold belongs to star, and on a source switcher it would clash with Claude's amber identity color and mislead). Once a specific source is selected, the whole trigger fills with that source's identity badge.

### 1.2 Conversation role colors (shared by card-preview `.msg-*` and conversation view `.conv-*`)

Conversation role identity uses a **single-channel encoding (after distilling)**: identity is carried by the **background tint** alone, while text labels and line numbers stay a neutral gray — color is no longer used redundantly. The hue is stored as an **rgb-channel token**, with alpha written at the point of use as `rgba(var(--x-rgb), a)`, so that "change the hue in one place and everything follows":

```css
--role-user-rgb: 5,150,105;        /* green (same hue as --sage) — what you said; background tint 0.10 */
--role-assistant-rgb: 47,111,235;  /* blue (same hue as --accent) — AI reply; background tint 0.03 (near-clean, so the reading surface stays a clean document) */
--role-qa-rgb: 139,92,246;         /* purple — Q&A / side turns; background tint 0.08 */
--role-skill-rgb: 245,158,11;      /* orange — skill injection / Q&A Other (external content shares the same orange family) */
--role-subagent-rgb: 20,184,166;   /* teal — subagent spawn */
--role-system-rgb: 107,114,128;    /* gray — system events */
/* labels/line numbers are uniformly neutral gray: the background tint already carries role identity, so labels don't repeat the color */
--role-qa-fg:       var(--text-2);
--role-skill-fg:    var(--text-2);
--role-subagent-fg: var(--text-2);
--role-system-fg:   #6b7280;        /* system events — gray */
```

**Iron rules**:
- **Single channel**: role identity is expressed via "background tint + neutral label." **Do not** color the label as well (this used to be a double encoding — qa purple / skill orange / subagent teal — and has since been collapsed). To add a new subtype → add another `--role-*-rgb` step to the background tint and reuse `--text-2` for the label.
- **No side bars**: a role block **must not** use a colored side bar (`border-left/right ≥ 2px`) as its identity marker (an absolute impeccable prohibition, plus it duplicates the background tint). There used to be 3px colored bars all over `.msg-*` / `.conv-*`; they've all been removed, leaving the background tint as the only identity. The one legitimate survivor is the neutral quote bar on blockquotes (`border-left:3px solid var(--border)` — a generic quoting convention, not a role color).
- **Mark only the exceptions, not the norm**: color is reserved for errors / anomalies (e.g. `.conv-tool-error` turns the tool name `--rust`); normal turns stay quiet. The user green shares a hue with `--sage` and the assistant blue shares a hue with `--accent`, but their semantic is "conversation role," not "success / action" — each gets its own token.

---

## 2. Hover semantics table (**most important**)

A button's hover color is **decided by the action's semantic**, not by "what looks nice." The table:

| Element action semantic | hover changes to | implemented examples |
|---|---|---|
| **Action / link** — copy, reveal, files, popout, link, navigate | `--accent` blue | `.sid-link-btn`, `.card-files-btn`, `.card-sid`, `.conv-link-btn`, `.conv-popout`, `.qq-cell-btn`, `.btn-files-inline`, `.file-action-btn` |
| **Star** | `--gold` amber-gold | `.btn-star` (the site title `.page-title` historically belonged here too, but it has since been replaced by `.source-filter`, see §1.1: the source switcher's All state uses neutral `--text-1` and does **not** flip to gold) |
| **Danger / Archive / Delete** — any **destructive / not-one-click-undoable** operation | `--rust` red | `.btn-danger`, `.group-archive-btn`, `.qq-cell-btn.danger` |
| **Neutral chrome** — close, toggle, refresh, collapse, scroll back to top | `--text-1` black | `.refresh-btn`, `.files-close`, `.files-toggle`, `.conv-close`, `.btn` default, `.qq-close`, `.recent-days button`, `.section-header` |

**Iron rules**:
- The same action semantic appearing in different places → its hover must use the same color. Example: every "copy / reveal / files / link" button hovers to `--accent`.
- Don't grab gold/rust at random just to "make a button pop." Gold is star-only, rust is destructive-only. Misusing them makes readers think the button will star / delete something.
- Neutral chrome should not hover to accent blue — blue implies "actionable," but chrome buttons aren't primary actions.

---

## 3. Typography

```
font-body:  self-hosted Inter / system-ui / PingFang SC / Noto Sans SC          ← body
font-mono:  self-hosted JetBrains Mono / Fira Code / SF Mono / Menlo / PingFang SC / Noto Sans SC  ← details / code
```

**No remote fonts**: the dashboard must not load Google Fonts or other external font resources. Inter and JetBrains Mono are served from `vendor/fonts/`; system fonts are fallback only for unsupported glyphs or unexpected asset failures.

**CJK fallback must be unified to Simplified**: this product is in Simplified Chinese, so the CJK fallback is always `PingFang SC` (native on Mac) → `Noto Sans SC` (cross-platform). **Do not use `Noto Sans TC` (Traditional)** — it would render shared code points with Traditional glyph shapes. `--font-mono` carries the same CJK fallback set so that Chinese renders as the same font whether in a sans or mono context (mono fonts have no CJK glyphs of their own; without a fallback, Chinese inside mono would each fall to a system default and look inconsistent with the body).

**When to use mono**:
- session id, jsonl path, size, time (numeric-alignment semantic)
- the "action verb" in a button label (the `.btn` system: mono / uppercase / letter-spacing 0.1em / 10px)
- code block / inline code / git status letters
- **chrome controls / labels**: the top-bar filter trigger (`.source-filter-trigger` "All Sessions"), toggle labels (`.oneshot-toggle` "Hide 1-round"), count, source badges (`.source-chip`), caret, group header, recent-days chip — **chrome speaks mono, uniformly**. Note: using `inherit` / not declaring a font inherits the body's sans, which is the wrong font for a chrome element; you must explicitly set `var(--font-mono)` (judge by the **actual rendered** font, not by whether a token was used — many elements inherit from a mono parent and are already correct).

**When to use sans**:
- headings, body text, filename / path text (filenames use `.file-rel` = `var(--font-body)` sans, same font as body; this used to hardcode a separate system font stack and has been pulled back into the token)
- conversation-view body

**Font-size scale (integer steps, already consolidated — don't reintroduce .5 or stray values like 15/17)**:

`9 · 10 · 11 · 12 · 13 · 14 · 15 · 16 · 18` (px). It once sprawled across 18 values (9.5/10.5/11.5/12.5/13.5/14.5/15/15.5/16/17…), and the half-pixel jitter produced a "muddy hierarchy"; all of it has been snapped to this integer set.

| Step | Role |
|---|---|
| 9 | tiny chip / label (`.source-chip`, `.conv-qa-other-tag`) |
| 10 | detail mono (id, size, time, `.btn` label, `.conv-ts`) |
| 11 | small UI / secondary mono (count, search-match, inline code, fold-hint) |
| 12 | group header, recent-days, QA text, empty-state hint, conv table |
| 13 | tight-leading list body (`.msgs`, `.conv-text`, conv h3) |
| 14 | body (`html,body`), card title (weight 600), conv markdown h2 |
| 15 | standalone body (reading mode +2), source-filter trigger |
| 16 | conv markdown h1, standalone h2, modal close `✕` |
| 18 | largest heading (standalone h1) |

Exception: `.group-dots` at 7px is a decorative dot (not text), so it stays.
**Iron rule**: all new text picks a step from the table above. To add a new step, first prove the existing 9 steps can't cover it — and never use .5.

**Hover hints**: icon-only / short-label action buttons use a hand-drawn `data-tooltip` (instant); multi-sentence help text (e.g. the long descriptions for export/brief/compact) keeps the native `title` (which wraps automatically and positions intelligently, whereas `data-tooltip` is nowrap and would overflow). conv-head sits flush against the top of the modal, so its `data-tooltip` is uniformly flipped to below (`.conv-head [data-tooltip]::after { top: calc(100% + 6px) }`).

---

## 4. Spacing (no tokens, but there is a convention)

Spacing values come from a limited scale — don't just type a number: **2 / 4 / 6 / 8 / 10 / 12 / 16 / 20 / 24 / 28** (plus large whitespace 40 / 48 / 60 / 64). One-off deviations (5/7/9/11/18…) should snap back to the nearest step — unless a comment states it's "deliberately tuned for alignment" (e.g. the icon midline in §6.1, a negative margin offset, or asymmetric padding that reserves room for a caret), in which case leave it alone.

- card padding: `14px 16px 12px` (top / sides / bottom, the bottom slightly tighter so the foot hugs the edge); right-column (repo) cards tighten vertically to `11px…9px` (register differentiation)
- row padding: `6px 20px` (`.file-row`, card-embedded rows)
- card spacing: `margin-bottom: 8px`
- **modal header horizontal padding is uniformly 24** (`.conv-head` / `.files-head` / `.qq-head` all the same; qq's entire head/body/foot is internally consistent at 24 horizontal) — same-role modal headers must share the same geometry (§5.5)
- button inner padding:
  - large mono-label button (`.btn`): `8px 12px`
  - compact mono-label button (inline hover overlay): `3px 6px`
  - icon-only square button: container ≥ icon + 8px

---

## 5. Component patterns

### 5.1 Card top-right / right-side icon buttons (hover affordance)

- only appear on card / row hover, transitioning opacity 0 → 1
- absolute positioning so they don't affect the main row's layout; don't give them dedicated flex space
- the icon midline must **align** with the other icon columns in the same card / row (see §6)
- icon `stroke="currentColor"`, so it follows `color` and recolors automatically
- no background / border / box-shadow — a simple ghost; over-decoration = redundant visual hierarchy

```css
/* good: ghost button, only changes color */
.card-files-btn:hover { color: var(--accent); }
.file-action-btn:hover { color: var(--accent); }

/* bad: row hover is already a gray background, so a chip on the button = redundant layer */
.bad-btn:hover {
  background: var(--bg-card);
  box-shadow: 0 0 0 1px var(--border-light);  /* ✗ */
}
```

### 5.2 On row hover, cover the right-side stats (without squeezing row width)

How `.file-actions` does it: absolute against the row's right edge, with a linear-gradient softly fading into the row's hover bg.

Don't stuff buttons into the end of the row and crowd out the filename column.

### 5.3 group header actions

- the group header is mono 12px overall → its buttons follow the same font and size, in a **plain text-link style**
- don't add a border to labels like `Files` / `Archive all` — group headers don't need chip-ification
- hover recolors per table §2 (Files → accent, Archive all → rust)

### 5.4 The `.btn` system (card actions / conv-head actions)

mono / uppercase / letter-spacing `0.1em` / font-size `10px` / font-weight `500`, icon + 6px gap + label.
Just change color on hover (except the star / danger subclasses).

### 5.5 Same component, consistent across surfaces (**the lens to check when changing UI / auditing**)

When the same **logical component** appears across different surfaces (the card, the conversation modal, standalone, the Files modal), its **presentation + interaction must be identical** — no writing each one its own way. This is the heart of "consistency" and the easiest thing to miss, because it's neither a color problem nor a single-point behavior problem, but a structural fork — "the same thing looks/behaves differently in two places" — that slips through the cracks of an audit that only checks colors or only checks contracts.

**Before changing any component that appears in more than one place**: pull out every version of it across all surfaces and diff them side by side. The known list of logical components:
- session id + copy/path/share (card `.card-sid` / `.sid-link-btn` ↔ conv `.conv-copy-id` / `.conv-link-btn`): unified to "**click the id text itself to copy + a borderless ghost icon**," not a bordered "copy" pill (this used to fork in conv-detail, violating §7's no-border rule)
- source identity (card `.source-chip` abbreviation ↔ conv `.conv-source-pill` full name): content may differ by available space (abbreviation/full name), but the color usage must be the same `--source-*` set, and the geometry follows a unified scale
- star / archive (card icon-only ↔ conv icon+label): icon-only vs labeled is a reasonable narrow/wide adaptation, but the **hover-hint mechanism must match** (short labels always use `data-tooltip` for instant display, see §3)
- close `✕`, copy/share/open and other generic actions: the character, the icon, and the hover color are identical product-wide

**Real fork vs reasonable adaptation**: trimming content to fit the space (abbreviation/full name, icon-only/with-label) is reasonable; a difference in presentation style (bordered/borderless, pill/ghost), interaction method (click text/click button), or hint mechanism (instant/delayed) = a real fork, to be collapsed back to the product's mainstream language.

### 5.6 State / feedback / wayfinding patterns (make core semantics scannable while at rest)

The cockpit principle: make core semantics like "time decay / source identity / the dashboard being alive" **scannable by peripheral vision without adding noise** — not reliant on hover, not relying on docs as a backstop. Patterns already in place (reuse them, don't invent your own):

- **Source wayfinding (A1)**: list cards are fully neutral at rest, and **the source identity color glows through only on hover** — `.card[data-source=x]:hover` uses `--source-*-glow-2` (extremely faint) for a box-shadow plus a `-solid` outline. Card rendering must carry `data-source`. Don't give cards a persistent source color (it turns into a Christmas tree).
- **Freshness (A2)**: mtime within the last 1h → `.time-tag.fresh` (bold; if there's no stop_reason color, it also turns `--text-1`). No new color is added, and it doesn't fight the stop_reason palette.
- **Time-decay threshold made explicit (A2)**: the Dusty section title carries `.section-title-note` showing the currently-effective `Nd+`, linked live to recentDays. It drags the bet hidden in the docs out onto the surface.
- **Live heartbeat (B1)**: the top-bar `.live-dot` — normally an extremely faint sage static dot (= alive), and when data actually changes it adds `.beat` to pulse once; `markLive()` updates the "checked HH:MM:SS" tooltip. Manual ↻ adds `.spinning` for one rotation.
- **Background updates made perceptible (B2)**: live polling only flashes cards that are **genuinely new / had their mtime move forward** (`flashLiveCards`, reusing `card-flash`, ≤10 of them, and **not** scrollIntoView — it doesn't hijack the user's viewport). Don't flash the whole page.
- **Persistent state markers (D2)**: a card with a note → the foot carries a persistent `.card-has-note` micro-marker (shown in the collapsed state too), so you don't need hover to see which cards were annotated.
- **Role scan anchor column (D1)**: the assistant turn's `.msg-role` uses an aligned, extremely faint blue dot (`::before`) to restore the constant left-edge anchor column, recovering the you→AI scan rhythm (it used to be an empty span).

### 5.7 Feedback language (restrained toasts + a way out + recoverable)

- **Undo over confirm**: destructive / not-one-click-undoable operations (archive, Archive all) → `toast(msg, {action:{label:'Undo', fn}})`. A toast with an action is clickable and stays for 5s. The rollback uses the already-stored `oldScope` / snapshot, at zero cost.
- **An empty state is not a dead end (C3)**: each empty state is typed and offers a way out — `.empty-title` + `.empty-hint` + `.empty-action` (a dashed ghost button, hover → accent). Empty source filter → "Show all sources"; empty search → echo the query back + "Clear search"; zero sessions → plain language pointing at the real workflow; empty Starred → an invitation rather than "— nothing —".
- **Loading uses a skeleton, not a blank screen (C3)**: heavy operations (opening a conversation, Files) use `.skeleton-line` (shimmer) to pre-occupy the layout — it reads as "working" better than a spinner and reduces CLS.
- **Errors are retryable (C4)**: read-only operation failures uniformly offer an `.empty-action` / link-style Retry (re-entering the original fetch function) plus an explanation; a list failure spells out "the dashboard will reconnect automatically." Don't let a read-only failure throw you into a dead end.
- All animations are backstopped by the global `* { animation:none; transition:none }` under `@media (prefers-reduced-motion: reduce)`.

---

## 6. Alignment (**a common pitfall**)

### 6.1 Icon-column midline alignment

When multiple rows of icon buttons appear in the same card / same modal (e.g. the folder at the card's top-right and the pencil/star/archive in the card-foot), the **icon midlines must be strictly aligned** — not the container's right edges.

The math:
- icon midline = container right edge − container right padding − half the icon width
- when tuning an absolute element's `right`, work backwards: to put the icon midline at X → `right = card_right − X − container_width/2`

Anti-example (`.card-files-btn` before the fix): `right: 10px`, container 22px, icon 14px. Its midline sits 21px from the card's right.
The corresponding `.card-actions .btn` has inner padding 10px and icon 12px, so the rightmost btn's icon midline sits 32px from the card's right. An 11px difference → visibly misaligned.

### 6.2 Right-inner-padding alignment

The row-end padding should match the card's padding system — don't just type 4 / 6 / 10 / 16 / 20 at random. This project really has only two steps:
- card / modal container: right padding 16px
- row / embedded list item: right padding 20px

---

## 7. Anti-patterns (pits we've already stepped in)

- ❌ **Adding a white background + ring to a button that's already on a gray row-hover background** — redundant hierarchy. The row hover is already `--bg-hover`, so the button only needs to change color to stand out.
- ❌ **An icon-only button with no text and no tooltip** — on hover you can't tell what it does. Either add a mono label or a `data-tooltip="..."` (the hand-drawn tooltip system, see the `[data-tooltip]` implementation in `index.html`).
- ❌ **Same semantic, different color** — "copy session id" hovers accent while "copy jsonl path" hovers gold. Both are copy; the color must be consistent.
- ❌ **Creating a new color variable** — the four existing semantic colors (accent / gold / rust / sage) + four gray steps cover 95% of cases. Before adding one, first prove the existing colors can't cover it.
- ❌ **Using gold/rust as decoration** — these two are occupied by semantics (star / destructive). Even neutral chrome that wants to feel "a bit warmer" can't use gold.
- ❌ **Piling on decoration with chip-ification + ring + gradient + bg** — under the light + readability-over-aesthetics principle, less is more.
- ❌ **A colored side bar as an identity marker** — a colored bar with `border-left/right ≥ 2px` (role blocks, callouts, list items) is an absolute impeccable prohibition and duplicates the background-tint encoding. Identity is expressed single-channel via "background tint + neutral label" (see §1.2). The only legitimate exception is the neutral quote bar on blockquotes.
- ❌ **Using a unicode character as an interactive button icon** — `×`/`✕` (close), `▾`/`▸` (dropdown/collapse), `↑`/`↓` (navigation) and the like, used as button icons, will: ① fall back to a system font (in testing, `✕` → Arial, which matches neither the site's mono nor sans); ② be impossible to control for stroke-width / size / centering (the root cause of "the icon looks crooked"). **Button icons always use the feather SVGs in `ICONS`** (`viewBox="0 0 24 24"` `stroke="currentColor"` `stroke-width="2"`), with size set via CSS `svg{width/height}`, the container as `inline-flex` to center, and direction via `transform: rotate()`. The caret reuses `ICONS.chevron` (right, rotated 90° when the collapse family is open) / `ICONS.chevronDown` (down, rotated 180° when the dropdown family is open). Unicode is reserved only for genuine typographic characters (`·` separator, `⌘` keycap, arrows within body text).
- ❌ **Coloring the norm too** — roles/states should be quiet by default; only **exceptions** get color (errors turn `--rust`, freshness goes bold). Dyeing every kind of turn a different color = a Christmas tree, which actually makes scanning more tiring (the distilled "restrained quieting").
- ❌ **Shipping without verifying icon visual alignment** — after a UI change, start the server + screenshot the hover in chrome-devtools; don't infer alignment by reading the CSS in the source.

---

## 8. UI-change workflow

1. **Before changing**: grep `:hover` to see what color same-semantic buttons already use, and follow it.
2. **Writing**: use the existing `var(--xxx)` tokens, don't write hex.
3. **Verifying**: start the server + chrome-devtools `hover` + `take_screenshot`, and eyeball the real capture.
4. **Recording**: if you added a component pattern / discovered a new anti-pattern → write it back into this doc.

If you can't decide a new component's hover color → go back to the §2 table and look up the action semantic. If the semantic isn't in the table → align with the user on which step to add, then update this doc + CLAUDE.md.
