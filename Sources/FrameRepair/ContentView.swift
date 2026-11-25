import FrameRepairKit
import SwiftUI
import UniformTypeIdentifiers

struct ContentView: View {
    @Environment(AppModel.self) private var model
    @State private var dropTargeted = false

    var body: some View {
        @Bindable var model = model
        HSplitView {
            VStack(spacing: 0) {
                clipList
                Divider()
                statusBar
            }
            .frame(minWidth: 520, maxWidth: .infinity, maxHeight: .infinity)
            SettingsPanel()
                .frame(minWidth: 320, idealWidth: 340, maxWidth: 420, maxHeight: .infinity)
        }
        .toolbar { toolbar }
        .fileImporter(isPresented: $model.showImporter,
                      allowedContentTypes: [.movie, .folder],
                      allowsMultipleSelection: true) { result in
            if case .success(let urls) = result { model.add(urls) }
        }
        .sheet(item: $model.logJob) { job in LogSheet(job: job) }
    }

    // MARK: Clip list

    private var clipList: some View {
        @Bindable var model = model
        return Group {
            if model.jobs.isEmpty {
                ContentUnavailableView {
                    Label("Drop gameplay clips here", systemImage: "film.stack")
                } description: {
                    Text("Then select clips, click Analyze, and when that's done, click Process. Everything runs on this Mac.")
                } actions: {
                    Button("Add Clips…") { model.showImporter = true }
                }
            } else {
                List(selection: $model.selection) {
                    ForEach(model.jobs) { job in
                        ClipRow(job: job)
                            .tag(job.id)
                            .contextMenu { menu(for: job) }
                    }
                }
                .onDeleteCommand { model.remove(model.selection) }
            }
        }
        .overlay {
            if dropTargeted {
                RoundedRectangle(cornerRadius: 10)
                    .strokeBorder(Color.accentColor, lineWidth: 3)
                    .padding(6)
                    .allowsHitTesting(false)
            }
        }
        .dropDestination(for: URL.self) { urls, _ in
            model.add(urls)
            return true
        } isTargeted: { dropTargeted = $0 }
    }

    /// Counts plus a hint for the next step.
    private var statusBar: some View {
        let jobs = model.jobs
        let analyzed = jobs.filter { $0.analysis != nil }.count
        let done = jobs.filter(\.isFinished).count
        return HStack(spacing: 12) {
            Text(jobs.isEmpty ? "No clips" : "\(jobs.count) clip\(jobs.count == 1 ? "" : "s") · \(analyzed) analyzed · \(done) done")
            Spacer()
            Text(hint).foregroundStyle(.secondary)
        }
        .font(.caption)
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
    }

    private var hint: String {
        if model.jobs.isEmpty { return "" }
        if model.selection.isEmpty { return "Select clips (⌘A for all)" }
        if !model.analyzeTargets.isEmpty { return "Step 1: Analyze" }
        if model.selectedNeedingAnalysis > 0 { return "Analyzing…" }
        if !model.processTargets.isEmpty { return "Step 2: Process" }
        return ""
    }

    @ViewBuilder
    private func menu(for job: ClipJob) -> some View {
        if job.isFinished, let out = job.output {
            Button("Show Output in Finder") { model.reveal(out) }
            Button("Open Output") { model.open(out) }
            Divider()
        }
        Button("Show Original in Finder") { model.reveal(job.url) }
        Button("Open Original") { model.open(job.url) }
        Divider()
        Button("Analyze Again") { model.reanalyze(job) }
            .disabled(job.isBusy || job.canAnalyze)
        Button("Process Again") { model.processAgain(job) }
            .disabled(!job.isFinished)
        Button("Show Log") { model.logJob = job }
        Divider()
        Button("Remove from List") { model.remove([job.id]) }
            .disabled(job.isBusy)
    }

    // MARK: Toolbar

    @ToolbarContentBuilder
    private var toolbar: some ToolbarContent {
        ToolbarItemGroup(placement: .navigation) {
            Button { model.showImporter = true } label: { Label("Add Clips", systemImage: "plus") }
                .help("Add clips or a folder of clips")
            Button { model.remove(model.selection) } label: { Label("Remove", systemImage: "minus") }
                .disabled(model.selection.isEmpty)
                .help("Remove the selected clips from the list")
            Button { model.clearFinished() } label: { Label("Clear Finished", systemImage: "checkmark.circle") }
                .disabled(!model.jobs.contains { $0.isFinished })
                .help("Remove finished clips from the list")
        }
        ToolbarItemGroup(placement: .primaryAction) {
            if model.isBusy {
                Button { model.stopAll() } label: { Label("Stop", systemImage: "stop.fill") }
                    .help("Stop everything. Unfinished files are deleted; queued clips go back to ready.")
            }
            let a = model.analyzeTargets.count
            Button { model.startAnalysis() } label: {
                Label(a > 1 ? "Analyze \(a) Clips" : "Analyze", systemImage: "waveform.badge.magnifyingglass")
                    .labelStyle(.titleAndIcon)
            }
            .disabled(a == 0)
            .keyboardShortcut("r", modifiers: .command)
            .help(a == 0 ? "Select clips that haven't been analyzed yet" : "Step 1: find repeated frames and the game's frame rate (⌘R)")

            let p = model.processTargets.count
            Button { model.startProcessing() } label: {
                Label(p > 1 ? "Process \(p) Clips" : "Process", systemImage: "play.fill")
                    .labelStyle(.titleAndIcon)
            }
            .disabled(p == 0)
            .keyboardShortcut(.return, modifiers: .command)
            .help(p == 0 ? "Select analyzed clips. Analyze them first if needed." : "Step 2: render the selected clips with the current settings (⌘↩)")
        }
    }
}

// MARK: - Row

struct ClipRow: View {
    @Environment(AppModel.self) private var model
    let job: ClipJob

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "film")
                .font(.title2)
                .foregroundStyle(.secondary)
                .frame(width: 28)
            VStack(alignment: .leading, spacing: 3) {
                Text(job.name).font(.headline).lineLimit(1).truncationMode(.middle)
                if let a = job.analysis {
                    Text(a.summary).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    if job.status == .analyzed {
                        Text(a.advice).font(.caption).foregroundStyle(.tertiary).lineLimit(1)
                    }
                }
                statusLine
            }
            Spacer(minLength: 8)
            trailing
        }
        .padding(.vertical, 4)
    }

    @ViewBuilder
    private var statusLine: some View {
        switch job.status {
        case .analyzing:
            progressBar(label: "Analyzing")
        case .running:
            progressBar(label: "Processing")
        case .done:
            Label("Saved as \(job.output?.lastPathComponent ?? "")" + elapsedText, systemImage: "checkmark.circle.fill")
                .font(.caption).foregroundStyle(.green).lineLimit(1).truncationMode(.middle)
        case .failed(let reason), .analysisFailed(let reason):
            Label(reason, systemImage: "exclamationmark.triangle.fill")
                .font(.caption).foregroundStyle(.red).lineLimit(2)
        case .cancelled:
            Label("Stopped", systemImage: "stop.circle").font(.caption).foregroundStyle(.secondary)
        case .added, .analysisQueued, .analyzed, .queued:
            EmptyView()
        }
    }

    private func progressBar(label: String) -> some View {
        ProgressView(value: job.progress) {
            EmptyView()
        } currentValueLabel: {
            Text("\(label) \(Int(job.progress * 100))%").font(.caption).monospacedDigit()
        }
        .progressViewStyle(.linear)
    }

    @ViewBuilder
    private var trailing: some View {
        switch job.status {
        case .added:
            tag("Not analyzed")
        case .analysisQueued:
            tag("Waiting to analyze")
        case .analyzed:
            tag("Ready to process")
        case .queued:
            tag("Queued")
        case .done:
            if let out = job.output {
                Button { model.reveal(out) } label: { Image(systemName: "magnifyingglass") }
                    .buttonStyle(.borderless)
                    .help("Show the output in Finder (right-click for more)")
            }
        case .analyzing, .running, .failed, .analysisFailed, .cancelled:
            EmptyView()
        }
    }

    private func tag(_ text: String) -> some View {
        Text(text).font(.caption).foregroundStyle(.secondary)
    }

    private var elapsedText: String {
        guard let e = job.elapsed else { return "" }
        return e < 60 ? String(format: "  (%.0f s)", e) : String(format: "  (%d min %02d s)", Int(e) / 60, Int(e) % 60)
    }
}

// MARK: - Log

struct LogSheet: View {
    @Environment(\.dismiss) private var dismiss
    let job: ClipJob

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(job.name).font(.headline)
            ScrollView {
                Text(job.log.isEmpty ? "Nothing logged yet." : job.log.joined(separator: "\n"))
                    .font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(minHeight: 260)
            HStack {
                Spacer()
                Button("Done") { dismiss() }.keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
        .frame(width: 640, height: 420)
    }
}
