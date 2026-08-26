# Onboarding runtime behavior

The end-to-end OCR-to-speech test has one cancellation scope across model
download, backend startup and OCR/playback. The page exposes `Cancel test`,
switches to `Cancelling...`, and disables wizard back/close navigation until the
worker reports a terminal result. Cancellation sets the shared token and shuts
down the controller. Isolated backend workers poll the controller shutdown token
while waiting for model health and terminate their exact child process when it
is set; the orchestration also checks cancellation before and after each stage.
A cancelled run is never reported as a startup failure or a successful test.
The Qt Cancel action only sets the shared token and updates visible status; the
background operation that owns startup also owns teardown. This prevents a Qt
slot from blocking on controller shutdown or racing a controller that is still
starting. Final application shutdown invalidates profile and live-stop
generation tokens, disables every controller-mutating action, discards modal
continuations and ensures any startup that crossed the cancellation boundary is
shut down when it returns. The ordinary initial post-onboarding controller start
uses the same generation-owned lifecycle: Quit suppresses its readiness and
hotkey signals and forces compensating cleanup if a blocked start returns late.

Readiness and onboarding diagnostics run on the shared Qt thread pool while the
GUI displays indeterminate progress. Retry starts a new launch identity; cancel,
page navigation and dialog closure invalidate the active identity immediately.
Results are accepted only from the latest still-active launch, so a slow probe
cannot repopulate a closed dialog, overwrite a newer settings check or authorize
the wizard after cancellation. The wizard keeps Next disabled until the current
diagnostic run completes without errors. Probe cancellation is best-effort at
the UI boundary: a blocking platform API may finish in its worker thread, but
its invalidated result is discarded.

Calibration review follows the same UI-thread boundary. The frozen selected crop
is visible immediately while OCR runs on the shared thread pool; Save stays
disabled until the current result arrives. A failed recognizer does not silently
look successful: the only enabled fallback is explicitly labeled `Save region
without OCR preview`. Closing the review invalidates a late OCR result.
