---
name: volcengine-seedream-commercial
description: Commercial image planning and prompt protocol adapted from the official Volcengine Agent Plan Seedream Skill
---

# Volcengine Seedream Commercial

Use this Skill for customer-facing posters, product scenes, social creatives,
campaign key visuals, image variations, and reference-guided image generation.
It is an Astra-managed adaptation of the official Volcengine Agent Plan
Seedream Skill v3.0.0.

## Capability and security boundary

- This Skill is an instruction package. It does not grant a provider account,
  a Tool, a model entitlement, or permission to spend Credits.
- Use only the managed `generate_image_minimax` Tool. Despite its legacy name,
  the identifier is protocol compatibility rather than provider evidence.
  Astra routes formal delivery with `execution_strategy=commercial_quality`
  and exploratory ideation with `execution_strategy=creative_exploration`.
- Never request, repeat, save, or pass an API key in chat or Tool arguments.
  Astra resolves the encrypted platform credential server-side.
- Never execute the upstream JavaScript directly. Its desktop paths,
  environment-key discovery, and local persistence bypass Astra tenant
  storage, durable task records, Credits, and provider failover.
- If `generate_image_minimax` is absent or its capability status is
  unavailable, explain the missing capability. Do not claim that this Skill
  itself generated an image.

## Build the commercial brief before the prompt

Infer these fields from the request and attachments. Ask only when the missing
answer would materially change the output:

1. **Outcome** — packshot, lifestyle scene, poster background, campaign key
   visual, editorial illustration, or a creative redraw.
2. **Format** — default to `1:1`; use `3:4` for poster/e-commerce, `9:16` for
   short-video covers, and `16:9` for banners.
3. **Subject contract** — who or what must remain recognizable, including age
   range, wardrobe, product shape, material, color, and distinctive details.
4. **Scene** — location, time, props, foreground/background separation.
5. **Composition** — framing, subject position, lens perspective, and negative
   space for later copy.
6. **Look** — one coherent style, lighting direction, color palette, material
   response, and finish.
7. **Exclusions** — unwanted words, watermarks, duplicated products, extra
   fingers, malformed hands, distorted packaging, or competing focal points.

## Prompt protocol

Write one concrete production prompt in this order:

`deliverable + subject contract + action + scene + composition + camera/lens +
lighting + palette/material + commercial finish + consistency constraints +
exclusions`

Prefer observable instructions over empty quality adjectives. For example,
replace "high-end, 8K" with "soft key light from camera left, narrow rim light,
brushed stainless-steel reflections, shallow depth of field, clean premium
appliance campaign finish."

For real people, explicitly constrain realistic skin texture, anatomically
correct hands, natural eye direction, plausible contact with the product, and
stable wardrobe/product identity.

For a set of related images, first define a shared identity block for the
subject, product, palette, lighting, and lens. Repeat that block unchanged in
every Tool call, then change only the shot/action block. Astra currently
delivers one durable image artifact per call; never claim that an unsupported
upstream sequential mode was used.

## Reference, brand, and copy rules

- Use `reference_image` when the model may creatively redraw the subject.
- Use `brand_asset` when the uploaded product, package, or logo must remain
  pixel-faithful. Do not combine it with `reference_image`.
- Put exact visible wording in `overlay_text`, not in the generation prompt.
  Preserve the user's Unicode characters and line breaks exactly.
- For hierarchical poster copy use ordered `overlay_blocks`; the returned PNG
  is already the final deterministic composition. Never place it behind the
  same HTML/PDF/PPTX text a second time, and do not create unrequested formats.
- When `brand_asset` or `overlay_text` is used, ask the model for only the clean
  background and leave deliberate negative space. Astra will composite the
  protected layer and copy deterministically.
- For vertical posters, compose one continuous scene rather than literal
  top/middle/bottom panels. Carry light, atmosphere, color, particles, and
  secondary graphics through the frame with feathered transitions. Create
  copy-safe space by reducing local detail and contrast, never with a flat
  rectangular slab, hard horizontal seam, split-screen section, or template
  band.
- One flat reference cannot prove unseen product sides. Request approved
  multi-view/3D material or disclose the limitation before inventing a new
  angle.

## Quality gate

After the Tool succeeds, inspect the actual artifact before calling it ready:

- subject/product identity and count are correct;
- hands, face, contact, geometry, reflections, and shadows are plausible;
- the intended focal hierarchy is visible at thumbnail size;
- no pseudo-text, watermark, unexpected logo, or duplicated object exists;
- exact copy and protected assets remain crisp;
- aspect ratio and delivery path match the request.

If a defect is local, revise only the failing part of the prompt. Do not replace
the full brief with generic "more detailed" wording. A successful Tool receipt
proves delivery, not commercial quality; record the visual review separately.

## Provenance

The reviewed upstream source, version, hash, supported Agent Plan model, and
Astra adaptation boundary are recorded in
`references/official-agent-plan-contract.md`.
