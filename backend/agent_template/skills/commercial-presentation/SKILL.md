---
name: commercial-presentation
description: Create or revise customer-facing slide decks, proposals, reports, pitches, and presentations that require editable PPTX plus matching PDF, sourced claims, intentional layouts, real visuals, and governed quality review
---

# Commercial Presentation

Use this Skill when the requested result is a presentation, slide deck, proposal,
pitch, review deck, report deck, or an editable PPTX/PDF deliverable.

Do not use it for a single poster, a standalone video, a plain text outline, or
casual advice that does not require a presentation artifact.

## Keep capability and authority separate

- This Skill guides presentation planning, construction, and review. It does not
  grant a Tool, a Provider account, model entitlement, Credits, tenant access, or
  approval authority.
- Use only Tools exposed in the current runtime. Never choose or reveal a media
  Provider or API key.
- For a customer-ready PPTX/PDF, use the governed
  `builtin.presentation.v1` Deliverable workflow. Treat its server-owned brief,
  paths, output contract, visual policy, and approval state as authoritative.
- If the formal workflow or either `convert_html_to_pptx` or
  `convert_html_to_pdf` is unavailable, preserve a useful outline and page plan,
  report the missing capability, and stop. Never present an outline, HTML file,
  placeholder, or broken download as a finished deck.

## Infer the brief without turning the task into a form

Infer the following from the request, attachments, tenant context, and supplied
sources. Ask only when a missing answer would materially change the result:

1. goal and decision or action the deck must support;
2. audience and what they already know;
3. required page count, language, and aspect ratio;
4. required points, exact wording, facts, metrics, and brand constraints;
5. tone, visual direction, and examples the user wants to follow or avoid;
6. supplied sources and assets, including their workspace paths;
7. required editability and delivery formats.

Default to the page count and language in the formal request. Do not invent an
audience, customer, company, author, brand, metric, quote, date, ranking, price,
market share, ROI, or source. Mark an unsupported idea as a hypothesis to
validate, or stop and request the missing evidence when the claim is material.

## Build one narrative before drawing slides

1. Define the core message in one sentence.
2. Give every page one purpose and one takeaway.
3. Order the pages as a decision narrative: context, evidence, implication,
   recommendation, and next action. Adapt the sequence to the actual task rather
   than forcing a fixed template.
4. Remove pages that repeat another page's job.
5. Keep exact customer wording unchanged unless the user approves a rewrite.

For a formal request, write the required `outline.json` and `slide_spec.json`
under `workspace/deliverables/<request_id>/`. Keep slide identifiers, titles,
purpose, evidence, visual intent, layout, body points, asset references, and
source references consistent across the outline, page specification, HTML, PPTX,
and PDF.

## Match the visual form to the information

Choose the layout from the page's purpose and information shape:

- use a hero composition for an opening or decisive closing;
- use an editable chart or table only for sourced structured data;
- use a timeline, process lane, relationship map, or comparison matrix when it
  explains a real relationship;
- use supplied or generated imagery for product, people, environment, or
  campaign storytelling;
- use typography, shapes, spacing, and restrained color for information-led
  pages that do not need imagery.

Do not use the same title-and-bullets layout repeatedly. Do not use emoji,
decorative pseudo-charts, fake dashboards, star ratings, or meaningless icons as
evidence. Do not ask an image model to generate a complete slide containing
important text. Keep important text, charts, tables, and diagrams editable.

When the formal visual policy requires generated images, use the product-level
image Tool only for the declared asset roles. Use the exact versioned workspace
path returned by the Tool. Never retry a paid generation manually after an
accepted or ambiguous submission. If imagery is optional and unavailable,
continue with a clean information design. If required imagery is unavailable,
stop before conversion and report the missing asset role.

## Produce one validated source and two matching outputs

Build one `presentation.html` source at 1280x720 per slide. Keep each visible
element inside the page; reduce copy, padding, gaps, or visual height rather than
hiding overflow.

Treat projected readability as a delivery contract, not a visual preference.
Target at least 22px for ordinary body copy and keep every title, body, table,
caption, footnote, and decision label at or above the 16px hard floor. Only a
short folio or eyebrow placed at the top or bottom edge may use 10–15px, and it
must declare `data-clawith-text-role="metadata"`. Never mark body copy, evidence,
tables, or footnotes as metadata to bypass the floor. Split the slide or reduce
copy instead of shrinking text.

Before conversion, verify:

- page count and ordered slide identifiers match the formal brief;
- every page has exactly one visible title and an implemented visual region;
- every factual claim has a real `source_ref` or is clearly labelled as a
  hypothesis;
- local asset paths exist and every required image is visibly used;
- no TODO, TBD, placeholder copy, fabricated rating, or internal workflow label
  remains;
- adjacent pages do not repeat the same layout without a content reason.

Convert the same validated HTML exactly once to editable or hybrid-editable PPTX
with `convert_html_to_pptx`, and exactly once to paginated PDF with
`convert_html_to_pdf`. Pass the formal page-count, outline, and slide-spec
contracts to both Tools. Keep all files under
`workspace/deliverables/<request_id>/`.

Report only the exact versioned workspace paths returned by successful Tools.
Do not claim page-count, editability, visual consistency, no-overflow, or
commercial readiness until the registered Artifact checks and required human
review confirm them.

## Review and revise without destroying accepted work

Review the rendered pages, not only the HTML or conversion receipt:

- narrative continuity and audience fit;
- fact, identity, source, and exact-copy accuracy;
- readable hierarchy, density, contrast, and alignment;
- purposeful layout and visual variety;
- image crop, resolution, product/person consistency, and watermark safety;
- PPTX/PDF page count, aspect ratio, editability, and visual agreement.

Record page-specific defects. Revise only the affected page, asset, fact, or
composition when the workflow supports a governed revision. Never overwrite an
approved Artifact. If the current workflow cannot attach a page-level revision,
preserve the review notes and start the governed revision path instead of
silently replacing the previous file.

Treat a successful conversion as a candidate, not a commercial approval.
