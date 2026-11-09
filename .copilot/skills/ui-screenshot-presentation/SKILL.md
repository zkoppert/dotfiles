---
name: ui-screenshot-presentation
description: Apply these instructions when creating UI screenshots, before/after comparisons, or annotated UI explanations for pull request descriptions. Preserve the authentic UI and style only the surrounding presentation. Also use when a user asks to annotate, present, or compare captured UI.
---

# Present UI screenshots in pull request descriptions

Preserve the real UI. Style only the surrounding presentation.
Use mixed sans-serif and monospace typography, not an all-violet monospace poster.

## Inspect the visual reference

Before composing, open and inspect this [approved example](https://github.com/user-attachments/assets/44776e2c-cc0c-406b-8969-ceeb149be5a9).

Match its typography hierarchy, quiet canvas, compact numbered badges, and leader lines. Use it only as a presentation reference. Capture fresh, authentic UI for the current task.

If you cannot open the reference, report that limitation. Follow the explicit specifications below.

## Assign typography roles

- Main figure heading: neutral or dark Mona Sans VF, semibold, approximately 28 to 32px. Never use violet or monospace.
- Figure description: neutral or dark Mona Sans VF, regular, approximately 16px. Never use violet or monospace.
- Short state labels and optional eyebrows: violet, bold Mona Sans Mono VF, approximately 12 to 14px.
- Callout headings: violet, bold Mona Sans Mono VF, approximately 16px.
- Callout descriptions: neutral or dark Mona Sans VF, regular, approximately 16px.
- Marker numbers: white, bold Mona Sans Mono VF, approximately 13px, inside a violet badge.

Use the actual font assets and verify that they rendered. Do not silently substitute a system monospace font. Do not change the captured UI's typography.

Keep approximately 4 to 8px between the figure heading and its description. Keep approximately 4px between a callout heading and its description. Callout headings must not be smaller than their descriptions.

Do not apply violet mono styling to a parent that contains all text. Reserve this styling for short annotation labels and numbers.

## Draw the markers

For annotated explanations and before/after presentations, normally include one to three targeted numbered callouts.

Each callout consists of:

- A compact violet badge with a bold white mono number.
- A thin violet leader line.
- A small violet endpoint dot beside the relevant UI feature.
- An adjacent short callout heading and description.

A violet caption or `Before` or `After` label alone is not a callout.

Use approximately:

- Badge padding: 4px vertical and 8px horizontal.
- Badge corner radius: 6px.
- Leader-line stroke: 1 to 1.5px.
- Endpoint-dot radius: 3 to 4px.

These values are CSS dimensions at the intended display size. They are not doubled export-pixel dimensions.

Draw lines and dots as a separate SVG or presentation layer. Keep badges and explanations outside the UI. Route lines through whitespace. Do not obscure readable text or controls.

Choose targets that explain an actual change or distinction. Do not invent differences or add meaningless markers. Simple unannotated screenshots can omit markers when there is nothing useful to identify.

## Write copy and inspect the export

Headings must name the actual component, behavior, state, or change. Do not use slogans, generic praise, or decorative filler.

Descriptions must add a concrete fact. Omit the description when it adds no useful fact. Do not put source footers or capture metadata in the artwork.

Before sharing, inspect the exported image at its intended size:

- Main heading and prose use neutral sans-serif typography.
- Short annotation headings use bold violet monospace typography.
- Annotated figures contain badges, lines, and dots.
- Text is readable.
- Spacing is tight within callouts.
- The real UI remains unchanged.
