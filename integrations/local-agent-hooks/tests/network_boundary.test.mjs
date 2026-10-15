import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";
import { apiRequest } from "../lib/local_memory.mjs";

async function listen(server, context) {
  await new Promise((accept) => server.listen(0, "127.0.0.1", accept));
  context.after(() => new Promise((accept) => server.close(accept)));
  return `http://127.0.0.1:${server.address().port}`;
}

for (const status of [301, 302, 303, 307, 308]) {
  test(`local hooks reject HTTP ${status} without forwarding memory`, async (context) => {
    let forwarded = 0;
    const destination = await listen(createServer((request, response) => {
      forwarded += 1;
      request.resume();
      response.setHeader("Content-Type", "application/json");
      response.end('{"ok":true}');
    }), context);
    const baseUrl = await listen(createServer((request, response) => {
      request.resume();
      response.writeHead(status, { Location: destination });
      response.end();
    }), context);
    await assert.rejects(apiRequest(
      { baseUrl, timeoutMs: 2000 }, "POST", "/v1/messages",
      { content: "synthetic loopback-only memory" },
      { TMCRA_LOCAL_TOKEN: "synthetic-loopback-test-token" }, // public-audit: allow-test-fixture
    ));
    assert.equal(forwarded, 0);
  });
}
