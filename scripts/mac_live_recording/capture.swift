import AppKit
import AVFoundation
import ScreenCaptureKit
import CoreGraphics
import Foundation
import Darwin

func windowBoundsMetadata(_ frame: CGRect) -> [String: Double] {
    ["x": frame.origin.x, "y": frame.origin.y, "width": frame.size.width, "height": frame.size.height]
}

struct SelectedWindowCheck {
    let reason: String
    let evidence: [String: Any]
    let observedFrame: CGRect?
    var accepted: Bool { reason == "unchanged" || reason == "bounds-origin-changed" }
}

private func windowIdentityNumber(_ value: Any?, maximum: Double) -> NSNumber? {
    guard let number = value as? NSNumber,
          CFGetTypeID(number) != CFBooleanGetTypeID(),
          number.doubleValue.isFinite, number.doubleValue > 0,
          number.doubleValue <= maximum,
          number.doubleValue.rounded() == number.doubleValue else { return nil }
    return number
}

func checkSelectedWindow(_ lookup: Any?, windowID: CGWindowID, pid: pid_t, frame: CGRect) -> SelectedWindowCheck {
    let rows = lookup as? [Any]
    let current = rows?.first as? [String: Any]
    let observedID = windowIdentityNumber(current?[kCGWindowNumber as String], maximum: Double(UInt32.max))
    let observedPID = windowIdentityNumber(current?[kCGWindowOwnerPID as String], maximum: Double(Int32.max))
    let rawBounds = current?[kCGWindowBounds as String] as? [String: Any]
    let coordinates = ["X", "Y", "Width", "Height"].compactMap { key -> Double? in
        guard let number = rawBounds?[key] as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID(), number.doubleValue.isFinite else { return nil }
        return number.doubleValue
    }
    let observedFrame = coordinates.count == 4 ? CGRect(x: coordinates[0], y: coordinates[1], width: coordinates[2], height: coordinates[3]) : nil
    let validFrame = observedFrame.flatMap { bounds -> CGRect? in
        guard bounds.origin.x.isFinite, bounds.origin.y.isFinite,
              bounds.size.width.isFinite, bounds.size.height.isFinite,
              bounds.size.width > 0, bounds.size.height > 0 else { return nil }
        return bounds
    }
    let reason: String
    if lookup == nil { reason = "lookup-unavailable" }
    else if rows == nil { reason = "lookup-malformed" }
    else if rows!.isEmpty { reason = "lookup-empty" }
    else if current == nil { reason = "lookup-entry-malformed" }
    else if rows!.count != 1 { reason = "lookup-entry-count-mismatch" }
    else if current?[kCGWindowNumber as String] == nil { reason = "window-id-missing" }
    else if observedID == nil { reason = "window-id-malformed" }
    else if observedID!.uint32Value != windowID { reason = "window-id-mismatch" }
    else if current?[kCGWindowOwnerPID as String] == nil { reason = "owner-pid-missing" }
    else if observedPID == nil { reason = "owner-pid-malformed" }
    else if observedPID!.int32Value != pid { reason = "owner-pid-mismatch" }
    else if current?[kCGWindowBounds as String] == nil { reason = "bounds-missing" }
    else if validFrame == nil { reason = "bounds-malformed" }
    else if validFrame!.origin != frame.origin && validFrame!.size != frame.size { reason = "bounds-origin-and-size-changed" }
    else if validFrame!.origin != frame.origin { reason = "bounds-origin-changed" }
    else if validFrame!.size != frame.size { reason = "bounds-size-changed" }
    else { reason = "unchanged" }
    return SelectedWindowCheck(reason: reason, evidence: [
        "reason": reason,
        "original_target": ["window_id": windowID, "pid": pid, "bounds": windowBoundsMetadata(frame)],
        "observed_target": [
            "window_id": observedID as Any? ?? NSNull(),
            "pid": observedPID as Any? ?? NSNull(),
            "bounds": observedFrame.map { windowBoundsMetadata($0) as Any } ?? NSNull()
        ],
        "lookup_available": lookup != nil,
        "lookup_entry_count": rows.map { $0.count as Any } ?? NSNull()
    ], observedFrame: validFrame)
}

func targetMetadata(_ window: SCWindow) -> [String: Any] {
    ["window_id": window.windowID, "pid": window.owningApplication!.processID,
     "title": window.title as Any? ?? NSNull(),
     "bounds": windowBoundsMetadata(window.frame),
     "mechanism": "ScreenCaptureKit desktopIndependentWindow", "selected_at": ISO8601DateFormatter().string(from: Date()),
     "monotonic_s": monotonicSeconds()]
}

@MainActor func capture(_ args: [String]) async throws {
    let videoOnly = args.first == "--video-only"
    let a = Array(args.dropFirst())
    guard (videoOnly && a.count == 4) || (args.first == "--capture" && a.count == 5),
          let pid = Int32(a[0]), pid > 0,
          let wid = UInt32(a[videoOnly ? 1 : 2]), wid > 0,
          let seconds = Double(a[videoOnly ? 2 : 3]), seconds.isFinite, seconds >= 1, seconds <= 600,
          a.last!.hasPrefix("/") else {
        throw CaptureError("Usage: --capture GAME_PID VNTTS_PID WINDOW_ID SECONDS(1..600) NEW_ABSOLUTE_DIR | --video-only PID WINDOW_ID SECONDS NEW_ABSOLUTE_DIR | --selfcheck | --writer-selfcheck NEW_ABSOLUTE_DIR")
    }
    let ttsPID = videoOnly ? nil : Int32(a[1])
    guard videoOnly || (ttsPID != nil && ttsPID! > 0 && ttsPID != pid) else { throw CaptureError("Distinct explicit VNTTS PID required") }
    _ = NSApplication.shared
    guard CGPreflightScreenCaptureAccess() else { throw CaptureError("Screen Recording permission missing; no permission prompt or capture started") }
    let content = try await SCShareableContent.excludingDesktopWindows(true, onScreenWindowsOnly: false)
    guard let window = content.windows.first(where: { $0.windowID == wid && $0.owningApplication?.processID == pid }) else {
        throw CaptureError("Selected PID/window unavailable; refusing wider capture")
    }
    var ttsStream: SCStream?
    let root = URL(fileURLWithPath: a.last!, isDirectory: true)
    guard mkdir(root.path, 0o700) == 0 else { throw CaptureError("Output must be new with existing parent: errno \(errno)") }
    let width = max(2, Int(window.frame.width) / 2 * 2), height = max(2, Int(window.frame.height) / 2 * 2)
    let queue = DispatchQueue(label: "bounded.evidence.capture", autoreleaseFrequency: .workItem)
    let game = try Recorder(label: "game", root: root, queue: queue, width: width, height: height, requiresAudio: !videoOnly)
    let tts = try ttsPID.map { _ in try Recorder(label: "vntts", root: root, queue: queue) }
    let config = SCStreamConfiguration()
    config.width = width; config.height = height
    config.minimumFrameInterval = CMTime(value: 1, timescale: 30)
    config.queueDepth = 3; config.showsCursor = false
    config.capturesAudio = !videoOnly; config.captureMicrophone = false
    config.sampleRate = 48000; config.channelCount = 2; config.excludesCurrentProcessAudio = true
    let gameStream = SCStream(filter: SCContentFilter(desktopIndependentWindow: window), configuration: config, delegate: game)
    try gameStream.addStreamOutput(game, type: .screen, sampleHandlerQueue: queue)
    if !videoOnly { try gameStream.addStreamOutput(game, type: .audio, sampleHandlerQueue: queue) }
    if let ttsPID, let tts {
        guard let app = content.applications.first(where: { $0.processID == ttsPID }),
              let display = content.displays.first(where: { $0.frame.intersects(window.frame) }) else {
            throw CaptureError("Explicit VNTTS PID or selected window display unavailable")
        }
        let audio = SCStreamConfiguration()
        audio.width = 2; audio.height = 2; audio.capturesAudio = true; audio.captureMicrophone = false
        audio.sampleRate = 48000; audio.channelCount = 2; audio.excludesCurrentProcessAudio = true
        ttsStream = SCStream(filter: SCContentFilter(display: display, including: [app], exceptingWindows: []), configuration: audio, delegate: tts)
        try ttsStream!.addStreamOutput(tts, type: .audio, sampleHandlerQueue: queue)
    }
    var metadata = targetMetadata(window)
    metadata.merge(["game_pid": pid, "game_window_id": wid, "vntts_pid": ttsPID as Any? ?? NSNull(),
        "requested_seconds": seconds, "video_size": [width, height], "clock_anchor": clockAnchor(),
        "microphone": false, "video_only": videoOnly,
        "audio_scope": videoOnly ? "none" : "selected game owning application and explicit VNTTS application in separate CAF streams",
        "status": "in_progress"]) { _, new in new }
    func save() throws { try JSONSerialization.data(withJSONObject: metadata, options: [.prettyPrinted, .sortedKeys]).write(to: root.appendingPathComponent("session.json"), options: .atomic) }
    try save()
    DispatchQueue.global().asyncAfter(deadline: .now() + seconds + 15) {
        FileHandle.standardError.write(Data("capture watchdog expired; session remains incomplete\n".utf8)); exit(2)
    }
    // SIGINT/SIGTERM request orderly Stop; the watchdog also bounds stalled native stop/finalization.
    signal(SIGINT, SIG_IGN); signal(SIGTERM, SIG_IGN)
    var stopRequested = false
    func boundStop() {
        DispatchQueue.global().asyncAfter(deadline: .now() + 10) {
            FileHandle.standardError.write(Data("Stop watchdog expired; outputs remain incomplete\n".utf8)); exit(2)
        }
    }
    let signals = [SIGINT, SIGTERM].map { value -> DispatchSourceSignal in
        let source = DispatchSource.makeSignalSource(signal: value, queue: .main)
        source.setEventHandler { stopRequested = true; boundStop() }; source.resume(); return source
    }
    defer { signals.forEach { $0.cancel() } }
    var errors: [String] = []
    var started = false, ttsStarted = false
    let begin = monotonicSeconds()
    var recordingBegin = begin
    var lastObservedFrame = window.frame
    var positionChangeCount = 0
    do {
        try await gameStream.startCapture(); started = true; recordingBegin = monotonicSeconds()
        if let ttsStream { try await ttsStream.startCapture(); ttsStarted = true }
        while !stopRequested && monotonicSeconds() - begin < seconds {
            try await Task.sleep(nanoseconds: 250_000_000)
            if let failure = queue.sync(execute: { game.failure ?? tts?.failure }) { throw CaptureError(failure) }
            let checked = checkSelectedWindow(CGWindowListCopyWindowInfo(.optionIncludingWindow, wid), windowID: wid, pid: pid, frame: lastObservedFrame)
            if !checked.accepted {
                var evidence = checked.evidence
                evidence["monotonic_s"] = monotonicSeconds()
                metadata["target_guard_failure"] = evidence
                try save()
                throw CaptureError("Selected window target guard failed: \(checked.reason); capture aborted")
            }
            // The stream owns a window ID, so an origin-only move cannot widen its scope.
            if checked.reason == "bounds-origin-changed" {
                var evidence = checked.evidence
                evidence["monotonic_s"] = monotonicSeconds()
                positionChangeCount += 1
                metadata["target_position_change_count"] = positionChangeCount
                if positionChangeCount == 1 { metadata["first_target_position_change"] = evidence }
                metadata["last_target_position_change"] = evidence
                metadata["target_position_changes_not_retained"] = max(0, positionChangeCount - 2)
                lastObservedFrame = checked.observedFrame!
                try save()
            }
        }
    } catch { errors.append(String(describing: error)) }
    let recordingSeconds = monotonicSeconds() - recordingBegin
    boundStop()
    if started { do { try await gameStream.stopCapture() } catch { errors.append("game Stop: \(error)") } }
    if ttsStarted, let ttsStream { do { try await ttsStream.stopCapture() } catch { errors.append("VNTTS Stop: \(error)") } }
    let videoSpan = queue.sync {
        game.firstVideoPTS.flatMap { first in game.lastVideoPTS.map { ($0 - first).seconds } } ?? 0
    }
    let videoCoverageComplete = recordingSeconds <= 5 || videoSpan >= recordingSeconds - 5
    for recorder in [game, tts].compactMap({ $0 }) {
        do { try await recorder.finish() } catch { errors.append("\(recorder.label) finish: \(error)") }
    }
    metadata["native_status"] = errors.isEmpty ? "succeeded" : "failed"
    metadata["status"] = errors.isEmpty ? (videoCoverageComplete ? "succeeded" : "incomplete") : "failed"
    metadata["video_change_coverage"] = videoCoverageComplete ? "covered" : "static_or_incomplete"
    metadata["video_span_seconds"] = videoSpan
    metadata["errors"] = errors
    metadata["stop_requested"] = stopRequested
    metadata["finished_at"] = ISO8601DateFormatter().string(from: Date())
    metadata["elapsed_seconds"] = monotonicSeconds() - begin
    try save()
    if !errors.isEmpty { throw CaptureError(errors.joined(separator: "; ")) }
    guard videoCoverageComplete else {
        throw CaptureError("Video change coverage unverified: \(videoSpan)s across \(recordingSeconds)s capture; static or incomplete, not a proven encoder failure")
    }
    print("capture complete: \(root.path)")
}
