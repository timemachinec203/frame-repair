// Draws the app icon (film strip on a dark gradient) into an .iconset folder.
// Usage: swift tools/make-icon.swift <out.iconset>
import AppKit

let out = URL(fileURLWithPath: CommandLine.arguments[1])
try? FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)

func draw(_ px: Int) -> Data {
    let s = CGFloat(px)
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8,
                               samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                               colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)

    // macOS icon grid: 824/1024 rounded square, centred.
    let inset = s * 100 / 1024
    let rect = NSRect(x: inset, y: inset, width: s - 2 * inset, height: s - 2 * inset)
    let shape = NSBezierPath(roundedRect: rect, xRadius: rect.width * 0.225, yRadius: rect.width * 0.225)
    NSGradient(colors: [NSColor(calibratedRed: 0.10, green: 0.13, blue: 0.24, alpha: 1),
                        NSColor(calibratedRed: 0.20, green: 0.28, blue: 0.55, alpha: 1)])!
        .draw(in: shape, angle: 90)

    // Three frames, the middle one "rebuilt" (accent), with motion lines.
    let fw = rect.width * 0.22, fh = rect.height * 0.30, gap = rect.width * 0.04
    let y = rect.midY - fh / 2
    let x0 = rect.midX - (3 * fw + 2 * gap) / 2
    for i in 0..<3 {
        let r = NSRect(x: x0 + CGFloat(i) * (fw + gap), y: y, width: fw, height: fh)
        let p = NSBezierPath(roundedRect: r, xRadius: fw * 0.12, yRadius: fw * 0.12)
        (i == 1 ? NSColor(calibratedRed: 0.45, green: 0.78, blue: 1.0, alpha: 1)
                : NSColor(calibratedWhite: 0.92, alpha: 1)).setFill()
        p.fill()
    }
    // sprocket holes
    NSColor(calibratedWhite: 1, alpha: 0.55).setFill()
    let hole = rect.width * 0.035
    for row in [y - hole * 2.2, y + fh + hole * 1.2] {
        var x = x0
        while x < x0 + 3 * fw + 2 * gap - hole {
            NSBezierPath(roundedRect: NSRect(x: x, y: row, width: hole, height: hole), xRadius: hole * 0.25, yRadius: hole * 0.25).fill()
            x += hole * 2
        }
    }
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

for (name, px) in [("16x16", 16), ("16x16@2x", 32), ("32x32", 32), ("32x32@2x", 64), ("128x128", 128),
                   ("128x128@2x", 256), ("256x256", 256), ("256x256@2x", 512), ("512x512", 512), ("512x512@2x", 1024)] {
    try! draw(px).write(to: out.appendingPathComponent("icon_\(name).png"))
}
