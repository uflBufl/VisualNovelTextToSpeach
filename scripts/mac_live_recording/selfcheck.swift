import AVFoundation
import Foundation
import CoreVideo

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
