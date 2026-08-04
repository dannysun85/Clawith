# Official Agent Plan speech contract

- Source: https://docs.volcengine.com/docs/82379/2516286?lang=zh
- Reviewed: 2026-08-04 against the official page updated on 2026-07-29.
- Speech generation model: `doubao-seed-tts-2.0`.
- HTTP endpoint:
  `POST https://openspeech.bytedance.com/api/v3/plan/tts/unidirectional`.
- Resource identifier: `seed-tts-2.0`.
- Astra uses a fresh connect identifier for every request, bounds the streamed
  response size, stores only the resulting audio Artifact and secret-free
  provider receipt, and never exposes the Agent Plan API key to an Agent.

The same official page documents `doubao-seed-asr-2.0` for speech recognition.
ASR is a separate inbound-audio product capability: this voice-over Skill does
not transcribe uploads, and Astra currently has no provider-neutral
transcription Tool, durable transcript Artifact contract, or user-facing ASR
entry. Do not claim ASR is available until those boundaries and their tests are
implemented.

The official Volcengine Skill registry currently publishes dedicated Seedream
and Seedance Skills, not a speech Skill package. Astra therefore keeps speech
as a managed provider-neutral Skill backed by the official API contract rather
than inventing an upstream voice Skill identity.
