import AppKit
import SwiftUI

@main
struct FrameRepairApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @State private var model = AppModel()

    var body: some Scene {
        Window("Frame Repair", id: "main") {
            ContentView()
                .environment(model)
                .frame(minWidth: 940, minHeight: 560)
                .onAppear {
                    delegate.model = model
                    let env = ProcessInfo.processInfo.environment
                    if let clips = env["FRAME_REPAIR_SELFTEST"] {      // paths separated by "|"
                        Task {
                            await model.selfTest(clips: clips.split(separator: "|").map { URL(fileURLWithPath: String($0)) },
                                                 outFolder: env["FRAME_REPAIR_SELFTEST_OUT"].map { URL(fileURLWithPath: $0) })
                        }
                    }
                }
        }
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("Add Clips…") { model.showImporter = true }
                    .keyboardShortcut("o")
            }
        }
    }
}

/// Files dropped on the Dock icon or opened with "Open With", and stopping the
/// engine if the app quits mid-render.
final class AppDelegate: NSObject, NSApplicationDelegate {
    @MainActor var model: AppModel? {
        didSet { if let m = model, !pending.isEmpty { m.add(pending); pending = [] } }
    }
    @MainActor private var pending: [URL] = []

    @MainActor func application(_ application: NSApplication, open urls: [URL]) {
        if let model { model.add(urls) } else { pending += urls }
    }

    @MainActor func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let model, model.isBusy else { return .terminateNow }
        let alert = NSAlert()
        alert.messageText = "Clips are still being analyzed or processed."
        alert.informativeText = "Quitting stops it and deletes the unfinished file."
        alert.addButton(withTitle: "Keep Working")
        alert.addButton(withTitle: "Quit")
        guard alert.runModal() == .alertSecondButtonReturn else { return .terminateCancel }
        model.stopForQuit()
        return .terminateNow
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}
