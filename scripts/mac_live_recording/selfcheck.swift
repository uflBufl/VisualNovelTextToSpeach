import AVFoundation
import Foundation
import CoreVideo
import CoreGraphics

func targetGuardSelfcheck() throws {
    let frame = CGRect(x: 10, y: 20, width: 640, height: 400)
    let windowID: CGWindowID = 123
    let pid: pid_t = 456
    let row: [String: Any] = [
        kCGWindowNumber as String: windowID,
        kCGWindowOwnerPID as String: pid,
        kCGWindowBounds as String: frame.dictionaryRepresentation,
        kCGWindowName as String: "unrelated-title-must-never-be-retained"
    ]
    var checks = 0
    func check(_ lookup: Any?, _ reason: String) throws {
        let result = checkSelectedWindow(lookup, windowID: windowID, pid: pid, frame: frame)
        guard result.reason == reason, result.accepted == (reason == "unchanged" || reason == "bounds-origin-changed") else {
            throw CaptureError("Target selfcheck expected \(reason), received \(result.reason)")
        }
        let encoded = try JSONSerialization.data(withJSONObject: result.evidence, options: [.sortedKeys])
        guard let evidence = String(data: encoded, encoding: .utf8), !evidence.contains("unrelated-title") else {
            throw CaptureError("Target selfcheck retained unrelated metadata")
        }
        checks += 1
    }
    func changed(_ key: String, _ value: Any?) -> [String: Any] {
        var result = row
        result[key] = value
        return result
    }
    try check([row], "unchanged")
    try check(nil, "lookup-unavailable")
    try check("not a lookup", "lookup-malformed")
    try check([], "lookup-empty")
    try check(["not a row"], "lookup-entry-malformed")
    try check([row, row], "lookup-entry-count-mismatch")
    try check([changed(kCGWindowNumber as String, nil)], "window-id-missing")
    try check([changed(kCGWindowNumber as String, 124)], "window-id-mismatch")
    try check([changed(kCGWindowOwnerPID as String, nil)], "owner-pid-missing")
    try check([changed(kCGWindowOwnerPID as String, 457)], "owner-pid-mismatch")
    let invalidIdentities: [Any] = [true, 0, -1, 1.5, Double.infinity, Double(UInt32.max) + 1, "123"]
    for invalid in invalidIdentities {
        try check([changed(kCGWindowNumber as String, invalid)], "window-id-malformed")
        try check([changed(kCGWindowOwnerPID as String, invalid)], "owner-pid-malformed")
    }
    try check([changed(kCGWindowBounds as String, nil)], "bounds-missing")
    try check([changed(kCGWindowBounds as String, "not bounds")], "bounds-malformed")
    let malformedBounds: [[String: Any]] = [[:], ["X": "bad", "Y": 20, "Width": 640, "Height": 400], ["X": true, "Y": 20, "Width": 640, "Height": 400], ["X": Double.infinity, "Y": 20, "Width": 640, "Height": 400]]
    for malformed in malformedBounds {
        try check([changed(kCGWindowBounds as String, malformed)], "bounds-malformed")
    }
    try check([changed(kCGWindowBounds as String, CGRect(x: 10, y: 20, width: 0, height: 400).dictionaryRepresentation)], "bounds-malformed")
    try check([changed(kCGWindowBounds as String, ["X": 10, "Y": 20, "Width": -640, "Height": 400])], "bounds-malformed")
    try check([changed(kCGWindowBounds as String, frame.offsetBy(dx: 1, dy: 0).dictionaryRepresentation)], "bounds-origin-changed")
    try check([changed(kCGWindowBounds as String, frame.offsetBy(dx: -2000, dy: 1000).dictionaryRepresentation)], "bounds-origin-changed")
    try check([changed(kCGWindowBounds as String, CGRect(x: 10, y: 20, width: 641, height: 400).dictionaryRepresentation)], "bounds-size-changed")
    try check([changed(kCGWindowBounds as String, CGRect(x: 11, y: 20, width: 641, height: 400).dictionaryRepresentation)], "bounds-origin-and-size-changed")
    let mismatch = checkSelectedWindow([changed(kCGWindowOwnerPID as String, 457)], windowID: windowID, pid: pid, frame: frame)
    let original = mismatch.evidence["original_target"] as! [String: Any]
    let observed = mismatch.evidence["observed_target"] as! [String: Any]
    guard (original["pid"] as? NSNumber)?.int32Value == pid,
          (observed["pid"] as? NSNumber)?.int32Value == 457,
          observed["bounds"] is [String: Double] else { throw CaptureError("Target selfcheck lost original/observed identity") }
    print("target guard selfcheck passed: \(checks) fail-closed predicate checks; no window lookup or capture")
}

func writerSelfcheck(root: URL) async throws {
    guard root.path.hasPrefix("/"), mkdir(root.path, 0o700) == 0 else { throw CaptureError("Selfcheck output must be a new absolute directory") }
    let queue = DispatchQueue(label: "synthetic.writer.check", autoreleaseFrequency: .workItem)
    let recorder = try Recorder(label: "synthetic", root: root, queue: queue, width: 320, height: 180, requiresAudio: false)
    for index in 0..<180 {
        try queue.sync {
            try autoreleasepool {
                var pixel: CVPixelBuffer?
                guard CVPixelBufferCreate(nil, 320, 180, kCVPixelFormatType_32BGRA, [kCVPixelBufferIOSurfacePropertiesKey: [:]] as CFDictionary, &pixel) == kCVReturnSuccess, let pixel else { throw CaptureError("Synthetic pixel allocation failed") }
                CVPixelBufferLockBaseAddress(pixel, [])
                let bytes = CVPixelBufferGetBaseAddress(pixel)!.assumingMemoryBound(to: UInt8.self)
                for row in 0..<180 { for column in 0..<320 {
                    let offset = row * CVPixelBufferGetBytesPerRow(pixel) + column * 4
                    bytes[offset] = index >= 120 ? 255 : 0
                    bytes[offset + 1] = index >= 60 && index < 120 ? 255 : 0
                    bytes[offset + 2] = index < 60 ? 255 : 0; bytes[offset + 3] = 255
                } }
                CVPixelBufferUnlockBaseAddress(pixel, [])
                var format: CMVideoFormatDescription?
                CMVideoFormatDescriptionCreateForImageBuffer(allocator: nil, imageBuffer: pixel, formatDescriptionOut: &format)
                var timing = CMSampleTimingInfo(duration: CMTime(value: 1, timescale: 30), presentationTimeStamp: CMTime(value: Int64(index), timescale: 30), decodeTimeStamp: .invalid)
                var sample: CMSampleBuffer?
                guard CMSampleBufferCreateReadyWithImageBuffer(allocator: nil, imageBuffer: pixel, formatDescription: format!, sampleTiming: &timing, sampleBufferOut: &sample) == noErr, let sample else { throw CaptureError("Synthetic sample allocation failed") }
                if !recorder.videoStarted {
                    guard recorder.writer!.startWriting() else { throw recorder.writer!.error! }
                    recorder.writer!.startSession(atSourceTime: sample.presentationTimeStamp); recorder.videoStarted = true
                }
                guard recorder.videoInput!.isReadyForMoreMediaData else { throw CaptureError("Synthetic encoder backpressure") }
                guard recorder.videoInput!.append(sample) else { throw recorder.writer!.error! }
                recorder.videoFrames += 1
            }
        }
        try await Task.sleep(nanoseconds: 33_333_333)
    }
    try await recorder.finish()
    let asset = AVURLAsset(url: root.appendingPathComponent("window.mov"))
    guard let track = try await asset.loadTracks(withMediaType: .video).first else { throw CaptureError("Encoded video track missing") }
    let reader = try AVAssetReader(asset: asset)
    let output = AVAssetReaderTrackOutput(track: track, outputSettings: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
    reader.add(output)
    guard reader.startReading() else { throw reader.error! }
    var decoded = 0
    var colors = Set<Int>()
    while let sample = output.copyNextSampleBuffer(), let pixel = sample.imageBuffer {
        CVPixelBufferLockBaseAddress(pixel, .readOnly)
        let p = CVPixelBufferGetBaseAddress(pixel)!.assumingMemoryBound(to: UInt8.self)
        colors.insert(p[0] > 150 ? 0 : p[1] > 150 ? 1 : 2)
        CVPixelBufferUnlockBaseAddress(pixel, .readOnly)
        guard abs(sample.presentationTimeStamp.seconds - Double(decoded) / 30) < 0.001 else { throw CaptureError("Decoded PTS changed") }
        decoded += 1
    }
    guard reader.status == .completed, decoded == 180, colors.count == 3 else { throw CaptureError("Synthetic decode failed: frames=\(decoded), colors=\(colors)") }
    print("writer selfcheck passed: 180 real decoded frames, 3 changing scenes, original PTS; no screen capture/playback")
}
