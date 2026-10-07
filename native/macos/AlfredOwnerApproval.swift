// Unsandboxed owner helper. A receipt proves presence, never provider billing.
// Caller/installation trust and event persistence belong to the trusted host.
import AppKit
import CryptoKit
import Foundation
import LocalAuthentication

enum Invalid: Error { case input, cancelled, authentication, expired }

// JSONSerialization accepts duplicate keys. Scan first, rejecting duplicates at
// every depth and retaining the exact canonical request bytes sent by Python.
struct StrictJSON {
    let data: [UInt8]
    var offset = 0
    var requestRange: Range<Int>?

    mutating func whitespace() {
        while offset < data.count && [9, 10, 13, 32].contains(data[offset]) {
            offset += 1
        }
    }
    mutating func take(_ byte: UInt8) throws {
        whitespace()
        guard offset < data.count, data[offset] == byte else { throw Invalid.input }
        offset += 1
    }
    mutating func string() throws -> String {
        whitespace()
        let start = offset
        try take(34)
        while offset < data.count {
            let byte = data[offset]
            offset += 1
            if byte == 34 {
                guard let value = try JSONSerialization.jsonObject(
                    with: Data(data[start..<offset]), options: [.fragmentsAllowed]
                ) as? String else { throw Invalid.input }
                return value
            }
            guard byte >= 32 else { throw Invalid.input }
            if byte == 92 {
                guard offset < data.count else { throw Invalid.input }
                offset += 1
            }
        }
        throw Invalid.input
    }
    mutating func value(depth: Int = 0) throws {
        guard depth < 128 else { throw Invalid.input }
        whitespace()
        guard offset < data.count else { throw Invalid.input }
        switch data[offset] {
        case 34:
            _ = try string()
        case 123:
            offset += 1
            whitespace()
            if offset < data.count && data[offset] == 125 { offset += 1; return }
            var keys = Set<String>()
            while true {
                let key = try string()
                guard keys.insert(key).inserted else { throw Invalid.input }
                try take(58)
                whitespace()
                let start = offset
                try value(depth: depth + 1)
                if depth == 0 && key == "request" { requestRange = start..<offset }
                whitespace()
                guard offset < data.count else { throw Invalid.input }
                if data[offset] == 125 { offset += 1; return }
                try take(44)
            }
        case 91:
            offset += 1
            whitespace()
            if offset < data.count && data[offset] == 93 { offset += 1; return }
            while true {
                try value(depth: depth + 1)
                whitespace()
                guard offset < data.count else { throw Invalid.input }
                if data[offset] == 93 { offset += 1; return }
                try take(44)
            }
        default:
            let start = offset
            while offset < data.count && ![9, 10, 13, 32, 44, 93, 125].contains(data[offset]) {
                offset += 1
            }
            guard offset > start else { throw Invalid.input }
            _ = try JSONSerialization.jsonObject(
                with: Data(data[start..<offset]), options: [.fragmentsAllowed]
            )
        }
    }
}

func readExact(_ count: Int) throws -> Data {
    var data = Data()
    while data.count < count {
        let piece = FileHandle.standardInput.readData(ofLength: count - data.count)
        guard !piece.isEmpty else { throw Invalid.input }
        data.append(piece)
    }
    return data
}

func readChallenge() throws -> ([String: Any], Data) {
    let header = try readExact(4)
    let count = header.reduce(0) { ($0 << 8) | Int($1) }
    guard count > 0 && count <= 4 * 1024 * 1024 else { throw Invalid.input }
    let payload = try readExact(count)
    // The trusted host closes stdin after the single challenge frame.
    guard FileHandle.standardInput.readData(ofLength: 1).isEmpty else {
        throw Invalid.input
    }
    var parser = StrictJSON(data: [UInt8](payload))
    try parser.value()
    parser.whitespace()
    guard parser.offset == payload.count, let range = parser.requestRange,
          let challenge = try JSONSerialization.jsonObject(with: payload)
            as? [String: Any] else { throw Invalid.input }
    return (challenge, payload.subdata(in: range))
}

func parseTime(_ value: String) -> Date? {
    let fractional = ISO8601DateFormatter()
    fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return fractional.date(from: value) ?? ISO8601DateFormatter().date(from: value)
}

func present(challenge: [String: Any], request: Data) throws {
    let app = NSApplication.shared
    app.setActivationPolicy(.regular)
    let alert = NSAlert()
    alert.messageText = "批准此精确的 Alfred 本地对象"
    alert.informativeText = "请核对完整对象、用途和摘要。此确认不会自动启动运行，也不证明供应商费用上限。"
    alert.addButton(withTitle: "批准此精确对象")
    alert.addButton(withTitle: "取消")
    let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 760, height: 480))
    scroll.hasVerticalScroller = true
    scroll.hasHorizontalScroller = true
    let view = NSTextView(frame: scroll.bounds)
    view.isEditable = false
    view.isSelectable = true
    view.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
    view.isHorizontallyResizable = true
    view.autoresizingMask = [.width]
    let scope = (challenge["request"] as? [String: Any])?["scope"] ?? "完整对象内的限定用途"
    view.string = "用途: \(scope)\n理由: \(challenge["reason"]!)\n"
        + "nonce: \(challenge["nonce"]!)\nSHA256: \(challenge["request_sha256"]!)\n"
        + "有效至: \(challenge["expires_at"]!)\n\n完整精确对象（未截断）:\n"
        + String(decoding: request, as: UTF8.self)
    scroll.documentView = view
    alert.accessoryView = scroll
    app.activate(ignoringOtherApps: true)
    guard alert.runModal() == .alertFirstButtonReturn else { throw Invalid.cancelled }
}

func authenticate(until deadline: Date) throws {
    let context = LAContext()
    context.touchIDAuthenticationAllowableReuseDuration = 0
    var error: NSError?
    guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: &error) else {
        throw Invalid.authentication
    }
    var result: Bool?
    context.evaluatePolicy(.deviceOwnerAuthentication,
        localizedReason: "认证刚才显示的 Alfred 精确批准对象") { success, _ in
        DispatchQueue.main.async { result = success }
    }
    while result == nil && Date() < deadline {
        RunLoop.current.run(until: Date().addingTimeInterval(0.05))
    }
    context.invalidate()
    guard Date() < deadline else { throw Invalid.expired }
    guard result == true else { throw Invalid.authentication }
}

func main() throws {
    guard CommandLine.arguments.count == 1 else { throw Invalid.input }
    let (challenge, rawRequest) = try readChallenge()
    let fields = Set(["contract", "version", "nonce", "request_sha256", "request",
                      "reason", "expires_at"])
    guard Set(challenge.keys) == fields,
          challenge["contract"] as? String == "V1-LOCAL-OWNER-AUTH",
          let version = challenge["version"] as? NSNumber,
          ["i", "q", "s", "l"].contains(String(cString: version.objCType)),
          version.intValue == 1,
          let nonce = challenge["nonce"] as? String, nonce.count == 64,
          nonce.allSatisfy({ $0.isHexDigit && !$0.isUppercase }),
          challenge["request"] is [String: Any],
          let reason = challenge["reason"] as? String, !reason.isEmpty,
          reason.count <= 2048,
          let expected = challenge["request_sha256"] as? String,
          let expiry = challenge["expires_at"] as? String,
          let deadline = parseTime(expiry), deadline > Date(),
          deadline.timeIntervalSinceNow <= 300 else { throw Invalid.input }
    let actual = SHA256.hash(data: rawRequest).map { String(format: "%02x", $0) }.joined()
    guard actual == expected else { throw Invalid.input }
    try present(challenge: challenge, request: rawRequest)
    guard Date() < deadline else { throw Invalid.expired }
    try authenticate(until: deadline)
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    let receipt: [String: Any] = [
        "contract": "V1-LOCAL-OWNER-AUTH", "version": 1,
        "nonce": nonce, "request_sha256": expected, "expires_at": expiry,
        "owner_uid": Int(getuid()), "authenticated_at": formatter.string(from: Date()),
        "authentication": "deviceOwnerAuthentication", "approved": true,
    ]
    let output = try JSONSerialization.data(withJSONObject: receipt, options: [.sortedKeys])
    var length = UInt32(output.count).bigEndian
    FileHandle.standardOutput.write(Data(bytes: &length, count: 4))
    FileHandle.standardOutput.write(output)
}

do {
    try main()
} catch {
    FileHandle.standardError.write(Data("owner_authentication_not_approved\n".utf8))
    exit(1)
}
