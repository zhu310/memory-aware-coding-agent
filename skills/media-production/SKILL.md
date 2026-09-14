---
name: media-production
description: Generate images and configured music, and deliver or process uploaded audio and video files.
---

Images: image_generate with a stable request_id, then image_job_status(wait_seconds=30). Reuse reference_file_ids for edits. Inspect using image_understand before describing visible results.
Music: music_generate(prompt,request_id,seconds=30,instrumental=true) calls an independently configured ElevenLabs service. If unconfigured, tell the user what configuration is missing; do not fake an MP3 or call speech synthesis music. Use creative_job_status to wait and creative_job_cancel when requested. unknown submission must not be retried under a new ID without resolving whether the provider accepted it.
Office/music jobs persist and reappear after refresh. Cancellation of external generation may stop local retrieval without reversing provider billing.
Uploaded WAV/MP3/OGG/FLAC/M4A/MP4/WebM are stored and playable. file_read reports technical metadata only, NOT the content or a transcript. Do not claim to have listened to audio or understood video using metadata alone.
FFmpeg and ffprobe are installed for short CPU media processing. Work inside the workspace, retain the original, bound duration/resolution, then file_import. Start with short samples on this small server. Use existing background_run only for disposable short commands; it does not provide durable media-job recovery. Never claim such a shell process survives a backend restart. Professional long renders require a separate worker and budget.
