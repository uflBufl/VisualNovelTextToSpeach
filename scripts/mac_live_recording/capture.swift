import AppKit
import AVFoundation
import ScreenCaptureKit
import CoreGraphics
import Foundation
import Darwin

func targetMetadata(_ window: SCWindow) -> [String: Any] {
    ["window_id": window.windowID, "pid": window.owningApplication!.processID,
     "title": window.title as Any? ?? NSNull(),
     "bounds": ["x": window.frame.minX, "y": window.frame.minY, "width": window.frame.width, "height": window.frame.height],
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
    do {
        try await gameStream.startCapture(); started = true; recordingBegin = monotonicSeconds()
        if let ttsStream { try await ttsStream.startCapture(); ttsStarted = true }
        while !stopRequested && monotonicSeconds() - begin < seconds {
            try await Task.sleep(nanoseconds: 250_000_000)
            if let failure = queue.sync(execute: { game.failure ?? tts?.failure }) { throw CaptureError(failure) }
            let current = (CGWindowListCopyWindowInfo(.optionIncludingWindow, wid) as? [[String: Any]])?.first
            guard let current, (current[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value == pid,
                  let bounds = current[kCGWindowBounds as String] as? [String: Any],
                  let frame = CGRect(dictionaryRepresentation: bounds as CFDictionary), frame == window.frame else {
                throw CaptureError("Selected window disappeared, changed owner or bounds; capture aborted")
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
