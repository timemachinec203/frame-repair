import Foundation
import FrameRepairKit

/// Runs the bundled `rife-repair` engine as a child process and streams its output
/// back line by line.
final class EngineProcess: @unchecked Sendable {
    private let process = Process()
    private let lock = NSLock()
    private var outBuffer = LineBuffer()
    private var errBuffer = LineBuffer()
    private var openStreams = 2
    private var exited = false
    private var continuation: CheckedContinuation<Int32, Never>?

    /// The engine lives in the app at Contents/Resources/engine/rife-repair, next to
    /// its model and shader bundles. During development it sits next to this binary.
    static var engineURL: URL? {
        if let u = Bundle.main.url(forResource: "rife-repair", withExtension: nil, subdirectory: "engine") { return u }
        let dev = Bundle.main.executableURL?.deletingLastPathComponent().appendingPathComponent("rife-repair")
        return dev.flatMap { FileManager.default.isExecutableFile(atPath: $0.path) ? $0 : nil }
    }

    /// Runs to completion. `onLine` gets every stdout/stderr line (isError = stderr).
    func run(arguments: [String], onLine: @escaping @Sendable (String, Bool) -> Void) async -> Int32 {
        guard let exe = Self.engineURL else {
            onLine("The processing engine is missing from the app.", true)
            return -1
        }
        process.executableURL = exe
        process.arguments = arguments
        let out = Pipe(), err = Pipe()
        process.standardOutput = out
        process.standardError = err
        process.standardInput = FileHandle.nullDevice

        func attach(_ pipe: Pipe, isError: Bool) {
            pipe.fileHandleForReading.readabilityHandler = { [weak self] h in
                guard let self else { return }
                let data = h.availableData
                if data.isEmpty {
                    h.readabilityHandler = nil
                    let tail = self.withLock { isError ? self.errBuffer.flush() : self.outBuffer.flush() }
                    tail.forEach { onLine($0, isError) }
                    self.finish(stream: true)
                    return
                }
                let text = String(decoding: data, as: UTF8.self)
                let lines = self.withLock { isError ? self.errBuffer.feed(text) : self.outBuffer.feed(text) }
                lines.forEach { onLine($0, isError) }
            }
        }
        attach(out, isError: false)
        attach(err, isError: true)
        process.terminationHandler = { [weak self] _ in self?.finish(stream: false) }

        return await withCheckedContinuation { c in
            withLock { continuation = c }
            do {
                try process.run()
            } catch {
                onLine("Could not start the engine: \(error.localizedDescription)", true)
                withLock { continuation = nil }
                c.resume(returning: -1)
            }
        }
    }

    func cancel() {
        if process.isRunning { process.terminate() }
    }

    private func withLock<T>(_ body: () -> T) -> T { lock.lock(); defer { lock.unlock() }; return body() }

    /// Resume once the process has exited and both pipes reached end-of-file.
    private func finish(stream: Bool) {
        let c: CheckedContinuation<Int32, Never>? = withLock {
            if stream { openStreams -= 1 } else { exited = true }
            guard exited && openStreams == 0, let c = continuation else { return nil }
            continuation = nil
            return c
        }
        c?.resume(returning: process.terminationStatus)
    }
}
