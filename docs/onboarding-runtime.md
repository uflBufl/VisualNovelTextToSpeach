# Onboarding runtime behavior

The end-to-end OCR-to-speech test has one cancellation scope across model
download, backend startup and OCR/playback. The page exposes `Cancel test`,
switches to `Cancelling...`, and disables wizard back/close navigation until the
worker reports a terminal result. Cancellation sets the shared token and shuts
down the controller. Isolated backend workers poll the controller shutdown token
while waiting for model health and terminate their exact child process when it
is set; the orchestration also checks cancellation before and after each stage.
A cancelled run is never reported as a startup failure or a successful test.

Readiness and onboarding diagnostics run on the shared Qt thread pool while the
GUI displays indeterminate progress. Retry starts a new launch identity; cancel,
page navigation and dialog closure invalidate the active identity immediately.
Results are accepted only from the latest still-active launch, so a slow probe
cannot repopulate a closed dialog, overwrite a newer settings check or authorize
the wizard after cancellation. The wizard keeps Next disabled until the current
diagnostic run completes without errors. Probe cancellation is best-effort at
the UI boundary: a blocking platform API may finish in its worker thread, but
its invalidated result is discarded.
