import CoreVideo
import Foundation

/// Averages several same-size BGRA frames into one: the "open shutter" for --shutter.
/// Same maths as ffmpeg's tmix (equal weights, gamma-encoded values), which is what
/// the approved two-step 240 fps -> 24 fps test used. Rows are split across CPU cores.
final class FrameAverager {
    private var pool: CVPixelBufferPool?
    private var poolSize = (0, 0)

    private func makeOutput(width: Int, height: Int) throws -> CVPixelBuffer {
        if pool == nil || poolSize != (width, height) {
            let attrs: [String: Any] = [
                kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
                kCVPixelBufferWidthKey as String: width,
                kCVPixelBufferHeightKey as String: height,
                kCVPixelBufferIOSurfacePropertiesKey as String: [String: Any](),
                kCVPixelBufferMetalCompatibilityKey as String: true,
            ]
            var p: CVPixelBufferPool?
            CVPixelBufferPoolCreate(nil, nil, attrs as CFDictionary, &p)
            pool = p
            poolSize = (width, height)
        }
        var out: CVPixelBuffer?
        guard let pool, CVPixelBufferPoolCreatePixelBuffer(nil, pool, &out) == kCVReturnSuccess, let out else {
            throw RepairError("Could not allocate a \(width)x\(height) frame for motion blur")
        }
        return out
    }

    func average(_ frames: [CVPixelBuffer]) throws -> CVPixelBuffer {
        precondition(!frames.isEmpty)
        let w = CVPixelBufferGetWidth(frames[0]), h = CVPixelBufferGetHeight(frames[0])
        for f in frames {
            guard CVPixelBufferGetWidth(f) == w, CVPixelBufferGetHeight(f) == h,
                  CVPixelBufferGetPixelFormatType(f) == kCVPixelFormatType_32BGRA
            else { throw RepairError("Motion blur needs same-size BGRA frames") }
        }
        let out = try makeOutput(width: w, height: h)

        for f in frames { CVPixelBufferLockBaseAddress(f, .readOnly) }
        CVPixelBufferLockBaseAddress(out, [])
        defer {
            CVPixelBufferUnlockBaseAddress(out, [])
            for f in frames { CVPixelBufferUnlockBaseAddress(f, .readOnly) }
        }

        let srcs = frames.map { CVPixelBufferGetBaseAddress($0)!.assumingMemoryBound(to: UInt8.self) }
        let srcRows = frames.map { CVPixelBufferGetBytesPerRow($0) }
        let dst = CVPixelBufferGetBaseAddress(out)!.assumingMemoryBound(to: UInt8.self)
        let dstRow = CVPixelBufferGetBytesPerRow(out)
        let n = UInt32(frames.count)
        let half = n / 2
        let rowBytes = w * 4

        // Split rows into bands, one task per band.
        let bands = min(h, ProcessInfo.processInfo.activeProcessorCount * 4)
        DispatchQueue.concurrentPerform(iterations: bands) { band in
            let y0 = band * h / bands, y1 = (band + 1) * h / bands
            var acc = [UInt32](repeating: 0, count: rowBytes)
            acc.withUnsafeMutableBufferPointer { a in
                for y in y0..<y1 {
                    for i in 0..<rowBytes { a[i] = half }           // round to nearest
                    for (s, stride) in zip(srcs, srcRows) {
                        let row = s + y * stride
                        for i in 0..<rowBytes { a[i] &+= UInt32(row[i]) }
                    }
                    let o = dst + y * dstRow
                    for i in 0..<rowBytes { o[i] = UInt8(truncatingIfNeeded: a[i] / n) }
                }
            }
        }
        return out
    }
}
