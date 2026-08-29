import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import vm from "node:vm";


const source = await readFile(new URL("../frontend/app.js", import.meta.url), "utf8");


class MockClassList {
  constructor() {
    this.values = new Set(["skeleton"]);
  }

  remove(...names) {
    for (const name of names) {
      this.values.delete(name);
    }
  }
}


class MockElement {
  constructor() {
    this.classList = new MockClassList();
    this.dataset = {};
    this.hidden = true;
    this.listeners = new Map();
    this.attributes = new Map();
    this.textContent = "";
    this.dateTime = "";
    this.children = [];
    this.disabled = false;
    this.value = "";
    this.checked = false;
    this.className = "";
    this.title = "";
    this.type = "";
  }

  addEventListener(name, callback) {
    this.listeners.set(name, callback);
  }

  setAttribute(name, value) {
    this.attributes.set(name, value);
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  append(...children) {
    for (const child of children) {
      if (child && typeof child === "object") {
        child.parentElement = this;
      }
    }
    this.children.push(...children);
  }

  replaceChildren(...children) {
    for (const child of children) {
      if (child && typeof child === "object") {
        child.parentElement = this;
      }
    }
    this.children = [...children];
  }

  focus() {
    this.focused = true;
  }

  showModal() {
    this.attributes.set("open", "");
  }

  close() {
    this.attributes.delete("open");
  }
}


function createHarness(fetchImplementation, options = {}) {
  const selectors = [
    "#scanner-status",
    "#status-primary",
    "#stream-status",
    "#stream-chip",
    "#status-last-updated",
    "#ledger-offset",
    "#offset-detail",
    "#active-party-count",
    "#party-count-detail",
    "#catalog-summary",
    "#catalog-detail",
    "#error-banner",
    "#error-title",
    "#error-message",
    "#retry-button",
    "#admin-dialog",
    "#admin-form",
    "#admin-token",
    "#admin-feedback",
    "#admin-label",
    "#close-admin",
    "#unlock-admin",
    "#open-admin",
    "#party-search",
    "#party-list",
    "#party-total-count",
    "#party-previous",
    "#party-next",
    "#party-page-status",
    "#selection-count",
    "#selection-limit",
    "#selection-message",
    "#reset-selection",
    "#save-selection",
    "#focused-party",
    "#balance-state",
    "#balance-grid",
    "#history-state",
    "#history-table-wrap",
    "#history-body",
    "#history-summary",
    "#history-previous",
    "#history-next",
    "#history-page-status",
  ];
  const elements = new Map(selectors.map((selector) => [selector, new MockElement()]));
  const documentListeners = new Map();
  const windowListeners = new Map();
  const documentElement = { dataset: {} };
  let timerId = 0;

  const document = {
    documentElement,
    visibilityState: "visible",
    querySelector: (selector) => elements.get(selector) ?? null,
    createElement: () => new MockElement(),
    addEventListener: (name, callback) => documentListeners.set(name, callback),
  };
  const window = {
    setTimeout: () => ++timerId,
    clearTimeout: () => {},
    addEventListener: (name, callback) => windowListeners.set(name, callback),
    location: { href: options.href ?? "http://scanner.test/" },
    history: { replaceState: () => {} },
  };

  const context = vm.createContext({
    AbortController,
    Date,
    Error,
    Intl,
    Map,
    Math,
    Number,
    Object,
    RegExp,
    Set,
    String,
    URL,
    URLSearchParams,
    document,
    fetch: fetchImplementation,
    navigator: { onLine: true, clipboard: options.clipboard },
    window,
  });
  vm.runInContext(source, context);
  return { elements, documentElement, documentListeners, windowListeners };
}


async function settle() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}


test("health polling renders the persisted scanner state", async () => {
  const health = {
    status: "ok",
    last_offset: 2920767,
    catalog: {
      complete: true,
      error: null,
      readable_count: 5784,
      refreshed_at: new Date().toISOString(),
    },
    active_party_count: 3,
    desired_party_count: 3,
    restart_required: false,
  };
  const harness = createHarness(async (path) => {
    let payload;
    if (path === "/health") {
      payload = health;
    } else if (path === "/parties/selection") {
      payload = {
        desired_parties: [],
        active_parties: [],
        max_parties: 50,
        selection_management_enabled: false,
      };
    } else {
      payload = { items: [], total: 0, limit: 50, offset: 0 };
    }
    return {
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => payload,
    };
  });

  await settle();

  assert.equal(harness.documentElement.dataset.js, "ready");
  assert.equal(harness.elements.get("#stream-status").textContent, "Private index ready");
  assert.equal(harness.elements.get("#stream-chip").textContent, "Ready");
  assert.equal(harness.elements.get("#ledger-offset").textContent, "2,920,767");
  assert.equal(harness.elements.get("#active-party-count").textContent, "3");
  assert.equal(harness.elements.get("#catalog-summary").textContent, "5,784 readable");
  assert.equal(harness.elements.get("#scanner-status").attributes.get("aria-busy"), "false");
  assert.equal(harness.elements.get("#error-banner").hidden, true);
});


test("a failed refresh produces a retrying state without exposing raw errors", async () => {
  const harness = createHarness(async () => {
    throw new Error("sensitive upstream failure detail");
  });

  await settle();

  assert.equal(
    harness.elements.get("#stream-status").textContent,
    "Health refresh interrupted",
  );
  assert.equal(harness.elements.get("#stream-chip").textContent, "Retrying");
  assert.equal(harness.elements.get("#error-banner").hidden, false);
  assert.equal(
    harness.elements.get("#error-message").textContent.includes("sensitive"),
    false,
  );
});


test("party explorer preserves a full draft and submits with an in-memory token", async () => {
  const alice = "alice::1220alice";
  const bob = "bob::1220bob";
  const requests = [];
  const harness = createHarness(async (path, options = {}) => {
    requests.push({ path: String(path), options });
    let payload;
    if (path === "/health") {
      payload = {
        status: "ok",
        last_offset: 10,
        catalog: { complete: true, readable_count: 2 },
        active_party_count: 1,
        desired_party_count: 1,
        restart_required: false,
      };
    } else if (path === "/parties/selection" && options.method === "PUT") {
      payload = {
        desired_parties: [alice, bob],
        active_parties: [alice],
        desired_count: 2,
        active_count: 1,
        restart_required: true,
      };
    } else if (path === "/parties/selection") {
      payload = {
        desired_parties: [alice],
        active_parties: [alice],
        max_parties: 50,
        selection_management_enabled: true,
      };
    } else if (String(path).startsWith("/balance/")) {
      payload = { party: alice, balances: [], last_offset: 10, active: true };
    } else if (String(path).startsWith("/history/")) {
      payload = {
        party: alice,
        transfers: [],
        count: 0,
        total: 0,
        limit: 20,
        offset: 0,
        last_offset: 10,
        active: true,
      };
    } else {
      payload = {
        items: [
          {
            party: alice,
            display_name: "alice",
            readable: true,
            is_local: true,
            selected: true,
            active: true,
          },
          {
            party: bob,
            display_name: "bob",
            readable: true,
            is_local: true,
            selected: false,
            active: false,
          },
        ],
        total: 2,
        limit: 50,
        offset: 0,
      };
    }
    return {
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => payload,
    };
  });

  await settle();
  await settle();

  assert.equal(harness.elements.get("#party-list").children.length, 2);
  assert.equal(harness.elements.get("#party-total-count").textContent, "2");
  assert.equal(harness.elements.get("#selection-count").textContent, "1 selected");
  assert.equal(harness.elements.get("#party-search").disabled, false);

  harness.elements.get("#open-admin").listeners.get("click")();
  harness.elements.get("#admin-token").value = "tab-only-token";
  harness.elements.get("#admin-form").listeners.get("submit")({
    preventDefault: () => {},
  });
  assert.equal(harness.elements.get("#admin-label").textContent, "Admin unlocked");
  assert.equal(harness.elements.get("#admin-token").value, "");

  const bobRow = harness.elements.get("#party-list").children[1];
  const bobCheckbox = bobRow.children[0].children[0];
  bobCheckbox.checked = true;
  bobCheckbox.listeners.get("change")();
  assert.equal(harness.elements.get("#selection-count").textContent, "2 selected");

  await harness.elements.get("#save-selection").listeners.get("click")();
  await settle();

  const putRequest = requests.find((request) => request.options.method === "PUT");
  assert.ok(putRequest);
  assert.equal(putRequest.options.headers.Authorization, "Bearer tab-only-token");
  assert.deepEqual(JSON.parse(putRequest.options.body).parties, [alice, bob]);
});


test("focused dashboard preserves exact decimals and renders every transfer direction", async () => {
  const alice = "alice::1220alice";
  const bob = "bob::1220bob";
  const carol = "carol::1220carol";
  const exactAmount = "12345678901234567890.123456789012345678";
  const copied = [];
  const requests = [];
  const harness = createHarness(async (path) => {
    const route = String(path);
    requests.push(route);
    let payload;
    if (route === "/health") {
      payload = {
        status: "ok",
        last_offset: 44,
        catalog: { complete: true, readable_count: 3 },
        active_party_count: 1,
        desired_party_count: 1,
        restart_required: false,
      };
    } else if (route === "/parties/selection") {
      payload = {
        desired_parties: [alice],
        active_parties: [alice],
        max_parties: 50,
        selection_management_enabled: false,
      };
    } else if (route.startsWith("/parties?")) {
      payload = {
        items: [{ party: alice, display_name: "alice", readable: true, active: true }],
        total: 1,
        limit: 50,
        offset: 0,
      };
    } else if (route.startsWith("/balance/")) {
      payload = {
        party: alice,
        balances: [{ instrument: "Amulet", amount: exactAmount }],
        last_offset: 44,
        active: true,
      };
    } else if (route.includes("offset=20")) {
      payload = {
        party: alice,
        transfers: [{
          direction: "received",
          counterparty: carol,
          amount: "1.000000000000000001",
          instrument: "Amulet",
          record_time: "2026-08-29T17:30:00Z",
          offset: 40,
        }],
        count: 1,
        total: 21,
        limit: 20,
        offset: 20,
        last_offset: 44,
        active: true,
      };
    } else {
      payload = {
        party: alice,
        transfers: [
          {
            direction: "sent",
            counterparty: bob,
            amount: "10.000000000000000001",
            instrument: "Amulet",
            record_time: "2026-08-29T18:00:00Z",
            offset: 44,
          },
          {
            direction: "received",
            counterparty: carol,
            amount: "2.5",
            instrument: "USD",
            record_time: "2026-08-29T17:59:00Z",
            offset: 43,
          },
          {
            direction: "self",
            counterparty: alice,
            amount: "0.000000000000000001",
            instrument: "Amulet",
            record_time: "2026-08-29T17:58:00Z",
            offset: 42,
          },
        ],
        count: 3,
        total: 21,
        limit: 20,
        offset: 0,
        last_offset: 44,
        active: true,
      };
    }
    return {
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => payload,
    };
  }, {
    clipboard: { writeText: async (value) => copied.push(value) },
  });

  await settle();
  await settle();

  const balanceCard = harness.elements.get("#balance-grid").children[0];
  assert.equal(balanceCard.children[1].textContent, exactAmount);
  assert.equal(balanceCard.children[1].title, exactAmount);

  const historyRows = harness.elements.get("#history-body").children;
  assert.equal(historyRows.length, 3);
  assert.equal(historyRows[0].children[0].children[0].textContent, "sent");
  assert.equal(historyRows[1].children[0].children[0].textContent, "received");
  assert.equal(historyRows[2].children[0].children[0].textContent, "Self");
  assert.equal(historyRows[0].children[2].textContent, "10.000000000000000001");

  const copyButton = historyRows[0].children[1].children[0].children[1];
  await copyButton.listeners.get("click")();
  assert.deepEqual(copied, [bob]);
  assert.equal(copyButton.textContent, "Copied");

  assert.equal(harness.elements.get("#history-next").disabled, false);
  harness.elements.get("#history-next").listeners.get("click")();
  await settle();
  assert.ok(requests.some((route) => route.includes("offset=20")));
  assert.equal(harness.elements.get("#history-page-status").textContent, "Page 2 of 2");
});


test("inactive focused parties retain history without requesting a balance", async () => {
  const alice = "alice::1220alice";
  const bob = "bob::1220bob";
  const requests = [];
  const harness = createHarness(async (path) => {
    const route = String(path);
    requests.push(route);
    let payload;
    if (route === "/health") {
      payload = {
        status: "ok",
        last_offset: 50,
        catalog: { complete: true, readable_count: 2 },
        active_party_count: 1,
        desired_party_count: 1,
        restart_required: false,
      };
    } else if (route === "/parties/selection") {
      payload = {
        desired_parties: [alice],
        active_parties: [alice],
        max_parties: 50,
        selection_management_enabled: false,
      };
    } else if (route.startsWith("/parties?")) {
      payload = {
        items: [
          { party: alice, display_name: "alice", readable: true, active: true },
          { party: bob, display_name: "bob", readable: true, active: false },
        ],
        total: 2,
        limit: 50,
        offset: 0,
      };
    } else if (route.startsWith("/balance/")) {
      payload = { party: alice, balances: [], last_offset: 50, active: true };
    } else {
      const inactive = route.includes(encodeURIComponent(bob));
      payload = {
        party: inactive ? bob : alice,
        transfers: inactive ? [{
          direction: "received",
          counterparty: alice,
          amount: "7.25",
          instrument: "Amulet",
          record_time: "2026-08-29T18:30:00Z",
          offset: 49,
        }] : [],
        count: inactive ? 1 : 0,
        total: inactive ? 1 : 0,
        limit: 20,
        offset: 0,
        last_offset: 50,
        active: !inactive,
      };
    }
    return {
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => payload,
    };
  });

  await settle();
  await settle();
  const requestCountBeforeFocus = requests.length;
  const bobRow = harness.elements.get("#party-list").children[1];
  bobRow.children[1].listeners.get("click")();
  await settle();

  const focusedRequests = requests.slice(requestCountBeforeFocus);
  assert.equal(
    focusedRequests.some((route) => route.startsWith(`/balance/${encodeURIComponent(bob)}`)),
    false,
  );
  assert.equal(
    focusedRequests.some((route) => route.startsWith(`/history/${encodeURIComponent(bob)}`)),
    true,
  );
  assert.equal(
    harness.elements.get("#balance-grid").children[0].children[0].children[0].textContent,
    "No current balance available",
  );
  assert.equal(harness.elements.get("#history-body").children.length, 1);
  assert.equal(
    harness.elements.get("#history-state").textContent.includes("indexing inactive"),
    true,
  );
});
