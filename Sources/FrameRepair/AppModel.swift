import AppKit
import Foundation
import FrameRepairKit
import Observation

@Observable @MainActor
final class ClipJob: Identifiable {
    enum Status: Equatable {
        case added                     // dropped in, not analysed yet
        case analysisQueued
        case analyzing
        case analyzed                  // ready to process
        case analysisFailed(String)
        case queued                    // waiting for a free processing slot
        case running
        case done
        case failed(String)
        case cancelled
    }

    let id = UUID()
    let url: URL
    var status: Status = .added
    var progress = 0.0
    var analysis: ClipAnalysis?
    var output: URL?
    var log: [String] = []
    var elapsed: TimeInterval?
    /// Settings captured when Process was pressed, so changing the panel later
    /// doesn't affect clips already queued.
    var settings: RepairSettings?
    fileprivate var engine: EngineProcess?
    /// File being written right now (deleted if the app quits mid-render).
    fileprivate(set) var pendingOutput: URL?
    fileprivate var cancelRequested = false

    init(url: URL) { self.url = url }

    var name: String { url.lastPathComponent }
    var isFinished: Bool { status == .done }
    var canAnalyze: Bool {
        switch status {
        case .added, .analysisFailed: return true
        default: return false
        }
    }
    var canProcess: Bool {
        switch status {
        case .analyzed, .failed, .cancelled: return true
        default: return false
        }
    }
    var isBusy: Bool { [.analysisQueued, .analyzing, .queued, .running].contains(status) }
}

@Observable @MainActor
final class AppModel {
    var jobs: [ClipJob] = []
    var selection: Set<ClipJob.ID> = []
    var showImporter = false
    var logJob: ClipJob?

    var settings: RepairSettings { didSet { save() } }
    /// Named look, or .manual. Picking a named preset applies it; Manual leaves the
    /// current values in place and unlocks every control.
    var preset: Preset {
        didSet {
            UserDefaults.standard.set(preset.rawValue, forKey: "preset")
            preset.apply(to: &settings)
        }
    }
    /// Clips processed at the same time (1-3). 2 measured ~17% faster than 1 on the test Mac
    /// (the GPU idles while one clip decodes, blends or encodes); 3 is for bigger GPUs.
    var clipsAtOnce: Int { didSet { UserDefaults.standard.set(clipsAtOnce, forKey: "clipsAtOnce") } }

    private(set) var processingCount = 0
    private(set) var analysisRunning = false

    static let movieExtensions: Set<String> = ["mov", "mp4", "m4v"]

    init() {
        var s = RepairSettings.filmLook
        if let data = UserDefaults.standard.data(forKey: "settings"),
           let saved = try? JSONDecoder().decode(RepairSettings.self, from: data) {
            s = saved
        }
        let p = UserDefaults.standard.string(forKey: "preset").flatMap(Preset.init(rawValue:))
            ?? Preset.matching(s) ?? .manual
        p.apply(to: &s)
        settings = s
        preset = p
        let n = UserDefaults.standard.integer(forKey: "clipsAtOnce")
        clipsAtOnce = n == 0 ? 2 : min(max(n, 1), 3)
    }

    private func save() {
        if let data = try? JSONEncoder().encode(settings) { UserDefaults.standard.set(data, forKey: "settings") }
    }

    var isBusy: Bool { processingCount > 0 || analysisRunning || jobs.contains { $0.isBusy } }

    // MARK: Clips

    /// Adds clips (or folders of clips) and selects the new ones. Nothing starts.
    func add(_ urls: [URL]) {
        var expanded: [URL] = []
        for u in urls {
            var isDir: ObjCBool = false
            if FileManager.default.fileExists(atPath: u.path, isDirectory: &isDir), isDir.boolValue {
                let items = (try? FileManager.default.contentsOfDirectory(at: u, includingPropertiesForKeys: nil)) ?? []
                expanded += items.sorted { $0.lastPathComponent < $1.lastPathComponent }
            } else {
                expanded.append(u)
            }
        }
        var added: Set<ClipJob.ID> = []
        for u in expanded where Self.movieExtensions.contains(u.pathExtension.lowercased()) {
            guard !jobs.contains(where: { $0.url == u }) else { continue }
            let job = ClipJob(url: u)
            jobs.append(job)
            added.insert(job.id)
        }
        if !added.isEmpty { selection = added }
    }

    func remove(_ ids: Set<ClipJob.ID>) {
        jobs.removeAll { ids.contains($0.id) && !$0.isBusy }
        selection = selection.filter { id in jobs.contains { $0.id == id } }
    }

    func clearFinished() { jobs.removeAll { $0.isFinished } }

    private var selectedJobs: [ClipJob] { jobs.filter { selection.contains($0.id) } }
    var analyzeTargets: [ClipJob] { selectedJobs.filter(\.canAnalyze) }
    var processTargets: [ClipJob] { selectedJobs.filter(\.canProcess) }
    /// Selected clips that still need analysing before they can be processed.
    var selectedNeedingAnalysis: Int { selectedJobs.filter { $0.canAnalyze || $0.status == .analysisQueued || $0.status == .analyzing }.count }

    // MARK: Step 1: analyse

    func startAnalysis(_ targets: [ClipJob]? = nil) {
        for job in targets ?? analyzeTargets {
            job.status = .analysisQueued
            job.analysis = nil
        }
        if !analysisRunning { Task { await runAnalyses() } }
    }

    /// Re-analyse a clip that was already analysed (context menu).
    func reanalyze(_ job: ClipJob) {
        guard !job.isBusy else { return }
        startAnalysis([job])
    }

    private func runAnalyses() async {
        analysisRunning = true
        while let job = jobs.first(where: { $0.status == .analysisQueued }) {
            job.status = .analyzing
            job.progress = 0
            let engine = EngineProcess()
            job.engine = engine
            let collected = LockedLines()
            let code = await engine.run(arguments: RepairSettings.analysisArguments(input: job.url)) { line, _ in
                if case .progress(_, let done, let total) = EngineEvent.parse(line) {
                    Task { @MainActor in job.progress = overallFraction(phase: "analyse", done: done, total: total, twoPass: false) }
                } else {
                    collected.append(line)
                }
            }
            job.engine = nil
            if job.cancelRequested {
                job.cancelRequested = false
                job.status = .added
            } else if code == 0 {
                job.analysis = ClipAnalysis.parse(collected.all.joined(separator: "\n"))
                job.status = .analyzed
            } else {
                job.log.append(contentsOf: collected.all)
                let reason = collected.all.last(where: { $0.hasPrefix("Error") }) ?? "Could not read this clip."
                job.status = .analysisFailed(reason.replacingOccurrences(of: "Error: ", with: ""))
            }
            job.progress = 0
        }
        analysisRunning = false
    }

    // MARK: Step 2: process

    func startProcessing(_ targets: [ClipJob]? = nil) {
        let snapshot = settings
        for job in targets ?? processTargets {
            job.settings = snapshot
            job.status = .queued
            job.progress = 0
            job.output = nil
            job.elapsed = nil
        }
        pump()
    }

    /// Process a finished clip again (context menu).
    func processAgain(_ job: ClipJob) {
        guard job.status == .done else { return }
        startProcessing([job])
    }

    /// Fill free processing slots from the queue, in list order.
    private func pump() {
        while processingCount < clipsAtOnce, let job = jobs.first(where: { $0.status == .queued }) {
            processingCount += 1
            job.status = .running
            Task {
                await render(job)
                processingCount -= 1
                pump()
                if processingCount == 0 && !jobs.contains(where: { $0.status == .queued }) && !NSApp.isActive {
                    NSApp.requestUserAttention(.informationalRequest)
                }
            }
        }
    }

    private func render(_ job: ClipJob) async {
        let settings = job.settings ?? self.settings
        let output = settings.outputURL(for: job.url, exists: { [weak self] url in
            FileManager.default.fileExists(atPath: url.path) || (self?.reserved.contains(url) ?? false)
        })
        reserved.insert(output)                      // two clips at once must not pick the same name
        defer { reserved.remove(output) }
        if let folder = settings.outputFolder {
            try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        }
        let args = settings.arguments(input: job.url, output: output)
        job.log = ["$ rife-repair " + args.joined(separator: " ")]
        let twoPass = settings.effectiveTiming == .even
        let started = Date()
        let engine = EngineProcess()
        job.engine = engine
        job.pendingOutput = output

        let code = await engine.run(arguments: args) { line, isError in
            Task { @MainActor in
                switch EngineEvent.parse(line) {
                case .progress(let phase, let done, let total):
                    job.progress = overallFraction(phase: phase, done: done, total: total, twoPass: twoPass)
                case .output(let path):
                    job.output = URL(fileURLWithPath: path)
                case .text(let t):
                    job.log.append(isError ? "! " + t : t)
                }
            }
        }
        await Task.yield()                            // let queued line updates land
        job.engine = nil
        job.pendingOutput = nil
        job.elapsed = Date().timeIntervalSince(started)

        if job.cancelRequested {
            job.cancelRequested = false
            job.status = .cancelled
            try? FileManager.default.removeItem(at: output)      // unfinished file
        } else if code == 0 {
            job.status = .done
            job.progress = 1
            job.output = job.output ?? output
        } else {
            let reason = job.log.last(where: { $0.hasPrefix("! ") }).map { String($0.dropFirst(2)) }
                ?? "The engine stopped with code \(code)."
            job.status = .failed(reason.replacingOccurrences(of: "Error: ", with: ""))
            try? FileManager.default.removeItem(at: output)
        }
    }
    private var reserved: Set<URL> = []

    // MARK: Stop

    /// Stops everything: running clips are cancelled (unfinished files deleted),
    /// queued clips go back to where they were.
    func stopAll() {
        for job in jobs {
            switch job.status {
            case .queued: job.status = .analyzed
            case .analysisQueued: job.status = .added
            case .running, .analyzing:
                job.cancelRequested = true
                job.engine?.cancel()
            default: break
            }
        }
    }

    /// Quitting: stop the engines and delete their unfinished files right away.
    func stopForQuit() {
        stopAll()
        for job in jobs { if let o = job.pendingOutput { try? FileManager.default.removeItem(at: o) } }
    }

    // MARK: Self-test (FRAME_REPAIR_SELFTEST=<clip> FRAME_REPAIR_SELFTEST_OUT=<folder>)

    /// Drives the real two-step flow without clicking: add, Analyze, Process with the
    /// film-look preset, report what the UI would show, quit. Saved settings are kept.
    func selfTest(clips: [URL], outFolder: URL?) async {
        func say(_ s: String) { FileHandle.standardOutput.write((s + "\n").data(using: .utf8)!) }
        say("preset on launch: \(preset.label)")
        add(clips)
        let targets = jobs
        say("after drop: \(targets.map { "\($0.status)" }.joined(separator: ", ")), selected \(selection.count)")
        say("analyze enabled: \(!analyzeTargets.isEmpty), process enabled: \(!processTargets.isEmpty)")

        startAnalysis()
        while targets.contains(where: { $0.isBusy }) { try? await Task.sleep(for: .milliseconds(100)) }
        for j in targets { say("analysis \(j.name): \(j.analysis?.summary ?? "\(j.status)")") }
        say("process enabled: \(!processTargets.isEmpty)")

        let saved = settings
        var test = RepairSettings.filmLook
        test.outputFolder = outFolder
        settings = test
        let started = Date()
        startProcessing()
        settings = saved

        var maxParallel = 0
        var seen: [UUID: [Double]] = [:]
        while targets.contains(where: { $0.isBusy }) {
            maxParallel = max(maxParallel, targets.filter { $0.status == .running }.count)
            for j in targets where seen[j.id]?.last != j.progress { seen[j.id, default: []].append(j.progress) }
            try? await Task.sleep(for: .milliseconds(50))
        }
        for j in targets {
            let p = seen[j.id] ?? []
            let mono = zip(p, p.dropFirst()).allSatisfy { $0 <= $1 }
            say("\(j.name): \(j.status) -> \(j.output?.lastPathComponent ?? "none")  [\(p.count) updates, monotonic=\(mono), \(String(format: "%.1f", j.elapsed ?? 0)) s]")
        }
        say(String(format: "max clips at once: %d, total %.1f s", maxParallel, Date().timeIntervalSince(started)))
        exit(targets.allSatisfy(\.isFinished) ? 0 : 1)
    }

    // MARK: Finder

    func reveal(_ url: URL) { NSWorkspace.shared.activateFileViewerSelecting([url]) }
    /// Opens the file in its default app (usually QuickTime Player).
    func open(_ url: URL) { NSWorkspace.shared.open(url) }
}

/// Thread-safe line collector for the analysis callback.
final class LockedLines: @unchecked Sendable {
    private let lock = NSLock()
    private var lines: [String] = []
    func append(_ l: String) { lock.lock(); lines.append(l); lock.unlock() }
    var all: [String] { lock.lock(); defer { lock.unlock() }; return lines }
}
