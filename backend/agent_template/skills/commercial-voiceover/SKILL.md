---
name: commercial-voiceover
description: Create or revise customer-facing narration, voice-over, spoken advertisements, explainers, accessibility audio, and video voice tracks that require exact scripts, pronunciation control, durable audio artifacts, provider-safe routing, and human listening review
---

# Commercial Voiceover

Use this Skill for narration, voice-over, spoken advertisements, explainers,
accessibility audio, and a spoken track for a video.

Do not use it for music generation, synchronized actor dialogue, voice cloning,
or a text draft that does not require an audio artifact.

## Keep capability and authority separate

- This Skill guides script preparation, speech generation, listening review,
  and video mixing. It does not grant a Tool, Provider account, voice identity,
  model entitlement, Credits, tenant access, or approval authority.
- Use the provider-neutral `generate_speech_minimax` Tool name exposed by the
  current runtime. Astra selects an eligible Provider before submission.
- Never choose, reveal, or promise a Provider. Never retry manually after a
  Provider has accepted a request or acceptance is ambiguous.
- If the Tool is unavailable, preserve the exact script, pronunciation notes,
  and audio brief, report the blocked capability, and stop. Do not present a
  script, silent video, placeholder, or missing path as generated speech.

## Establish the spoken-audio contract

Infer the following from the request and supplied assets. Ask only when a
missing answer would materially change the result:

1. exact script and language;
2. audience, channel, and intended action;
3. narration, advertisement, explainer, or accessibility use;
4. tone, energy, pacing intent, and words that need pronunciation guidance;
5. target duration when material;
6. required format and whether the audio will be mixed into a video;
7. consent and usage rights for any requested voice identity.

Do not invent facts, claims, names, pronunciations, testimonials, or legal
disclaimers. Preserve exact customer copy. If the target duration cannot fit the
exact script naturally, propose a shorter script for approval; do not silently
speed, trim, or rewrite it.

## Use voice identity safely

- Omit `voice_id` to use the managed default and retain automatic Provider
  failover.
- Pass `voice_id` only when the user or an authorized tenant configuration
  supplies a real identifier and that exact identity is material.
- Voice identifiers are Provider-specific. Never invent one, translate one
  across Providers, or claim that two different voices are equivalent.
- A Provider-specific voice request may reduce or remove automatic failover.
  Report unavailability rather than silently switching identities.
- Do not claim voice cloning, celebrity imitation, or identity consent. The
  current Tool performs speech synthesis, not governed voice cloning.

## Generate one durable candidate

1. Normalize punctuation and pronunciation only when it does not change the
   approved wording.
2. Call `generate_speech_minimax` once with the exact script, an optional
   authorized `voice_id`, `mp3`, `wav`, or `flac`, and a path under
   `workspace/audio/`.
3. Trust only a successful structured Tool receipt and its exact versioned
   workspace path. Do not fabricate or shorten the path.
4. Treat the returned audio as a candidate until required listening review and
   customer approval are complete.

For narrated video, create or obtain the final visual first, generate the
voice-over with `require_audio=false` on the visual task, then use
`compose_video_audio`. Keep the voice intelligible and any music bed
subordinate. Do not claim lip synchronization unless the video Provider
generated synchronized audio and the result passed visual/audio review.

## Review the actual audio

Listen to the delivered file and record defects against the approved script:

- every word is present and in the correct order;
- names, numbers, abbreviations, and multilingual phrases are pronounced
  acceptably;
- voice, tone, pace, pauses, and emphasis fit the audience and channel;
- there is no clipping, truncation, silence, corruption, or distracting noise;
- the actual duration and browser-playable format are reported truthfully;
- video mixing preserves intelligibility and does not imply false lip sync.

Revise the script, voice selection, or mix only for the failed dimension.
Never overwrite an approved Artifact. A successful Provider response is not a
commercial approval.
