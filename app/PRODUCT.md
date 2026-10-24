# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Existing Windows desktop wrapper (pywebview / Edge WebView2), local Flask HTTP server, vanilla HTML/CSS/JavaScript. Preserve this stack; no framework migration.

## Users

People operating the existing local product-video mixing application. The current user prioritizes seeing more source videos per screen, with moderately sized covers and compact information.

## Product Purpose

Select local/downloaded source footage, confirm product narration, and export a product video using the existing audio, visual-matching, subtitle and music pipeline.

## Operating Context

Windows desktop; primary display 1920×1080 or larger. Existing window may be smaller than the physical screen, so its current minimum size remains supported. Main surfaces are the material library, mixing workbench and settings.

## Capabilities and Constraints

- Existing import/download, selection, free preflight, JD product reading, narration, whole-voice audio, matching, editing, music library, job/history and export behavior stay unchanged.
- This task changes frontend presentation and branding only. Preserve DOM IDs, event bindings, forms, input types, restrictions, confirmations, API contracts and paid-call consent.
- Do not expose credentials in fixtures, screenshots, source or reports. Use an isolated fixture runtime for visual testing; do not trigger paid production.
- Keep current installation/data paths, source materials, history and configuration intact.

## Brand Commitments

- Name: 量禾混剪.
- User-supplied gold/charcoal wheat/infinity logo: `../evidence/provided-logo.jpg` in this repair record. It is the authoritative supplied brand image, not an invitation to redraw it.
- The user confirmed branding should cover the application UI, native window title, desktop shortcut name and shortcut icon.

## Evidence on Hand

Existing implementation and automated frontend behavior tests; the supplied logo; previous user screenshots showing the workbench, material library and task logs. Real production data stays in the user's runtime and is not used as invented fixture content.

## Product Principles

1. Keep the user's production workflow and controls familiar and fully operational.
2. Increase scanability and screen efficiency instead of making a marketing landing page.
3. Make selected, disabled, busy, warning and error states unambiguous.
4. Express the new brand through deliberate, consistent visual details.

## Open Decisions

Resolved: the user selected the code-led 留白编排 direction on 2026-10-24. The approved sketch is `.impeccable/mocks/decision/model-pick.html`; the implementation contract is `.impeccable/direction-contract.md`.
