// The HTTP surface: two actions behind a static key, and one unauthenticated
// liveness probe that tells a caller nothing it does not already know.
//
// Nothing in this file reads the environment. `main.js` does that and passes the
// results in, which is what lets the tests drive a real server with a fake
// command runner and no UPS anywhere near them.

import { execFile } from "node:child_process";
import { createHash, timingSafeEqual } from "node:crypto";
import http from "node:http";

import { parseUpsInfo } from "./parse.js";

// No request data reaches any of these. The argument vectors are fixed at module
// scope and `execFile` runs the binary directly, with no shell to interpret
// anything, so there is no injection surface to reason about.
const COMMANDS = {
  status: ["sudo", ["pwrstat", "-status"]],
  config: ["sudo", ["pwrstat", "-config"]],
  poweroff: ["sudo", ["systemctl", "poweroff"]],
};

const COMMAND_TIMEOUT_MS = 10_000;

/** Run a command and resolve with its stdout, rejecting on a non-zero exit. */
function runCommand(file, args) {
  return new Promise((resolve, reject) => {
    execFile(file, args, { timeout: COMMAND_TIMEOUT_MS }, (error, stdout) => {
      if (error) reject(error);
      else resolve(stdout);
    });
  });
}

/**
 * Compare two keys without leaking their contents through timing.
 *
 * Both sides are hashed first so the comparison is always over 32 bytes.
 * Comparing the raw strings would return early on a length mismatch and leak the
 * length of the real key, which `timingSafeEqual` alone does not prevent because
 * it throws rather than compares when the lengths differ.
 */
function keyMatches(provided, expected) {
  const digest = (value) =>
    createHash("sha256").update(String(value ?? ""), "utf8").digest();
  return timingSafeEqual(digest(provided), digest(expected));
}

function sendJson(response, status, body, headers = {}) {
  const payload = JSON.stringify(body);
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(payload),
    // The responses are live state and a shutdown trigger. Neither should ever
    // be served from a cache.
    "cache-control": "no-store",
    ...headers,
  });
  response.end(payload);
}

/**
 * Build the server.
 *
 * @param {object} options
 * @param {string} options.key  the static key callers must present
 * @param {function} [options.run]  runs a command, `(file, args) => Promise<string>`
 * @param {function} [options.log]  receives one line strings
 */
export function createServer({ key, run = runCommand, log = () => {} }) {
  if (typeof key !== "string" || key === "") {
    throw new Error("a static key is required");
  }

  async function handleStatus(response) {
    let statusOutput;
    let configOutput;
    try {
      // Sequential rather than concurrent: pwrstat talks to the daemon over a
      // single socket and two at once is not worth the risk for two calls that
      // take milliseconds.
      statusOutput = await run(...COMMANDS.status);
      configOutput = await run(...COMMANDS.config);
    } catch (error) {
      // The message can carry a path or a sudo complaint, which is fine for the
      // log and not for the response body.
      log(`pwrstat failed: ${error.message}`);
      sendJson(response, 500, { error: "pwrstat failed" });
      return;
    }
    sendJson(response, 200, parseUpsInfo(statusOutput, configOutput));
  }

  async function handleShutdown(response) {
    try {
      await run(...COMMANDS.poweroff);
    } catch (error) {
      log(`poweroff failed: ${error.message}`);
      sendJson(response, 500, { error: "poweroff failed" });
      return;
    }
    // 202 rather than 200: the machine has accepted the request and is on its
    // way down, and there will be no further response about how it went.
    sendJson(response, 202, { status: "shutting down" });
  }

  return http.createServer((request, response) => {
    const path = new URL(request.url, "http://localhost").pathname;

    // Unauthenticated on purpose, and deliberately empty. No version, no
    // hostname, no UPS state: anything richer would be a free read for anyone
    // who can reach the port.
    if (path === "/healthz") {
      if (request.method !== "GET") {
        sendJson(response, 405, { error: "method not allowed" }, { allow: "GET" });
        return;
      }
      sendJson(response, 200, { ok: true });
      return;
    }

    // The key travels in a header, never a query string, because query strings
    // are written to access logs and kept in browser history.
    if (!keyMatches(request.headers["x-api-key"], key)) {
      log(`rejected ${request.method} ${path}`);
      sendJson(response, 401, { error: "unauthorized" });
      return;
    }

    if (path === "/ups/status") {
      if (request.method !== "GET") {
        sendJson(response, 405, { error: "method not allowed" }, { allow: "GET" });
        return;
      }
      handleStatus(response);
      return;
    }

    if (path === "/ups/shutdown") {
      // The PHP triggered this on GET. Anything that follows a link will issue a
      // GET, including crawlers, browser prefetchers and link preview bots, so
      // requiring POST is the difference between an action and an accident.
      if (request.method !== "POST") {
        sendJson(response, 405, { error: "method not allowed" }, { allow: "POST" });
        return;
      }
      handleShutdown(response);
      return;
    }

    sendJson(response, 404, { error: "not found" });
  });
}
