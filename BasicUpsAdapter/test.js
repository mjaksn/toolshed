import assert from "node:assert/strict";
import test from "node:test";

import { parseUpsInfo } from "./parse.js";
import { createServer } from "./server.js";

// The two fixtures indent differently on purpose: tabs in the status output,
// spaces in the config output. The pattern leads with `\s*`, so both work, and
// having one of each stops a future change from quietly depending on either.
const STATUS_OUTPUT = [
  "",
  "The UPS information shows as following:",
  "",
  "\tProperties:",
  "\t\tModel Name................... CP1500PFCLCD",
  "\t\tFirmware Number.............. CRCA102-3I1",
  "\t\tRating Voltage............... 120 V",
  "\t\tRating Power................. 1000 Watt",
  "",
  "\tCurrent UPS status:",
  "\t\tState........................ Normal",
  "\t\tPower Supply by.............. Utility Power",
  "\t\tUtility Voltage.............. 121 V",
  "\t\tOutput Voltage............... 121 V",
  "\t\tBattery Capacity............. 100 %",
  "\t\tRemaining Runtime............ 39 min.",
  "\t\tLoad......................... 90 Watt(9 %)",
  "\t\tLine Interaction............. None",
  "\t\tTest Result.................. Unknown",
  "\t\tLast Power Event............. None",
  "",
].join("\n");

const CONFIG_OUTPUT = [
  "",
  "Daemon Configuration:",
  "",
  "Alarm .............................................. On",
  "Hibernate .......................................... Off",
  "",
  "Action for Power Failure:",
  "",
  "    Delay time since Power failure ............. 60 sec.",
  "    Enable Shutdown System ..................... Off",
  "",
].join("\n");

test("parses both outputs into the shape the PHP produced", () => {
  const parsed = parseUpsInfo(STATUS_OUTPUT, CONFIG_OUTPUT);

  assert.deepEqual(Object.keys(parsed), ["UPS State", "UPS Configuration"]);
  assert.deepEqual(parsed["UPS State"]["Properties"], {
    "Model Name": "CP1500PFCLCD",
    "Firmware Number": "CRCA102-3I1",
    "Rating Voltage": "120 V",
    "Rating Power": "1000 Watt",
  });
  assert.equal(parsed["UPS State"]["Current UPS status"]["State"], "Normal");
  assert.equal(parsed["UPS State"]["Current UPS status"]["Battery Capacity"], "100 %");
  // A value carrying its own dot and brackets still survives intact.
  assert.equal(parsed["UPS State"]["Current UPS status"]["Remaining Runtime"], "39 min.");
  assert.equal(parsed["UPS State"]["Current UPS status"]["Load"], "90 Watt(9 %)");
});

test("the banner line becomes a section but never collects an entry", () => {
  // "The UPS information shows as following:" sets the current section, exactly
  // as the PHP did. Nothing follows it before "Properties:", so it must not
  // appear in the output at all.
  const parsed = parseUpsInfo(STATUS_OUTPUT, "");
  assert.ok(!("The UPS information shows as following" in parsed["UPS State"]));
  assert.deepEqual(Object.keys(parsed["UPS State"]), ["Properties", "Current UPS status"]);
});

test("quirk: a space before the dot run stays on the key", () => {
  // Inherited from the PHP, which strips dots from the label but not spaces.
  // Most of the config section is written this way, so most of its keys carry a
  // trailing space. Changing it would break anyone reading the old endpoint.
  const parsed = parseUpsInfo("", CONFIG_OUTPUT);
  const daemon = parsed["UPS Configuration"]["Daemon Configuration"];
  assert.deepEqual(Object.keys(daemon), ["Alarm ", "Hibernate "]);
  assert.equal(daemon["Alarm "], "On");
  assert.equal(daemon["Hibernate "], "Off");
  // And the tabbed status output, whose dots run straight on from the label,
  // produces keys with no trailing space at all.
  assert.equal("Model Name" in parseUpsInfo(STATUS_OUTPUT, "")["UPS State"]["Properties"], true);
});

test("quirk: a leading colon does not open a section", () => {
  // PHP's strpos returns 0 here, which is falsy, so the line was never treated
  // as a section header. `indexOf(...) > 0` preserves that.
  const parsed = parseUpsInfo(":not a section\nKey.......... value", "");
  assert.deepEqual(Object.keys(parsed["UPS State"]), [""]);
  assert.equal(parsed["UPS State"][""]["Key"], "value");
});

test("lines with fewer than ten dots are not entries", () => {
  const parsed = parseUpsInfo("Section:\nKey..... value", "");
  assert.deepEqual(parsed, {});
});

test("empty input yields an empty object", () => {
  assert.deepEqual(parseUpsInfo("", ""), {});
});

// ----- HTTP ------------------------------------------------------------------

const KEY = "test-key-of-sufficient-length";

/** Start a server on an ephemeral port and give the caller a fetch bound to it. */
async function withServer(run, body) {
  const server = createServer({ key: KEY, run });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    await body((path, init) => fetch(base + path, init));
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
}

const okRunner = (file, args) => {
  if (args[1] === "-status") return Promise.resolve(STATUS_OUTPUT);
  if (args[1] === "-config") return Promise.resolve(CONFIG_OUTPUT);
  return Promise.resolve("");
};

test("status needs the key", async () => {
  await withServer(okRunner, async (get) => {
    assert.equal((await get("/ups/status")).status, 401);
    assert.equal((await get("/ups/status", { headers: { "x-api-key": "wrong" } })).status, 401);
    const ok = await get("/ups/status", { headers: { "x-api-key": KEY } });
    assert.equal(ok.status, 200);
    assert.equal(ok.headers.get("content-type"), "application/json; charset=utf-8");
    const parsed = await ok.json();
    assert.equal(parsed["UPS State"]["Current UPS status"]["State"], "Normal");
  });
});

test("a key that is a prefix of the real one is rejected", async () => {
  await withServer(okRunner, async (get) => {
    const response = await get("/ups/status", {
      headers: { "x-api-key": KEY.slice(0, -1) },
    });
    assert.equal(response.status, 401);
  });
});

test("shutdown refuses GET and accepts POST", async () => {
  const calls = [];
  const runner = (file, args) => {
    calls.push([file, args]);
    return Promise.resolve("");
  };
  await withServer(runner, async (get) => {
    const rejected = await get("/ups/shutdown", { headers: { "x-api-key": KEY } });
    assert.equal(rejected.status, 405);
    assert.equal(rejected.headers.get("allow"), "POST");
    assert.deepEqual(calls, [], "a GET must not have powered anything off");

    const accepted = await get("/ups/shutdown", {
      method: "POST",
      headers: { "x-api-key": KEY },
    });
    assert.equal(accepted.status, 202);
    assert.deepEqual(await accepted.json(), { status: "shutting down" });
    assert.deepEqual(calls, [["sudo", ["systemctl", "poweroff"]]]);
  });
});

test("shutdown without the key does not run the command", async () => {
  const calls = [];
  await withServer(
    (file, args) => {
      calls.push([file, args]);
      return Promise.resolve("");
    },
    async (get) => {
      assert.equal((await get("/ups/shutdown", { method: "POST" })).status, 401);
      assert.deepEqual(calls, []);
    }
  );
});

test("a failing pwrstat gives 500 and leaks nothing", async () => {
  const failing = () => Promise.reject(new Error("sudo: pwrstat: command not found"));
  const logged = [];
  const server = createServer({ key: KEY, run: failing, log: (l) => logged.push(l) });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    const response = await fetch(base + "/ups/status", { headers: { "x-api-key": KEY } });
    assert.equal(response.status, 500);
    const body = await response.text();
    assert.deepEqual(JSON.parse(body), { error: "pwrstat failed" });
    assert.ok(!body.includes("command not found"), "the underlying message must stay in the log");
    assert.ok(logged.some((line) => line.includes("command not found")));
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test("healthz answers without a key and says nothing else", async () => {
  await withServer(okRunner, async (get) => {
    const response = await get("/healthz");
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { ok: true });
  });
});

test("unknown paths are 404 once the key is right", async () => {
  await withServer(okRunner, async (get) => {
    assert.equal((await get("/nope", { headers: { "x-api-key": KEY } })).status, 404);
    // Still 401 without the key, so the key check does not confirm which paths exist.
    assert.equal((await get("/nope")).status, 401);
  });
});

test("a server cannot be built without a key", () => {
  assert.throws(() => createServer({ key: "" }), /static key is required/);
});
