#!/usr/bin/env node
import { writeFile } from "node:fs/promises";

const [port, expression, output] = process.argv.slice(2);
if (!/^\d+$/.test(port || "") || !expression) {
  throw new Error("Usage: node android_cdp.mjs <forwarded-port> <expression> [new-result.json]");
}
const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
const page = pages.find((candidate) => candidate.type === "page" && !candidate.url.startsWith("devtools:"));
if (!page) throw new Error("No AnkiDroid WebView page found");
const socket = new WebSocket(page.webSocketDebuggerUrl);
const timer = setTimeout(() => { socket.close(); throw new Error("CDP evaluation timed out"); }, 20000);
const result = await new Promise((resolve, reject) => {
  socket.onopen = () => socket.send(JSON.stringify({ id: 1, method: "Runtime.evaluate", params: { expression, awaitPromise: true, returnByValue: true } }));
  socket.onerror = reject;
  socket.onmessage = ({ data }) => {
    const message = JSON.parse(data);
    if (message.id !== 1) return;
    if (message.error || message.result.exceptionDetails) reject(new Error(JSON.stringify(message)));
    else resolve({ page: { title: page.title, url: page.url }, result: message.result.result.value });
  };
});
clearTimeout(timer);
socket.close();
const json = JSON.stringify(result, null, 2) + "\n";
if (output) await writeFile(output, json, { flag: "wx" });
process.stdout.write(json);
