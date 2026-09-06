// Entry point. The only file that reads the environment, which is what keeps
// server.js testable.

import { createServer } from "./server.js";

const MINIMUM_KEY_LENGTH = 16;

const key = process.env.UPS_ADAPTER_KEY ?? "";
const port = Number(process.env.PORT ?? 8080);
// Loopback unless somebody says otherwise. This service can turn the machine
// off, so reaching the network is an explicit decision rather than the default.
const host = process.env.HOST ?? "127.0.0.1";

if (key === "") {
  console.error(
    "UPS_ADAPTER_KEY is not set. Refusing to start: an adapter that can power " +
      "the machine off does not get to run without a key."
  );
  process.exit(1);
}

if (key.length < MINIMUM_KEY_LENGTH) {
  // The length is reported because the caller already knows it. The key is not.
  console.error(
    `UPS_ADAPTER_KEY is ${key.length} characters. Refusing to start: use at ` +
      `least ${MINIMUM_KEY_LENGTH}, and generate it rather than inventing it.`
  );
  process.exit(1);
}

if (!Number.isInteger(port) || port < 1 || port > 65535) {
  console.error(`PORT is not a usable port number: ${process.env.PORT}`);
  process.exit(1);
}

const server = createServer({ key, log: (line) => console.log(line) });

server.listen(port, host, () => {
  console.log(`BasicUpsAdapter listening on http://${host}:${port}`);
  if (host !== "127.0.0.1" && host !== "localhost") {
    console.log(
      "Bound beyond loopback. Put a reverse proxy with TLS in front of this: " +
        "the key is sent as a plain header and is readable in transit without it."
    );
  }
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    server.close(() => process.exit(0));
  });
}
