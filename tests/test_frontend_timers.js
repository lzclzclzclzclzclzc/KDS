const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "app", "static", "js", "app.js"),
  "utf8"
);
const intervals = new Map();
let nextTimer = 0;
let resolveFirst;
const rendered = [];
const elements = new Map();
function getElement(id) {
  if (!elements.has(id)) {
    const element = {
      innerHTML: "",
      textContent: "",
      classList: { add() {}, remove() {}, toggle() {} },
      addEventListener() {},
      querySelector() { return element; },
      querySelectorAll() { return []; },
    };
    elements.set(id, element);
  }
  return elements.get(id);
}
const window = { addEventListener() {} };
const context = {
  window,
  document: { getElementById: getElement },
  location: { hash: "#/chat/A" },
  setInterval(fn) {
    const id = ++nextTimer;
    intervals.set(id, fn);
    return id;
  },
  clearInterval(id) { intervals.delete(id); },
  Date,
};
const instrumented = source.replace(
  /  route\(\);\r?\n\}\)\(\);/,
  `  api = (url) => url.endsWith("/A")
    ? new Promise((resolve) => { window.resolveFirst = resolve; })
    : Promise.resolve(window.secondConversation);
  renderChatState = (conv) => { window.rendered.push(conv.id); };
  window.exposed = { renderChat, clearChatTimer, pollChat };
})();`
);
assert.notEqual(instrumented, source, "failed to instrument app.js");
window.rendered = rendered;
window.secondConversation = {
  id: "B", status: "paused", messages: [], votes: [], can_resume: true,
  total_output_tokens: 4, total_max_tokens: 10,
};
vm.createContext(context);
vm.runInContext(instrumented, context);

async function run() {
  window.exposed.renderChat("A");
  window.exposed.renderChat("B");
  await Promise.resolve();
  assert.equal(intervals.size, 2, "one poller and one countdown timer should remain");
  assert.deepEqual(rendered, ["B"]);
  assert.equal(getElement("chat-tokens").textContent, "输出 4 / 10 tokens");

  window.secondConversation = {
    ...window.secondConversation, total_output_tokens: 7, total_max_tokens: 20,
  };
  await window.exposed.pollChat();
  assert.equal(getElement("chat-tokens").textContent, "输出 7 / 20 tokens");
  assert.deepEqual(rendered, ["B"], "a token-only change should not redraw messages");

  window.resolveFirst({ id: "A", status: "paused", messages: [], votes: [] });
  await Promise.resolve();
  assert.deepEqual(rendered, ["B"], "an old response must not overwrite the new chat");

  window.exposed.clearChatTimer();
  assert.equal(intervals.size, 0, "leaving chat should remove every timer");
}

run().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
