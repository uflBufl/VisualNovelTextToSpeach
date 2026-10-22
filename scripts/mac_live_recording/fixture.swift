import AppKit
import Foundation
final class FixtureWindow: NSWindow {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}
final class Scene: NSView {
    let start = ProcessInfo.processInfo.systemUptime
    override func draw(_ dirtyRect: NSRect) {
        let elapsed = Int(ProcessInfo.processInfo.systemUptime - start)
        let phase = elapsed < 25 ? 0 : elapsed < 50 ? 1 : 2
        [NSColor.systemRed, .systemGreen, .systemBlue][phase].setFill()
        bounds.fill()
        let text = "VNTTS recording fixture\n\(["BEGIN", "MIDDLE", "END"][phase])  second \(elapsed)"
        let style = NSMutableParagraphStyle(); style.alignment = .center
        (text as NSString).draw(in: NSRect(x: 10, y: 100, width: 620, height: 180), withAttributes: [
            .font: NSFont.monospacedSystemFont(ofSize: 34, weight: .bold), .foregroundColor: NSColor.white, .paragraphStyle: style])
    }
}
@main @MainActor struct Main {
    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        let window = FixtureWindow(contentRect: NSRect(x: 60, y: 60, width: 640, height: 400), styleMask: [.titled], backing: .buffered, defer: false)
        window.title = "VNTTS generated recorder fixture"
        let scene = Scene(frame: window.contentView!.bounds)
        window.contentView = scene
        window.orderFrontRegardless()
        print("{\"pid\":\(ProcessInfo.processInfo.processIdentifier),\"window_id\":\(window.windowNumber)}")
        fflush(stdout)
        let timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { _ in scene.needsDisplay = true; scene.display() }
        let stop = Timer.scheduledTimer(withTimeInterval: 90, repeats: false) { _ in app.terminate(nil) }
        withExtendedLifetime((window, timer, stop)) { app.run() }
    }
}
