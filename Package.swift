// swift-tools-version: 5.10
import PackageDescription

let package = Package(
    name: "rife-repair",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "rife-repair", targets: ["rife-repair"]),
        .executable(name: "FrameRepair", targets: ["FrameRepair"]),
    ],
    dependencies: [
        // RifeMetal (Apache-2.0): Metal RIFE v4.25, pinned to the commit this was built and tested with.
        .package(url: "https://github.com/cinemore/rife-metal.git", revision: "f18852ddb72f5706ddbeb3333b80643a63634222"),
        .package(url: "https://github.com/apple/swift-argument-parser.git", from: "1.3.0"),
    ],
    targets: [
        // Command-line engine: detection, retime, RIFE, encode.
        .executableTarget(
            name: "rife-repair",
            dependencies: [
                .product(name: "RifeMetal", package: "rife-metal"),
                .product(name: "ArgumentParser", package: "swift-argument-parser"),
            ]
        ),
        // App logic with no UI: settings -> engine arguments, output names, output parsing.
        .target(name: "FrameRepairKit"),
        // The Mac app. Runs the engine as a helper process (bundled inside the .app).
        .executableTarget(
            name: "FrameRepair",
            dependencies: ["FrameRepairKit"]
        ),
        .testTarget(
            name: "FrameRepairKitTests",
            dependencies: ["FrameRepairKit"]
        ),
    ]
)
