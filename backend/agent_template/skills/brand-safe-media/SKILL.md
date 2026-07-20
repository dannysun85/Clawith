---
name: Brand-safe Media
description: Required workflow for customer-facing images or videos containing exact copy, logos, packaging, or reference products
---

# Brand-safe Media

Use this skill whenever a user asks for an image, poster, short video, Douyin creative, advertisement, title card,
logo placement, packaging display, or reference-product creative.

## Infer the outcome before choosing a tool mode

Do not make the user complete a production form before work begins. Read the request and attachments, infer the
likely deliverable, and ask only for information whose absence would materially change the result (for example aspect
ratio, exact copy, or whether product redraw is acceptable). Present the inferred brief in one short confirmation when
the request is ambiguous.

Choose between these three outcomes:

1. **Poster / packshot / end card** — exact product and exact copy matter; a static product layer is appropriate.
2. **Product-in-motion advertisement** — the supplied product must participate in the scene or camera movement.
3. **Creative reference** — the attachment is inspiration and generative redraw is acceptable.

Never use the static product-layer workaround for outcome 2 and describe it as a generated product advertisement.

## Choose the delivery contract

### Brand-safe delivery

Use brand-safe delivery when the user supplies exact Chinese/English copy or requires a product, logo, packaging,
or brand asset to remain unchanged.

1. Preserve user-supplied copy exactly. Do not translate, rewrite, summarize, correct, or place it inside `prompt`.
2. Put the exact visible copy in `overlay_text` and describe only the text-free visual background in `prompt`.
3. Put an uploaded product/logo path in `brand_asset`. Do not combine `brand_asset` with `reference_image`,
   `first_frame_image`, or `last_frame_image`.
4. Prefer a transparent PNG for `brand_asset`. JPG/WebP are accepted but their original rectangular background is
   intentionally preserved; never silently remove or redraw it.
5. Use `brand_position` and `brand_scale` only for posters, packshots, and deliberate video end cards. In video this
   product layer is static; disclose that limitation and do not use it for a request that expects product motion.
6. Do not blur or soften the scene by default. Background sanitization is a fallback for provider-created pseudo-text,
   not a general quality enhancement. Prefer a clean text-free prompt and deterministic overlay copy first.
7. Only report success after the tool returns a real saved workspace path and a brand-safe receipt. The receipt must
   record `background_sanitized=true` whenever the background transform ran; this flag is an execution receipt, not
   proof that every unintended glyph is unreadable.

### Product-in-motion delivery

When the user provides a product photo and asks for a moving advertisement:

1. Use the real uploaded frame through `reference_image` or `first_frame_image` when the provider route supports it.
2. State that a single flat photo cannot guarantee unchanged packaging, typography, geometry, or unseen sides during
   generated motion. Do not call the result exact or brand-safe merely because the first frame was exact.
3. If exact identity is mandatory, offer a bounded composition instead: animate camera/background elements around a
   static packshot, produce a storyboard for approval, or request transparent/multi-view/3D brand assets.
4. If the selected provider cannot accept the reference frame, stop with a capability explanation. Do not silently
   fall back to text-to-video or regenerate a look-alike product.
5. Validate the returned artifact visually before presenting it: product identity, label readability, copy, duration,
   aspect ratio, and absence of corrupted frames.

### Creative delivery

Use `reference_image`, `first_frame_image`, or `last_frame_image` when generative motion or redraw is part of the
request. Explain that spelling, packaging, logos, and product geometry may vary unless the request already makes that
trade-off clear. Do not describe creative delivery as exact or brand-safe.

## Copy rules

- User supplied exact copy: pass the same Unicode characters and line breaks to `overlay_text`.
- User asked you to write copy: create the final copy first, then pass that final text unchanged to `overlay_text`.
- If the exact copy is too long for the canvas, shorten it only with user approval; never rely on silent truncation.
- Never ask an image/video model to draw words when `overlay_text` is available.

## Product limits

- One flat image cannot prove unseen sides or a physically accurate rotation.
- For new angles, request approved multi-view images or a 3D asset, or use creative delivery with an explicit warning.
- Skills guide the workflow; the native media tools enforce fonts, layout, frozen asset hashes, video decoding, storage,
  and Credits settlement.
