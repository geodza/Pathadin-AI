# Pathadin AI 4.0.1 verification

64 tests passed. New tests cover MRXS sidecar-folder exclusion, reviewed-mask gating with zero model calls, automatic masks remaining unreviewed, individual slide failures, queue-wide request caps, reuse on resume, duplicate queue detection, CSV row export, pause after the current slide, and restart-to-paused recovery.

Browser check in headless Microsoft Edge: folder scan, selection table, shared settings, queue creation, preflight, mask thumbnail and review-required state all worked without JavaScript errors. JavaScript syntax checks passed.

Provider responses in queue tests were simulated; no paid model calls were made. A fresh Windows installation and large overnight production studies have not been validated. Disk check is a minimum reserve only. No clinical accuracy claims.

Package rename verified: 64 tests passed after updating package imports, launcher, picker subprocess, browser session-token names and PATHADINAI_DATA. The release archive was scanned for the former package name and private data.
