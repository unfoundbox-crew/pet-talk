// WSClient.swift — the daemon's own socket to the studio.
//
// One URLSessionWebSocketTask to ws://127.0.0.1:8089/ws, JSON frames both ways,
// the studio token resolved exactly the way cli/client.py and server/auth.py
// resolve it, and a 300 ms /health probe before a turn so a dead server fails
// with a named reason in a third of a second instead of hanging the capsule.
//
// Everything here fails closed: a missing token, a rejected handshake, a
// refused health probe each surface as a NAMED reason the caller shows on the
// capsule. There is no silent retry that pretends the turn is fine.

import Foundation

// MARK: - Token

/// server/auth.py's resolution order, ported: env STUDIO_TOKEN, then the file
/// named by STUDIO_TOKEN_FILE, then the file the server generates at
/// `<repo>/.qa-scratch/studio.token`. The repo root is found by walking up from
/// the running executable for AGENTS.md — the same walk resolveCliPath() does —
/// so it works from any checkout and never needs an absolute path in tree.
public enum StudioToken {
    public enum Source: String {
        case env = "env"
        case file = "file"
        case generated = "generated"
        case none = "none"
    }

    public static let header = "X-Studio-Token"
    public static let queryName = "token"
    /// server/ws.py closes the handshake with this when the token is wrong.
    public static let unauthorizedCloseCode = 4401

    public static func resolve() -> (token: String, source: Source) {
        let env = ProcessInfo.processInfo.environment
        if let t = env["STUDIO_TOKEN"]?.trimmingCharacters(in: .whitespacesAndNewlines), !t.isEmpty {
            return (t, .env)
        }
        if let path = env["STUDIO_TOKEN_FILE"]?.trimmingCharacters(in: .whitespacesAndNewlines),
           !path.isEmpty,
           let contents = try? String(contentsOfFile: path, encoding: .utf8) {
            let t = contents.trimmingCharacters(in: .whitespacesAndNewlines)
            if !t.isEmpty { return (t, .file) }
        }
        if let root = repoRoot() {
            let generated = (root as NSString).appendingPathComponent(".qa-scratch/studio.token")
            if let contents = try? String(contentsOfFile: generated, encoding: .utf8) {
                let t = contents.trimmingCharacters(in: .whitespacesAndNewlines)
                if !t.isEmpty { return (t, .generated) }
            }
        }
        return ("", .none)
    }

    /// Walk up from the executable (and then the cwd) looking for AGENTS.md.
    public static func repoRoot() -> String? {
        var candidates: [String] = []
        candidates.append(URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath().deletingLastPathComponent().path)
        candidates.append(FileManager.default.currentDirectoryPath)
        for start in candidates {
            var dir = start
            for _ in 0..<8 {
                let marker = (dir as NSString).appendingPathComponent("AGENTS.md")
                if FileManager.default.fileExists(atPath: marker) { return dir }
                let parent = (dir as NSString).deletingLastPathComponent
                if parent == dir || parent.isEmpty { break }
                dir = parent
            }
        }
        return nil
    }
}

// MARK: - Errors

public enum WSClientError: Error, CustomStringConvertible {
    /// The /health probe did not answer 200 inside the budget.
    case healthProbeFailed(String)
    /// The handshake was rejected (401/403) or closed 4401 — token missing/wrong.
    case unauthorized
    /// The socket closed or failed for any other named reason.
    case transport(String)
    case badURL(String)

    public var reason: String {
        switch self {
        case .healthProbeFailed: return "health_probe_failed"
        case .unauthorized: return "ws_unauthorized"
        case .transport: return "ws_transport_failed"
        case .badURL: return "ws_bad_url"
        }
    }

    /// What the capsule says. User-facing, no engineering vocabulary.
    public var capsuleText: String {
        switch self {
        case .healthProbeFailed: return "can't reach the studio"
        case .unauthorized: return "studio token missing"
        case .transport: return "studio connection lost"
        case .badURL: return "studio address is wrong"
        }
    }

    public var description: String {
        switch self {
        case .healthProbeFailed(let d): return "health_probe_failed(\(d))"
        case .unauthorized: return "ws_unauthorized"
        case .transport(let d): return "ws_transport_failed(\(d))"
        case .badURL(let d): return "ws_bad_url(\(d))"
        }
    }
}

// MARK: - Client

public final class WSClient: NSObject {
    /// docs/SPEC.md's duplex port.
    public static let defaultURL = "ws://127.0.0.1:8089/ws"
    /// A dead server must cost a third of a second, not a hung capsule.
    public static let healthTimeout: TimeInterval = 0.3
    /// Reconnect backoff, capped. Five tries and then a named failure — an
    /// endless retry loop is a silent fallback wearing a progress spinner.
    public static let backoffMs: [Int] = [120, 250, 500, 1000, 2000]

    public private(set) var url: URL
    private let token: String
    public let tokenSource: StudioToken.Source

    private var session: URLSession!
    private var task: URLSessionWebSocketTask?
    private let queue = DispatchQueue(label: "pet-talk.ws-client")
    private var attempt = 0
    private var closedIntentionally = false
    /// Set by the task-completion delegate when the upgrade was answered 401/403
    /// (server/ws.py's pre-accept close surfaces exactly that way, measured in
    /// cli/client.py's comment too). Read on `queue`.
    private var sawUnauthorizedStatus = false
    /// A named failure is reported once per client, never twice for one cause.
    private var reportedFailure = false

    /// Every decoded frame, on `queue`. Ordering is the socket's ordering.
    public var onFrame: (([String: Any]) -> Void)?
    /// A named, final failure. Called at most once per connect attempt chain.
    public var onFailure: ((WSClientError) -> Void)?
    /// Connected and ready to send.
    public var onOpen: (() -> Void)?
    /// Line-oriented diagnostics for the daemon log.
    public var onLog: ((String) -> Void)?

    public init(urlString: String = WSClient.defaultURL) throws {
        let resolved = StudioToken.resolve()
        self.token = resolved.token
        self.tokenSource = resolved.source
        guard let base = URL(string: urlString) else {
            throw WSClientError.badURL(urlString)
        }
        // Both forms, exactly like cli/client.py: the header is the primary and
        // ?token= is the fallback for anything that drops headers on upgrade.
        // server/auth.py accepts either.
        if resolved.token.isEmpty {
            self.url = base
        } else {
            var comps = URLComponents(url: base, resolvingAgainstBaseURL: false)
            var items = comps?.queryItems ?? []
            if !items.contains(where: { $0.name == StudioToken.queryName }) {
                items.append(URLQueryItem(name: StudioToken.queryName, value: resolved.token))
            }
            comps?.queryItems = items
            self.url = comps?.url ?? base
        }
        super.init()
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 10
        self.session = URLSession(configuration: config, delegate: self, delegateQueue: nil)
    }

    /// `http(s)://host` for the same origin — where /health lives.
    public var httpBase: URL? {
        var comps = URLComponents(url: url, resolvingAgainstBaseURL: false)
        comps?.scheme = (url.scheme == "wss") ? "https" : "http"
        comps?.path = ""
        comps?.query = nil
        return comps?.url
    }

    // MARK: Health probe

    /// GET /health with a hard 300 ms budget. Synchronous by design: it runs
    /// before a turn starts, and a turn that cannot reach the studio must not
    /// open the microphone at all.
    public func probeHealth(timeout: TimeInterval = WSClient.healthTimeout) -> Result<Void, WSClientError> {
        guard let base = httpBase, let healthURL = URL(string: "/health", relativeTo: base) else {
            return .failure(.badURL(url.absoluteString))
        }
        var request = URLRequest(url: healthURL)
        request.timeoutInterval = timeout
        request.httpMethod = "GET"
        if !token.isEmpty { request.setValue(token, forHTTPHeaderField: StudioToken.header) }

        let sem = DispatchSemaphore(value: 0)
        var outcome: Result<Void, WSClientError> = .failure(.healthProbeFailed("no_response"))
        let probe = session.dataTask(with: request) { data, response, error in
            defer { sem.signal() }
            if let error = error {
                outcome = .failure(.healthProbeFailed((error as NSError).code == NSURLErrorTimedOut ? "timeout" : "unreachable"))
                return
            }
            guard let http = response as? HTTPURLResponse else {
                outcome = .failure(.healthProbeFailed("no_http_response"))
                return
            }
            guard http.statusCode == 200 else {
                outcome = .failure(.healthProbeFailed("status_\(http.statusCode)"))
                return
            }
            if let data = data,
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let ok = obj["ok"] as? Bool, ok == false {
                outcome = .failure(.healthProbeFailed("degraded"))
                return
            }
            outcome = .success(())
        }
        probe.resume()
        // Belt and braces: URLSession's own timeout has coarse granularity, so
        // the wait is bounded here too. Both paths report the same named reason.
        if sem.wait(timeout: .now() + timeout + 0.15) == .timedOut {
            probe.cancel()
            return .failure(.healthProbeFailed("timeout"))
        }
        return outcome
    }

    // MARK: Connect / send / close

    public func connect() {
        queue.async { [weak self] in
            guard let self = self else { return }
            self.closedIntentionally = false
            self.openSocket()
        }
    }

    private func openSocket() {
        var request = URLRequest(url: url)
        if !token.isEmpty { request.setValue(token, forHTTPHeaderField: StudioToken.header) }
        let t = session.webSocketTask(with: request)
        task = t
        t.resume()
        receiveLoop(on: t)
    }

    private func receiveLoop(on t: URLSessionWebSocketTask) {
        t.receive { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .success(let message):
                var text: String?
                switch message {
                case .string(let s): text = s
                case .data(let d): text = String(data: d, encoding: .utf8)
                @unknown default: text = nil
                }
                if let text = text,
                   let data = text.data(using: .utf8),
                   let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                    self.queue.async { self.onFrame?(obj) }
                } else {
                    self.onLog?("! [ws] undecodable frame dropped (bad_frame)")
                }
                self.receiveLoop(on: t)
            case .failure(let error):
                self.handleFailure(error)
            }
        }
    }

    private func handleFailure(_ error: Error) {
        queue.async { [weak self] in
            guard let self = self, !self.closedIntentionally else { return }
            let ns = error as NSError
            if self.sawUnauthorizedStatus {
                self.report(.unauthorized)
                return
            }
            if self.attempt < WSClient.backoffMs.count {
                let delay = WSClient.backoffMs[self.attempt]
                self.attempt += 1
                self.onLog?("| [ws] reconnect attempt \(self.attempt) in \(delay)ms (\(ns.code))")
                self.queue.asyncAfter(deadline: .now() + .milliseconds(delay)) { [weak self] in
                    guard let self = self, !self.closedIntentionally else { return }
                    self.openSocket()
                }
                return
            }
            self.report(.transport("\(ns.domain)#\(ns.code) after \(self.attempt) reconnects"))
        }
    }

    /// Must be called on `queue`.
    private func report(_ error: WSClientError) {
        guard !reportedFailure else { return }
        reportedFailure = true
        onFailure?(error)
    }

    /// One JSON frame. `turn_id` is the caller's job — every frame carries one
    /// (docs/SPEC.md §4), and a frame without one is a bug, not a default.
    public func send(_ frame: [String: Any], completion: ((WSClientError?) -> Void)? = nil) {
        guard let data = try? JSONSerialization.data(withJSONObject: frame),
              let text = String(data: data, encoding: .utf8) else {
            completion?(.transport("frame_not_serializable"))
            return
        }
        guard let t = task else {
            completion?(.transport("not_connected"))
            return
        }
        t.send(.string(text)) { error in
            if let error = error {
                completion?(.transport("send_failed(\((error as NSError).code))"))
            } else {
                completion?(nil)
            }
        }
    }

    public func close() {
        queue.async { [weak self] in
            guard let self = self else { return }
            self.closedIntentionally = true
            self.task?.cancel(with: .goingAway, reason: nil)
            self.task = nil
        }
    }
}

extension WSClient: URLSessionWebSocketDelegate {
    public func urlSession(
        _ session: URLSession,
        webSocketTask: URLSessionWebSocketTask,
        didOpenWithProtocol protocol: String?
    ) {
        queue.async { [weak self] in
            guard let self = self else { return }
            self.attempt = 0
            self.onLog?("| [ws] open \(self.url.host ?? "?"):\(self.url.port ?? 0) token=\(self.tokenSource.rawValue)")
            self.onOpen?()
        }
    }

    public func urlSession(
        _ session: URLSession,
        webSocketTask: URLSessionWebSocketTask,
        didCloseWith closeCode: URLSessionWebSocketTask.CloseCode,
        reason: Data?
    ) {
        queue.async { [weak self] in
            guard let self = self, !self.closedIntentionally else { return }
            if closeCode.rawValue == StudioToken.unauthorizedCloseCode {
                self.report(.unauthorized)
                return
            }
            self.onLog?("| [ws] closed code=\(closeCode.rawValue)")
            self.report(.transport("closed_\(closeCode.rawValue)"))
        }
    }

    public func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        didCompleteWithError error: Error?
    ) {
        // A rejected upgrade never becomes an open socket, so the HTTP status is
        // the only place the "no/bad token" case is legible. Record it here; the
        // receive failure that follows reports the named reason.
        guard let http = task.response as? HTTPURLResponse,
              http.statusCode == 401 || http.statusCode == 403 else { return }
        queue.async { [weak self] in
            guard let self = self, !self.closedIntentionally else { return }
            self.sawUnauthorizedStatus = true
            self.report(.unauthorized)
        }
    }
}
