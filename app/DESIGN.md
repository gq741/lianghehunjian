---
name: 量禾混剪
description: 留白编排 — compact native-Chinese video-workbench presentation, extracted from the approved implementation.
colors:
  primary: '#966d20'
  primary-hover: '#805c18'
  primary-active: '#6e4f15'
  primary-soft: '#f2eddf'
  canvas: '#fafbf8'
  surface: '#ffffff'
  ink: '#242522'
  muted: '#686a65'
  line: '#e2e3dc'
  line-strong: '#bfc3b7'
  error: '#a13f33'
  error-soft: '#fbefec'
  warning: '#855909'
  warning-soft: '#fbf3e3'
  success: '#426246'
  success-soft: '#edf3eb'
typography:
  headline:
    fontFamily: '"Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", system-ui, sans-serif'
    fontSize: 26px
    fontWeight: 650
    lineHeight: 1.4
    letterSpacing: -0.025em
  product-title:
    fontFamily: '"Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", system-ui, sans-serif'
    fontSize: 22px
    fontWeight: 650
    lineHeight: 1.5
    letterSpacing: normal
  title:
    fontFamily: '"Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", system-ui, sans-serif'
    fontSize: 17px
    fontWeight: 650
    lineHeight: 1.65
    letterSpacing: normal
  body:
    fontFamily: '"Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", system-ui, sans-serif'
    fontSize: 13px
    fontWeight: 400
    lineHeight: 1.65
    letterSpacing: normal
  control:
    fontFamily: '"Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", system-ui, sans-serif'
    fontSize: 12px
    fontWeight: 550
    lineHeight: 1.65
    letterSpacing: normal
  label:
    fontFamily: '"Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", system-ui, sans-serif'
    fontSize: 12px
    fontWeight: 600
    lineHeight: 1.65
    letterSpacing: normal
  metadata:
    fontFamily: '"Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", system-ui, sans-serif'
    fontSize: 11px
    fontWeight: 400
    lineHeight: 1.65
    letterSpacing: normal
  editor:
    fontFamily: '"Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", system-ui, sans-serif'
    fontSize: 15px
    fontWeight: 400
    lineHeight: 2
    letterSpacing: normal
rounded:
  badge: 3px
  tag: 4px
  control: 5px
  card: 7px
  panel: 8px
  dialog: 10px
spacing:
  micro: 4px
  tight: 8px
  compact: 12px
  group: 16px
  panel: 20px
  section: 24px
  gutter: 30px
  wide-gutter: 34px
components:
  button-primary:
    backgroundColor: '{colors.primary}'
    textColor: '{colors.surface}'
    typography: '{typography.control}'
    rounded: '{rounded.control}'
    padding: 7px 13px
    height: 36px
  button-primary-hover:
    backgroundColor: '{colors.primary-hover}'
  button-primary-active:
    backgroundColor: '{colors.primary-active}'
  button-secondary:
    backgroundColor: '{colors.surface}'
    textColor: '{colors.ink}'
    typography: '{typography.control}'
    rounded: '{rounded.control}'
    padding: 7px 13px
    height: 36px
  button-quiet:
    backgroundColor: transparent
    textColor: '{colors.muted}'
    typography: '{typography.control}'
    rounded: '{rounded.control}'
    padding: 7px 13px
    height: 36px
  input:
    backgroundColor: '{colors.surface}'
    textColor: '{colors.ink}'
    rounded: '{rounded.control}'
    padding: 8px 11px
  navigation-active:
    backgroundColor: '{colors.primary-soft}'
    textColor: '{colors.primary-hover}'
    rounded: '{rounded.control}'
    padding: 10px 13px
    height: 43px
  status-error:
    backgroundColor: '{colors.error-soft}'
    textColor: '{colors.error}'
    typography: '{typography.metadata}'
    rounded: '{rounded.tag}'
    padding: 1px 5px
  material-card:
    backgroundColor: '{colors.surface}'
    textColor: '{colors.ink}'
    rounded: '{rounded.card}'
  task-detail:
    textColor: '{colors.error}'
    typography: '{typography.metadata}'
    padding: '0'
---

# Design System: 量禾混剪

## Overview

**Creative North Star: "留白编排"**

留白编排 is a quiet working surface: a narrow white rail, an off-white canvas, charcoal Chinese UI type and restrained warm-gold actions. Source covers carry the visual attention; the interface supplies compact controls, thin separators and a clear next step rather than a promotional composition.

This scan-mode specification records the emitted CSS, including the approved-composition overrides after the older component rules. It follows the user-selected code-led model-pick sketch and direction contract, not a generated raster comp. The supplied wheat/infinity logo remains the exact original image; the brand is not redrawn or substituted. Native Chinese UI sans is an intentional Operate exception to expressive display typography.

**Key Characteristics:**
- White narrow navigation and open horizontal tooling.
- Moderate landscape covers with compact metadata.
- Warm gold for deliberate action and active state; semantic colors for outcomes.
- Flat working layers, contained media and native stateful controls.

## Colors

The palette pairs warm, subdued gold with neutral working layers. Frontmatter values are normative; the names below explain their jobs.

### Primary
- **Harvest gold** (`primary`): primary action fill, focus/caret and active accents.
- **Deep gold** (`primary-hover`, `primary-active`): hover/pressed feedback; deep gold also labels active navigation and source tabs.
- **Pale grain** (`primary-soft`): active navigation, running-task and workflow-step ground.

### Neutral
- **Quiet canvas** (`canvas`) and **clean surface** (`surface`): workspace versus white navigation, fields and working panels.
- **Charcoal ink** (`ink`) and **stone text** (`muted`): primary hierarchy versus supporting text. Muted copy remains legible on both canvas and white.
- **Fine divider** (`line`) and **field edge** (`line-strong`): structural separation versus actionable field/control boundaries.

### Semantic
- **Clay error** (`error`, `error-soft`): failed/interrupted jobs, alerts and destructive text.
- **Ochre warning** (`warning`, `warning-soft`): missing environment, shortages and adjustment notices.
- **Leaf success** (`success`, `success-soft`): completed jobs, configured status and ready timeline outcomes. Some existing ready environment chips retain the primary accent; this is not a semantic rewrite.

**The Restrained Gold Rule.** Gold identifies primary actions and active context; failed, warning and completed outcomes retain their distinct semantic families.

Principal source-token contrast checks are white on primary (4.66:1), ink on canvas (14.84:1) and muted on canvas (5.27:1). These are targeted token checks, not an exhaustive accessibility certification.

## Typography

**Body and heading font:** the local Chinese UI sans stack in the frontmatter. No downloaded font, decorative display face or forced mono face is part of this implementation. Numerals in number fields, durations, counts and timeline tables use tabular treatment where the source specifies it.

- **Headline:** page titles; the narrow layout uses the existing smaller heading treatment (23px).
- **Product title:** the existing product-information heading retains its separate contract.
- **Title:** section hierarchy; settings, preflight and preview headings retain their documented compact local overrides rather than forcing every heading to one size.
- **Body:** default application copy; field hints and controls use the denser label/control scale.
- **Control / label / metadata:** compact actions and descriptive/supporting information. Library filenames use the existing 12px desktop / 11px narrow scale.
- **Editor:** whole-voiceover editing (15px desktop, 14px narrow); the voiceover remains the readable focal text in its pane.

**The Native Legibility Rule.** Use the shipped local Chinese UI stack; keep functional text at or above the implemented 11px floor and preserve the established heading hierarchy.

## Layout

The application is desktop-first, with the user's primary working display at 1920px or larger. The fixed white navigation rail is 184px on wide windows; main content starts beside it. A slim topbar (56px) provides context. Pages use a fluid working width capped at 1920px; the settings page has its own narrower cap (1280px). Normal horizontal gutters are 30px, increasing to 34px from the 1700px breakpoint.

The library uses open, full-width tooling: a title and source tabs above divider-led import controls, then selection/filter actions and a responsive material grid. Its default auto-fill minimum is 196px, changing to 215px on wide windows. The reviewed library shows seven columns at 1920px, five at 1380px and two at 390px. Covers are 16:10 on desktop and 4:3 in the narrow layout. A sticky bordered selection bar remains near the bottom without an elevation shadow.

The three existing workbench steps remain. At 1300px and wider, product and video setup occupy paired columns, while preflight, music, draft workspace and tasks span the row. The editor/preview pair uses a flexible editor with a 306px preview column; at 1700px it expands to a 340px preview column. Below 900px the workspace stacks. Actual video playback uses contain rather than cover; its frame changes with available space without cropping the video.

Responsive layers retain the shipped cascade: at 1150px the rail is 182px and work areas tighten; at 900px the rail becomes 78px and settings/workbench stack; at 650px it becomes a horizontal 62px navigation bar, main content takes full width, page gutters become 16px and the material grid becomes two columns. Native forms wrap, settings fields become one column and lower working content remains scrollable. The reviewed library/workbench/settings set at 1920×1080, 1380×900 and 390×844 records no horizontal page overflow.

**The Covers First Rule.** Keep source covers moderate and landscape, controls compact and working space available; enlarge the number of visible materials rather than turning covers into tall posters.

## Elevation & Depth

Ordinary surfaces are flat: the canvas/surface contrast, fine borders and measured spacing do the separating. The selection bar has no shadow or backdrop blur in the final cascade. Dialogs have a single soft offset shadow and an unblurred translucent dark backdrop; toasts retain the existing transient-feedback shadow. Empty-state and dialog icons are unboxed, without a colored tile.

**The Quiet Depth Rule.** Separate ordinary working surfaces with tone, spacing and fine borders; reserve pronounced elevation for dialogs and transient feedback.

Motion is limited to short color/border/opacity feedback (150–160ms). The source's reduced-motion media query removes transitions and forces automatic scroll behavior. There is no page-load choreography, scroll pinning or marketing animation.

## Shapes

Controls use gently cut, small corners; material cards and working panels use slightly softer corners, while dialogs are the softest containers. The rounded scale is extracted in the frontmatter rather than normalized to a new library. Divider-led library import and task disclosure surfaces are square/open. Small status chips remain modest rectangles, not pill badges. A source card's selected state changes its edge without adding a halo or shadow. Native checkboxes retain their square affordance and gold accent; the supplied logo image remains an image, not a CSS-drawn emblem.

## Components

### Buttons

Primary buttons pair gold fill with white text. Secondary buttons are white with a field-edge border; quiet buttons use muted text with a transparent background/border. The base button minimum is 36px, with compact small actions (30px), in-row job actions (28px), and main workbench actions (39px) remaining their actual source exceptions. The frontmatter `height` records the base minimum, not a hard fixed height. Pressed primary gold and color/border hover feedback are retained. Disabled buttons reduce opacity to 0.45 and use the existing not-allowed cursor; they do not masquerade as actionable controls.

### Inputs / Fields

White fields use the field-edge stroke, compact padding and the control corner. Hover strengthens the edge; focus turns it gold with a subtle three-pixel gold halo. The global keyboard focus outline remains clearly visible (three pixels with a three-pixel offset); preview buttons use an inset outline to avoid clipping. Error messaging remains adjacent or in the existing alert/detail surface. No new validation behavior is introduced.

### Navigation / Source Tabs

The rail uses line icons, compact Chinese labels and the supplied logo. Active navigation uses pale grain with deep-gold text and a modest heavier weight. Source tabs use a thin gold underline rather than card containers. The narrow horizontal navigation restores text labels beside the icons and preserves active/ARIA state.

### Status Chips

Task state rectangles combine meaningful text with semantic color. Running uses pale grain; completed uses leaf; failure/interruption uses clay. Existing warning/adjustment status uses ochre. Color is not the only status indication.

### Material Cards

Material cards combine a landscape cover, a compact corner checkbox, duration overlay, one-line filename and small metadata. Covers use image cover cropping only in this browsing thumbnail slot. Hover and selected edge colors remain distinct; keyboard preview focus and play-overlay feedback remain available. The shadow-DOM specimen uses the existing no-thumbnail icon state so it does not embed a fake media asset.

### Task Tray / Full Details

The tray keeps its bounded 280px height. Failure rows remain 36px with a single-line ellipsized detail control; activating the existing control opens the complete text in a native dialog with wrapping and bounded scrolling. The compact row is not a substitute for the full error. Music continues to use the existing collapsed/expanded native details component in its existing workbench position.

**The Existing State Rule.** Preserve native dialogs, details, hidden and ARIA state contracts, media controls, field constraints and the existing three workflow steps.

### Extraction provenance

The normative source is the final cascade in `web/styles.css` and the existing markup in `web/index.html`, checked against `PRODUCT.md`, `.impeccable/direction-contract.md` and `.impeccable/mocks/decision/model-pick.html`. This is an existing-app presentation redesign with no added features or backend logic. The original 198 IDs and form/input contracts remain implementation constraints.

The exact user-supplied original image is recorded at `../evidence/provided-logo.jpg` and emitted as `/brand-logo.jpg`; `/brand-icon.ico` is its deterministic resource conversion. Neither this document nor its sidecar substitutes a newly drawn logo. The brand name covers the frontend and evidenced native title/icon; application data directories remain unchanged. The finish review reports ship for the captured frontend presentation with no material visual findings. It does not establish paid-pipeline execution or shortcut installation.

## Do's and Don'ts

### Do
- Do use the supplied original brand image and the exact 量禾混剪 name; the ICO is a deterministic resource conversion, not a redesigned mark.
- Do use the final approved override values when earlier declarations conflict; CSS cascade order is part of this extraction.
- Do retain compact failure rows with complete details in the existing dialog, and keep background music in its existing expandable location.
- Do preserve the existing IDs, form/input contracts, confirmations, APIs and data paths when applying the visual system.
- Do preserve visible keyboard focus, reduced-motion behavior and contained actual-video previews.

### Don't
- Don't replace the white navigation rail with a dark-navigation concept.
- Don't add sorting options, a third source tab, a help link or demo functionality from the decision sketch.
- Don't add font-CDN dependencies, GSAP choreography, page-load animation or marketing motion to this local working UI.
- Don't crop actual video previews, stretch source covers into portrait posters or hide the complete task error behind truncation alone.
- Don't treat fixture screenshots, frontend contracts or the visual ship verdict as verification of paid media production or shortcut deployment.
