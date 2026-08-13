# TODO

Keep this file limited to actionable work. Remove completed items and empty
sections after their implementation has been verified and committed.

## Architecture

- [ ] Split all Reverse: 1999-specific extraction and voice-preparation code
      into a standalone `reverse1999-extractor` repository. Keep VNTTS usable
      for any visual novel and connect the projects only through versioned,
      game-agnostic local artifacts.
  - [ ] Move installed-game discovery, config decryption, Unity story parsing,
        Wwise bank/media resolution, NPC catalog and alias handling, reference
        quality review, audition UI, and batch import out of VNTTS.
  - [ ] Move the Reverse: 1999 CLI commands, tests, examples, catalog data, and
        game-specific dependencies (`cryptography` and `UnityPy`) with them.
  - [ ] Define and test `vntts.story-index` JSONL schema version 1. Start with a
        metadata record, then stable line records containing chapter, sequence,
        speaker, text, source, portrait, original voice ID, timing, line kind,
        audio-resolution status, and optional emotion/delivery hints.
  - [ ] Continue emitting the existing generic VNTTS character voice manifest;
        define a separate generated-audio manifest mapping stable line IDs and
        text hashes to pregenerated audio without exposing Wwise concepts.
  - [ ] Add generic story-index selection to VNTTS settings and game profiles.
        Use it for chapter detection, voice preloading, and exact pregenerated
        audio lookup; remove the built-in Reverse: 1999 scanner and mapping UI.
  - [ ] Preserve compatibility with existing local Reverse: 1999 indexes,
        mappings, reviews, selected references, and voice packs or provide a
        one-time migration command.
  - [ ] Keep extracted game text/audio, decrypted configs, indexes, review
        caches, and generated packs out of Git; document that users need their
        own installed copy and the right to use the resources.
  - [ ] Verify both repositories independently, initialize the extractor as its
        own Git repository, then remove the migrated implementation from VNTTS.

- [ ] Extract every English story line that may need ahead-of-time voice
      generation, not only dialogue currently seen by OCR.
  - [ ] Parse the `configs/story` Unity bundle (`json_story_step_*`) and retain
        dialogue plus narration with surrounding-line context. Current local
        inventory: about 125,950 speakable lines.
  - [ ] Classify the 25 anecdote chapters separately. Current local inventory:
        about 15,792 speakable lines, including about 12,393 with no installed
        audio, 3,235 with audio, 109 configured without a local media route,
        and 55 unresolved.
  - [ ] Extract newer interactive character stories from
        `json_hero_story_plot`: about 2,763 lines (1,588 dialogue and 1,175
        narration), even though they do not carry direct story voice IDs.
  - [ ] Search remaining config tables and bundles for readable text outside
        main story, anecdotes, and hero stories: events, activities, tutorials,
        battle dialogue, tips, optional branches, mail, and other story-like UI.
  - [ ] Resolve original-audio availability exactly through config play-event
        rows, the game's Wwise event hash, bank event routes, and embedded or
        streamed media. Do not infer "missing" from a blank speaker or filename.
  - [ ] Persist explicit statuses: installed audio, definite no-audio, configured
        but missing local route/media, and unresolved. Current all-story estimate:
        about 48,737 installed, 63,463 definite no-audio, 13,367 configured but
        unavailable locally, and 383 unresolved.
  - [ ] Produce a generation queue for all lines without usable audio, grouped
        by character and story order, with previous/next text, narration flag,
        scene/chapter, portrait, timing, and inferred emotion/delivery. Preserve
        aliases such as `Slouch Hat` -> `Brimley` before voice selection.
  - [ ] Support resumable bulk generation, model/provider provenance, prompt and
        seed metadata, deterministic text hashes, quality checks, manual review,
        retries, and atomic manifest publication.
  - [ ] Prefer higher-quality offline generation for this queue, including
        models that accept natural-language emotion/delivery direction; keep the
        low-latency runtime model as a fallback rather than the quality ceiling.
  - [ ] Size and batch the queue before generation. Current definite-missing
        estimate is about 855,000 English words, roughly 95 hours of speech at
        150 words/minute and roughly 2.7 GB at Opus 64 kbit/s.

- [ ] Consolidate schema-version checking, malformed-document fallback, and
      atomic publication for versioned JSON settings, profiles, OCR corrections,
      and review metadata while keeping domain decoding in each store.

## Live mode

### P0 - Finish Reverse: 1999 NPC voice references before migration

- [ ] Process the remaining unresolved story NPCs with the validated assisted
      mapping and voice-import workflow, then continue this work in the separate
      extractor repository.

### P0 - Validate performance and resilience

- [ ] Validate the live-mode acceptance targets on supported hardware:
  - No audio underruns or buzzing during a 30-minute live session.
  - Start a cached-voice sentence within 2 seconds on the supported CPU target
    and within 750 ms on the supported CUDA target.
  - Start an already-visible second sentence within 300 ms of the first ending.
  - Never speak or advance stale OCR generations.

### P1 - Benchmark remaining backends and hardware

- [ ] Benchmark XTTS and Chatterbox Nano on the target Windows CPU and CUDA
      hardware; compare first-audio latency, realtime factor, speaker similarity,
      hallucinations, RAM/VRAM, and package size.
- [ ] Prototype Chatterbox Turbo for English CUDA voice cloning and compare it
      with the existing XTTS and Chatterbox Nano measurements.
- [ ] Keep F5-TTS as a GPU quality/performance comparison, not the initial live
      backend, because its strongest published latency path depends on
      TensorRT-LLM-class GPU deployment.

### P1 - Evaluate OCR replacement only after pipeline optimization

- [ ] Validate the optional RapidOCR backend in the Windows portable build and
      compare it with Tesseract on the target Windows hardware.

### P1 - Live-mode integration and safety tests

- [ ] Add real Windows and macOS soak tests covering CPU and GPU speech, animated
      scenes, rapid manual advancement, and application shutdown during every
      pipeline stage.

## Windows application

### P2 - Windows distribution

- [ ] Produce a standalone Windows build on Windows.
  - [ ] Build on Windows and confirm Python, Qt, Tesseract, English language
        data, and eSpeak-NG are present in the portable artifact.
  - [ ] Verify operation without Python, uv, Tesseract, or development tools installed.

- [ ] Create and sign a Windows installer.
  - Add Start Menu shortcuts, optional startup, upgrades, and clean uninstall support.
  - Preserve downloaded models and user settings during application upgrades.
  - Sign the executable and installer for public distribution.

- [ ] Add Windows compatibility and release testing.
  - Cover Windows 11, common GPU vendors, multiple displays, and DPI scaling.
  - Cover windowed, borderless, normal-user, and elevated game processes.
  - Run installation and OCR-to-speech smoke tests on a clean Windows machine.
