(() => {
  "use strict";

  const HEALTH_POLL_MS = 5_000;
  const DASHBOARD_POLL_MS = 10_000;
  const REQUEST_TIMEOUT_MS = 8_000;
  const STALE_HEARTBEAT_MS = 15_000;

  class ApiError extends Error {
    constructor(status, payload) {
      super(`API request failed with status ${status}`);
      this.name = "ApiError";
      this.status = status;
      this.payload = payload;
    }
  }

  class RequestTimeoutError extends Error {
    constructor() {
      super("API request timed out");
      this.name = "RequestTimeoutError";
    }
  }

  const elements = {
    scannerStatus: document.querySelector("#scanner-status"),
    statusPrimary: document.querySelector("#status-primary"),
    streamStatus: document.querySelector("#stream-status"),
    streamChip: document.querySelector("#stream-chip"),
    lastUpdated: document.querySelector("#status-last-updated"),
    ledgerOffset: document.querySelector("#ledger-offset"),
    offsetDetail: document.querySelector("#offset-detail"),
    activePartyCount: document.querySelector("#active-party-count"),
    partyCountDetail: document.querySelector("#party-count-detail"),
    catalogSummary: document.querySelector("#catalog-summary"),
    catalogDetail: document.querySelector("#catalog-detail"),
    errorBanner: document.querySelector("#error-banner"),
    errorTitle: document.querySelector("#error-title"),
    errorMessage: document.querySelector("#error-message"),
    retryButton: document.querySelector("#retry-button"),
    adminDialog: document.querySelector("#admin-dialog"),
    adminForm: document.querySelector("#admin-form"),
    adminTokenInput: document.querySelector("#admin-token"),
    adminFeedback: document.querySelector("#admin-feedback"),
    adminLabel: document.querySelector("#admin-label"),
    closeAdminButton: document.querySelector("#close-admin"),
    unlockAdminButton: document.querySelector("#unlock-admin"),
    openAdminButton: document.querySelector("#open-admin"),
    partySearch: document.querySelector("#party-search"),
    partyList: document.querySelector("#party-list"),
    partyTotalCount: document.querySelector("#party-total-count"),
    partyPrevious: document.querySelector("#party-previous"),
    partyNext: document.querySelector("#party-next"),
    partyPageStatus: document.querySelector("#party-page-status"),
    selectionCount: document.querySelector("#selection-count"),
    selectionLimit: document.querySelector("#selection-limit"),
    selectionMessage: document.querySelector("#selection-message"),
    resetSelection: document.querySelector("#reset-selection"),
    saveSelection: document.querySelector("#save-selection"),
    focusedParty: document.querySelector("#focused-party"),
    balanceState: document.querySelector("#balance-state"),
    balanceGrid: document.querySelector("#balance-grid"),
    historyState: document.querySelector("#history-state"),
    historyTableWrap: document.querySelector("#history-table-wrap"),
    historyBody: document.querySelector("#history-body"),
    historySummary: document.querySelector("#history-summary"),
    historyPrevious: document.querySelector("#history-previous"),
    historyNext: document.querySelector("#history-next"),
    historyPageStatus: document.querySelector("#history-page-status"),
  };

  const state = {
    health: null,
    healthController: null,
    healthRequestVersion: 0,
    pollTimer: null,
    lastSuccessfulAt: null,
    parties: [],
    partyQuery: "",
    partyLimit: 50,
    partyOffset: 0,
    partyTotal: 0,
    partyController: null,
    partyRequestVersion: 0,
    partySearchTimer: null,
    selection: null,
    desiredParties: new Set(),
    activeParties: new Set(),
    draftParties: new Set(),
    focusedParty: null,
    adminToken: null,
    adminUnlocked: false,
    selectionSaving: false,
    dashboardPollTimer: null,
    balance: null,
    balanceController: null,
    balanceRequestVersion: 0,
    history: null,
    historyLimit: 20,
    historyOffset: 0,
    historyController: null,
    historyRequestVersion: 0,
  };

  document.documentElement.dataset.js = "ready";

  async function requestJson(path, options = {}) {
    const controller = new AbortController();
    const parentSignal = options.signal;
    let timedOut = false;

    const abortFromParent = () => controller.abort(parentSignal.reason);
    if (parentSignal) {
      if (parentSignal.aborted) {
        abortFromParent();
      } else {
        parentSignal.addEventListener("abort", abortFromParent, { once: true });
      }
    }

    const timeout = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, options.timeoutMs ?? REQUEST_TIMEOUT_MS);

    try {
      const response = await fetch(path, {
        method: options.method ?? "GET",
        headers: {
          Accept: "application/json",
          ...(options.headers ?? {}),
        },
        body: options.body,
        signal: controller.signal,
      });

      const contentType = response.headers.get("content-type") ?? "";
      let payload = null;
      if (contentType.includes("application/json")) {
        payload = await response.json();
      } else {
        const text = await response.text();
        payload = text ? { detail: text.slice(0, 300) } : null;
      }

      if (!response.ok) {
        throw new ApiError(response.status, payload);
      }
      return payload;
    } catch (error) {
      if (timedOut) {
        throw new RequestTimeoutError();
      }
      throw error;
    } finally {
      window.clearTimeout(timeout);
      if (parentSignal) {
        parentSignal.removeEventListener("abort", abortFromParent);
      }
    }
  }

  function formatInteger(value) {
    if (value === null || value === undefined || value === "") {
      return "—";
    }
    const text = String(value);
    if (/^-?\d+$/.test(text)) {
      const sign = text.startsWith("-") ? "-" : "";
      const digits = sign ? text.slice(1) : text;
      return sign + digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    }
    return text;
  }

  function formatTime(date) {
    return new Intl.DateTimeFormat(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(date);
  }

  function formatRelativeTime(value) {
    if (!value) {
      return "not refreshed";
    }
    const timestamp = new Date(value);
    if (Number.isNaN(timestamp.getTime())) {
      return "refresh time unavailable";
    }
    const seconds = Math.max(0, Math.round((Date.now() - timestamp.getTime()) / 1000));
    if (seconds < 10) {
      return "just now";
    }
    if (seconds < 60) {
      return `${seconds}s ago`;
    }
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) {
      return `${minutes}m ago`;
    }
    const hours = Math.floor(minutes / 60);
    if (hours < 24) {
      return `${hours}h ago`;
    }
    return `${Math.floor(hours / 24)}d ago`;
  }

  function removeSkeleton(element) {
    element.classList.remove(
      "skeleton",
      "skeleton--metric",
      "skeleton--short",
      "skeleton--medium",
    );
  }

  function setMetric(element, value) {
    removeSkeleton(element);
    element.textContent = value;
  }

  function heartbeatIsStale(stream) {
    if (!stream || stream.status !== "connected" || !stream.last_heartbeat) {
      return false;
    }
    const heartbeat = new Date(stream.last_heartbeat).getTime();
    return Number.isFinite(heartbeat) && Date.now() - heartbeat > STALE_HEARTBEAT_MS;
  }

  function deriveStatus(health) {
    const catalog = health.catalog ?? {};
    const stream = health.stream ?? null;

    if (health.restart_required) {
      return {
        tone: "pending",
        label: "Reconciliation required",
        chip: "Pending",
      };
    }
    if (catalog.error) {
      return {
        tone: "error",
        label: "Catalog refresh failed",
        chip: "Attention",
      };
    }
    if (health.status === "bootstrap_required") {
      if (!catalog.complete && !catalog.refreshed_at) {
        return {
          tone: "discovering",
          label: "Discovering authorized parties",
          chip: "Starting",
        };
      }
      return {
        tone: "pending",
        label: "ACS bootstrap required",
        chip: "Setup",
      };
    }
    if (heartbeatIsStale(stream)) {
      return {
        tone: "stale",
        label: "Live stream heartbeat is stale",
        chip: "Stale",
      };
    }
    if (stream && stream.status) {
      const runtimeStates = {
        starting: ["starting", "Starting scanner worker", "Starting"],
        discovering: ["discovering", "Refreshing party catalog", "Discovering"],
        reconciling: ["pending", "Reconciling party selection", "Reconciling"],
        connected: ["connected", "Live stream connected", "Live"],
        retrying: ["retrying", "Scanner reconnecting", "Retrying"],
        stopped: ["stopped", "Scanner worker stopped", "Stopped"],
      };
      const runtime = runtimeStates[stream.status];
      if (runtime) {
        return { tone: runtime[0], label: runtime[1], chip: runtime[2] };
      }
    }
    return {
      tone: "ready",
      label: "Private index ready",
      chip: "Ready",
    };
  }

  function renderHealth(health) {
    const catalog = health.catalog ?? {};
    const presentation = deriveStatus(health);
    const activeCount = health.active_party_count ?? 0;
    const desiredCount = health.desired_party_count ?? 0;
    const readableCount = catalog.readable_count ?? 0;

    elements.statusPrimary.dataset.status = presentation.tone;
    elements.streamStatus.textContent = presentation.label;
    elements.streamChip.textContent = presentation.chip;

    setMetric(elements.ledgerOffset, formatInteger(health.last_offset));
    elements.offsetDetail.textContent = health.last_offset === null
      ? "Waiting for initial ACS snapshot"
      : "Resume checkpoint saved";

    setMetric(elements.activePartyCount, formatInteger(activeCount));
    elements.partyCountDetail.textContent = `${formatInteger(desiredCount)} desired`;

    setMetric(
      elements.catalogSummary,
      readableCount ? `${formatInteger(readableCount)} readable` : "Not populated",
    );
    if (catalog.error) {
      elements.catalogDetail.textContent = "Last refresh failed";
    } else if (catalog.refreshed_at) {
      elements.catalogDetail.textContent = `Refreshed ${formatRelativeTime(catalog.refreshed_at)}`;
    } else {
      elements.catalogDetail.textContent = "Catalog refresh required";
    }

    const updatedAt = state.lastSuccessfulAt ?? new Date();
    elements.lastUpdated.dateTime = updatedAt.toISOString();
    elements.lastUpdated.textContent = `Updated ${formatTime(updatedAt)}`;
    elements.scannerStatus.setAttribute("aria-busy", "false");
  }

  function friendlyError(error) {
    if (!navigator.onLine) {
      return {
        title: "You appear to be offline",
        message: "Live refresh will resume automatically when the connection returns.",
      };
    }
    if (error instanceof RequestTimeoutError) {
      return {
        title: "The scanner API is taking too long",
        message: "The last indexed state remains visible while the dashboard retries.",
      };
    }
    if (error instanceof ApiError) {
      const messages = {
        400: "The scanner rejected the request.",
        403: "This action is not authorized.",
        404: "The requested scanner resource was not found.",
        409: "The scanner is waiting for party reconciliation.",
        422: "The scanner could not validate the request.",
      };
      return {
        title: error.status >= 500
          ? "The scanner API is temporarily unavailable"
          : "The scanner could not refresh",
        message: messages[error.status]
          ?? "The last indexed state remains visible while the dashboard retries.",
      };
    }
    return {
      title: "The scanner could not be reached",
      message: "Check the service connection or retry in a moment.",
    };
  }

  function showError(error) {
    const message = friendlyError(error);
    elements.errorTitle.textContent = message.title;
    elements.errorMessage.textContent = message.message;
    elements.errorBanner.hidden = false;
  }

  function hideError() {
    elements.errorBanner.hidden = true;
  }

  function renderHealthFailure(error) {
    showError(error);
    elements.statusPrimary.dataset.status = navigator.onLine ? "retrying" : "offline";
    elements.streamStatus.textContent = navigator.onLine
      ? "Health refresh interrupted"
      : "Dashboard offline";
    elements.streamChip.textContent = navigator.onLine ? "Retrying" : "Offline";
    if (!state.health) {
      elements.scannerStatus.setAttribute("aria-busy", "false");
    }
    if (state.lastSuccessfulAt) {
      elements.lastUpdated.textContent = `Last updated ${formatTime(state.lastSuccessfulAt)}`;
    }
  }

  function clearPollTimer() {
    if (state.pollTimer !== null) {
      window.clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }
  }

  function scheduleHealthPoll() {
    clearPollTimer();
    if (document.visibilityState === "hidden") {
      return;
    }
    state.pollTimer = window.setTimeout(() => refreshHealth(), HEALTH_POLL_MS);
  }

  async function refreshHealth({ force = false } = {}) {
    if (document.visibilityState === "hidden") {
      return;
    }
    if (state.healthController) {
      if (!force) {
        return;
      }
      state.healthController.abort();
    }

    clearPollTimer();
    const controller = new AbortController();
    const requestVersion = ++state.healthRequestVersion;
    state.healthController = controller;

    try {
      const health = await requestJson("/health", { signal: controller.signal });
      if (!health || typeof health !== "object" || !("status" in health)) {
        throw new Error("Invalid health response");
      }
      if (requestVersion !== state.healthRequestVersion) {
        return;
      }
      const previousOffset = state.health?.last_offset;
      const offsetAdvanced = (
        previousOffset !== null
        && previousOffset !== undefined
        && health.last_offset !== null
        && health.last_offset !== undefined
        && String(previousOffset) !== String(health.last_offset)
      );
      state.health = health;
      state.lastSuccessfulAt = new Date();
      renderHealth(health);
      hideError();
      if (offsetAdvanced) {
        state.historyOffset = 0;
        refreshFocusedData({ force: true });
      }
    } catch (error) {
      if (controller.signal.aborted || requestVersion !== state.healthRequestVersion) {
        return;
      }
      renderHealthFailure(error);
    } finally {
      if (requestVersion === state.healthRequestVersion) {
        state.healthController = null;
        scheduleHealthPoll();
      }
    }
  }

  function selectionSetsEqual(left, right) {
    if (left.size !== right.size) {
      return false;
    }
    for (const party of left) {
      if (!right.has(party)) {
        return false;
      }
    }
    return true;
  }

  function selectionManagementEnabled() {
    return state.selection?.selection_management_enabled === true;
  }

  function selectionIsDirty() {
    return !selectionSetsEqual(state.draftParties, state.desiredParties);
  }

  function setSelectionMessage(message = "", tone = "") {
    elements.selectionMessage.textContent = message;
    if (tone) {
      elements.selectionMessage.dataset.tone = tone;
    } else {
      delete elements.selectionMessage.dataset.tone;
    }
  }

  function renderSelectionControls() {
    const maximum = state.selection?.max_parties ?? 50;
    const selectedCount = state.draftParties.size;
    const dirty = selectionIsDirty();
    const canManage = selectionManagementEnabled() && state.adminUnlocked;
    const selectionValid = selectedCount > 0 && selectedCount <= maximum;

    elements.selectionCount.textContent = `${formatInteger(selectedCount)} selected`;
    if (!selectionManagementEnabled()) {
      elements.selectionLimit.textContent = "Selection management disabled";
    } else if (!state.adminUnlocked) {
      elements.selectionLimit.textContent = `Admin locked · maximum ${maximum}`;
    } else if (selectedCount >= maximum) {
      elements.selectionLimit.textContent = `Maximum ${maximum} reached`;
    } else {
      elements.selectionLimit.textContent = `Maximum ${maximum}`;
    }

    elements.resetSelection.disabled = !canManage || !dirty || state.selectionSaving;
    elements.saveSelection.disabled = (
      !canManage
      || !dirty
      || !selectionValid
      || state.selectionSaving
    );
    elements.saveSelection.textContent = state.selectionSaving ? "Saving…" : "Apply";
  }

  function renderAdminState(feedback = null, tone = "") {
    const managementEnabled = selectionManagementEnabled();
    elements.openAdminButton.dataset.unlocked = String(state.adminUnlocked);
    elements.adminLabel.textContent = state.adminUnlocked ? "Admin unlocked" : "Admin locked";
    elements.adminTokenInput.disabled = !managementEnabled;
    elements.unlockAdminButton.disabled = !managementEnabled;

    if (feedback !== null) {
      elements.adminFeedback.textContent = feedback;
      elements.adminFeedback.dataset.tone = tone;
    } else if (!managementEnabled) {
      elements.adminFeedback.textContent = (
        "Set SCANNER_ADMIN_TOKEN on the service to enable party changes."
      );
      elements.adminFeedback.dataset.tone = "error";
    } else {
      elements.adminFeedback.textContent = (
        "The token remains in memory only until this tab closes."
      );
      delete elements.adminFeedback.dataset.tone;
    }
  }

  function closeAdminDialog() {
    if (typeof elements.adminDialog.close === "function") {
      elements.adminDialog.close();
    } else {
      elements.adminDialog.removeAttribute("open");
    }
  }

  function openAdminDialog() {
    if (typeof elements.adminDialog.showModal === "function") {
      elements.adminDialog.showModal();
    } else {
      elements.adminDialog.setAttribute("open", "");
    }
    if (!elements.adminTokenInput.disabled) {
      elements.adminTokenInput.focus();
    }
  }

  function lockAdmin(feedback = null) {
    state.adminToken = null;
    state.adminUnlocked = false;
    elements.adminTokenInput.value = "";
    renderAdminState(feedback, feedback ? "error" : "");
    renderSelectionControls();
    renderPartyList();
  }

  function abbreviatedParty(party, length = 18) {
    if (!party) {
      return "Choose a party";
    }
    const prefix = party.split("::", 1)[0];
    return prefix.length > length ? `${prefix.slice(0, length)}…` : prefix;
  }

  function compactPartyId(party) {
    const [prefix, suffix = ""] = party.split("::", 2);
    if (!suffix) {
      return prefix;
    }
    return `${prefix.slice(0, 8)}…::…${suffix.slice(-7)}`;
  }

  function setFocusedParty(party, { updateUrl = true } = {}) {
    const nextParty = party || null;
    const changed = nextParty !== state.focusedParty;
    state.focusedParty = nextParty;
    elements.focusedParty.textContent = abbreviatedParty(state.focusedParty);
    elements.focusedParty.title = state.focusedParty ?? "";
    elements.focusedParty.parentElement?.setAttribute(
      "aria-label",
      state.focusedParty ? `Focused party ${state.focusedParty}` : "No focused party",
    );

    if (updateUrl && window.history && window.location) {
      const url = new URL(window.location.href);
      if (state.focusedParty) {
        url.searchParams.set("party", state.focusedParty);
      } else {
        url.searchParams.delete("party");
      }
      window.history.replaceState({}, "", url);
    }
    renderPartyList();
    if (changed) {
      state.balance = null;
      state.history = null;
      state.historyOffset = 0;
      refreshFocusedData({ force: true });
    }
  }

  function createElement(tag, className = "", text = "") {
    const element = document.createElement(tag);
    if (className) {
      element.className = className;
    }
    if (text) {
      element.textContent = text;
    }
    return element;
  }

  function setPanelState(element, message, tone = "") {
    element.textContent = message;
    if (tone) {
      element.dataset.tone = tone;
    } else {
      delete element.dataset.tone;
    }
  }

  function createDashboardEmpty(title, message) {
    const empty = createElement("div", "dashboard-empty");
    const copy = createElement("div");
    copy.append(
      createElement("strong", "", title),
      createElement("span", "", message),
    );
    empty.append(copy);
    return empty;
  }

  function decimalIsZero(value) {
    const [coefficient] = String(value ?? "").trim().replace(/^[+-]/, "").split(/[eE]/, 1);
    const digits = coefficient.replace(".", "");
    return /^0+$/.test(digits);
  }

  function renderBalanceLoading() {
    elements.balanceGrid.replaceChildren();
    for (let index = 0; index < 2; index += 1) {
      const card = createElement(
        "article",
        `balance-card${index === 0 ? " balance-card--primary" : ""}`,
      );
      card.append(
        createElement("span", "balance-card__instrument", "Instrument"),
        createElement("span", "skeleton skeleton--balance", "Loading balance"),
        createElement("span", "balance-card__caption", "Indexed amount"),
      );
      elements.balanceGrid.append(card);
    }
    elements.balanceGrid.setAttribute("aria-busy", "true");
    setPanelState(elements.balanceState, "Loading current balances…");
  }

  function renderBalanceEmpty(title, message, tone = "") {
    elements.balanceGrid.replaceChildren(createDashboardEmpty(title, message));
    elements.balanceGrid.setAttribute("aria-busy", "false");
    setPanelState(elements.balanceState, message, tone);
  }

  function renderBalanceInactive() {
    renderBalanceEmpty(
      "No current balance available",
      "This party is not actively indexed. Its retained transfer history remains available below.",
      "inactive",
    );
  }

  function renderBalance(response) {
    elements.balanceGrid.replaceChildren();
    if (!response.balances.length) {
      renderBalanceEmpty(
        "Zero active holdings",
        "The active contract set contains no current Holding balances for this party.",
      );
      return;
    }

    response.balances.forEach((balance, index) => {
      const instrument = balance.instrument || "Unspecified instrument";
      const amount = String(balance.amount);
      const card = createElement(
        "article",
        `balance-card${index === 0 ? " balance-card--primary" : ""}`,
      );
      const instrumentElement = createElement(
        "span",
        "balance-card__instrument",
        instrument,
      );
      instrumentElement.title = instrument;
      const amountElement = createElement("strong", "balance-card__amount", amount);
      amountElement.dataset.zero = String(decimalIsZero(amount));
      amountElement.title = amount;
      card.append(
        instrumentElement,
        amountElement,
        createElement(
          "span",
          "balance-card__caption",
          decimalIsZero(amount)
            ? "Indexed total · explicit zero"
            : `Indexed at offset ${formatInteger(response.last_offset)}`,
        ),
      );
      elements.balanceGrid.append(card);
    });
    elements.balanceGrid.setAttribute("aria-busy", "false");
    setPanelState(
      elements.balanceState,
      `${formatInteger(response.balances.length)} instrument${response.balances.length === 1 ? "" : "s"} · checkpoint ${formatInteger(response.last_offset)}`,
    );
  }

  function renderHistoryLoading() {
    elements.historyBody.replaceChildren();
    for (let rowIndex = 0; rowIndex < 3; rowIndex += 1) {
      const row = createElement("tr", "table-placeholder");
      for (let columnIndex = 0; columnIndex < 6; columnIndex += 1) {
        const cell = createElement("td");
        cell.append(createElement("span"));
        row.append(cell);
      }
      elements.historyBody.append(row);
    }
    elements.historyBody.setAttribute("aria-busy", "true");
    elements.historyPrevious.disabled = true;
    elements.historyNext.disabled = true;
    elements.historyPageStatus.textContent = "Page —";
    setPanelState(elements.historyState, "Loading transfer activity…");
  }

  function renderHistoryEmpty(title, message, tone = "") {
    const row = createElement("tr", "history-empty-row");
    const cell = createElement("td");
    cell.colSpan = 6;
    const copy = createElement("div");
    copy.append(
      createElement("strong", "", title),
      createElement("span", "", ` ${message}`),
    );
    cell.append(copy);
    row.append(cell);
    elements.historyBody.replaceChildren(row);
    elements.historyBody.setAttribute("aria-busy", "false");
    elements.historyPrevious.disabled = true;
    elements.historyNext.disabled = true;
    elements.historyPageStatus.textContent = "Page 1 of 1";
    elements.historySummary.textContent = "Only confidently reconstructed transfers appear here.";
    setPanelState(elements.historyState, message, tone);
  }

  function formatRecordTime(value) {
    if (!value) {
      return "Not recorded";
    }
    const timestamp = new Date(value);
    if (Number.isNaN(timestamp.getTime())) {
      return String(value);
    }
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(timestamp);
  }

  async function copyParty(button, party) {
    try {
      if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") {
        throw new Error("Clipboard unavailable");
      }
      await navigator.clipboard.writeText(party);
      button.textContent = "Copied";
      button.setAttribute("aria-label", `Copied full party ID ${party}`);
      window.setTimeout(() => {
        button.textContent = "Copy";
        button.setAttribute("aria-label", `Copy full party ID ${party}`);
      }, 1_500);
    } catch (_error) {
      button.textContent = "Unavailable";
      button.setAttribute("aria-label", `Unable to copy full party ID ${party}`);
    }
  }

  function createTransferRow(transfer) {
    const direction = ["sent", "received", "self"].includes(transfer.direction)
      ? transfer.direction
      : "self";
    const row = createElement("tr", "transfer-row");

    const directionCell = createElement("td");
    directionCell.append(
      createElement(
        "span",
        `transfer-direction transfer-direction--${direction}`,
        direction === "self" ? "Self" : direction,
      ),
    );

    const counterparty = transfer.counterparty || "Unknown party";
    const counterpartyCell = createElement("td");
    const counterpartyWrap = createElement("div", "counterparty-cell");
    const counterpartyId = createElement("code", "", compactPartyId(counterparty));
    counterpartyId.title = counterparty;
    const copyButton = createElement("button", "copy-party", "Copy");
    copyButton.type = "button";
    copyButton.title = counterparty;
    copyButton.setAttribute("aria-label", `Copy full party ID ${counterparty}`);
    copyButton.addEventListener("click", () => copyParty(copyButton, counterparty));
    counterpartyWrap.append(counterpartyId, copyButton);
    counterpartyCell.append(counterpartyWrap);

    const amount = String(transfer.amount);
    const amountCell = createElement("td", "transfer-amount", amount);
    amountCell.title = amount;
    const instrumentCell = createElement(
      "td",
      "transfer-instrument",
      transfer.instrument || "Unspecified",
    );
    const timeCell = createElement("td", "transfer-time");
    const time = createElement("time", "", formatRecordTime(transfer.record_time));
    if (transfer.record_time) {
      time.dateTime = transfer.record_time;
      time.title = transfer.record_time;
    }
    timeCell.append(time);
    const offsetCell = createElement("td", "transfer-offset", formatInteger(transfer.offset));
    offsetCell.title = String(transfer.offset ?? "");

    row.append(
      directionCell,
      counterpartyCell,
      amountCell,
      instrumentCell,
      timeCell,
      offsetCell,
    );
    return row;
  }

  function renderHistory(response) {
    elements.historyBody.replaceChildren();
    if (!response.transfers.length) {
      renderHistoryEmpty(
        "No semantic transfers",
        "No confidently reconstructed transfers are stored for this party.",
        response.active ? "" : "inactive",
      );
      return;
    }

    for (const transfer of response.transfers) {
      elements.historyBody.append(createTransferRow(transfer));
    }
    elements.historyBody.setAttribute("aria-busy", "false");

    const limit = Number(response.limit) || state.historyLimit;
    const offset = Number(response.offset) || 0;
    const total = Number(response.total) || 0;
    const page = Math.floor(offset / limit) + 1;
    const pageCount = Math.max(1, Math.ceil(total / limit));
    const first = total ? offset + 1 : 0;
    const last = Math.min(offset + response.transfers.length, total);
    elements.historyPrevious.disabled = offset === 0;
    elements.historyNext.disabled = offset + limit >= total;
    elements.historyPageStatus.textContent = `Page ${page} of ${pageCount}`;
    elements.historySummary.textContent = `Showing ${formatInteger(first)}–${formatInteger(last)} of ${formatInteger(total)} transfers`;
    setPanelState(
      elements.historyState,
      response.active
        ? `Current activity · checkpoint ${formatInteger(response.last_offset)}`
        : `Historical activity retained · indexing inactive · checkpoint ${formatInteger(response.last_offset)}`,
      response.active ? "" : "inactive",
    );
  }

  async function loadBalance({ force = false } = {}) {
    const party = state.focusedParty;
    const active = party && state.activeParties.has(party);
    if (state.balanceController) {
      if (!force) {
        return;
      }
      state.balanceController.abort();
    }
    if (!party) {
      renderBalanceEmpty(
        "Choose a party",
        "Focus an authorized party to inspect its current balances.",
      );
      return;
    }
    if (!active) {
      renderBalanceInactive();
      return;
    }

    if (!state.balance || state.balance.party !== party) {
      renderBalanceLoading();
    } else {
      setPanelState(elements.balanceState, "Refreshing current balances…");
    }
    const controller = new AbortController();
    const requestVersion = ++state.balanceRequestVersion;
    state.balanceController = controller;
    try {
      const response = await requestJson(`/balance/${encodeURIComponent(party)}`, {
        signal: controller.signal,
      });
      if (!response || !Array.isArray(response.balances)) {
        throw new Error("Invalid balance response");
      }
      if (requestVersion !== state.balanceRequestVersion || party !== state.focusedParty) {
        return;
      }
      state.balance = response;
      renderBalance(response);
    } catch (error) {
      if (controller.signal.aborted || requestVersion !== state.balanceRequestVersion) {
        return;
      }
      if (error instanceof ApiError && error.status === 409) {
        state.activeParties.delete(party);
        renderBalanceInactive();
        renderPartyList();
        return;
      }
      if (!state.balance || state.balance.party !== party) {
        renderBalanceEmpty(
          "Balance unavailable",
          "The current balance could not be loaded. Retry in a moment.",
          "error",
        );
      } else {
        setPanelState(
          elements.balanceState,
          "Balance refresh failed; the last indexed value remains visible.",
          "error",
        );
      }
      showError(error);
    } finally {
      if (requestVersion === state.balanceRequestVersion) {
        state.balanceController = null;
      }
    }
  }

  async function loadHistory({ force = false } = {}) {
    const party = state.focusedParty;
    if (state.historyController) {
      if (!force) {
        return;
      }
      state.historyController.abort();
    }
    if (!party) {
      renderHistoryEmpty(
        "Choose a party",
        "Focus an authorized party to inspect retained transfer activity.",
      );
      return;
    }

    const requestedOffset = state.historyOffset;
    if (
      !state.history
      || state.history.party !== party
      || Number(state.history.offset) !== requestedOffset
    ) {
      renderHistoryLoading();
    } else {
      setPanelState(elements.historyState, "Refreshing transfer activity…");
    }
    const controller = new AbortController();
    const requestVersion = ++state.historyRequestVersion;
    state.historyController = controller;
    const parameters = new URLSearchParams({
      limit: String(state.historyLimit),
      offset: String(requestedOffset),
    });
    try {
      const response = await requestJson(
        `/history/${encodeURIComponent(party)}?${parameters}`,
        { signal: controller.signal },
      );
      if (!response || !Array.isArray(response.transfers)) {
        throw new Error("Invalid history response");
      }
      if (requestVersion !== state.historyRequestVersion || party !== state.focusedParty) {
        return;
      }
      state.history = response;
      state.historyOffset = Number(response.offset) || requestedOffset;
      renderHistory(response);
    } catch (error) {
      if (controller.signal.aborted || requestVersion !== state.historyRequestVersion) {
        return;
      }
      if (!state.history || state.history.party !== party) {
        renderHistoryEmpty(
          "History unavailable",
          "Transfer activity could not be loaded. Retry in a moment.",
          "error",
        );
      } else {
        setPanelState(
          elements.historyState,
          "History refresh failed; the last indexed page remains visible.",
          "error",
        );
      }
      showError(error);
    } finally {
      if (requestVersion === state.historyRequestVersion) {
        state.historyController = null;
      }
    }
  }

  async function refreshFocusedData({ force = false, refreshHistory = true } = {}) {
    const requests = [loadBalance({ force })];
    if (refreshHistory) {
      requests.push(loadHistory({ force }));
    }
    await Promise.allSettled(requests);
  }

  function clearDashboardPoll() {
    if (state.dashboardPollTimer !== null) {
      window.clearTimeout(state.dashboardPollTimer);
      state.dashboardPollTimer = null;
    }
  }

  function scheduleDashboardPoll() {
    clearDashboardPoll();
    if (document.visibilityState === "hidden") {
      return;
    }
    state.dashboardPollTimer = window.setTimeout(async () => {
      await refreshFocusedData({ refreshHistory: state.historyOffset === 0 });
      scheduleDashboardPoll();
    }, DASHBOARD_POLL_MS);
  }

  function createPartyBadge(text, modifier = "") {
    const badge = createElement(
      "span",
      `party-badge${modifier ? ` party-badge--${modifier}` : ""}`,
      text,
    );
    return badge;
  }

  function toggleDraftParty(party, checked) {
    const maximum = state.selection?.max_parties ?? 50;
    if (checked && state.draftParties.size >= maximum) {
      setSelectionMessage(`At most ${maximum} parties may be selected.`, "error");
      renderPartyList();
      return;
    }
    if (checked) {
      state.draftParties.add(party);
    } else {
      state.draftParties.delete(party);
    }
    setSelectionMessage();
    renderSelectionControls();
    renderPartyList();
  }

  function createPartyRow(item) {
    const selected = state.draftParties.has(item.party);
    const active = state.activeParties.has(item.party) || item.active === true;
    const canToggle = (
      state.adminUnlocked
      && selectionManagementEnabled()
      && (item.readable || selected)
    );
    const atMaximum = (
      state.draftParties.size >= (state.selection?.max_parties ?? 50)
      && !selected
    );

    const row = createElement("article", "party-row");
    row.dataset.focused = String(item.party === state.focusedParty);
    row.dataset.active = String(active);

    const checkLabel = createElement("label", "party-check");
    const checkbox = createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = selected;
    checkbox.disabled = !canToggle || atMaximum;
    checkbox.setAttribute(
      "aria-label",
      `${selected ? "Remove" : "Select"} ${item.display_name || item.party}`,
    );
    checkbox.addEventListener("change", () => {
      toggleDraftParty(item.party, checkbox.checked);
    });
    checkLabel.append(checkbox);

    const focusButton = createElement("button", "party-focus");
    focusButton.type = "button";
    focusButton.title = item.party;
    focusButton.setAttribute("aria-pressed", String(item.party === state.focusedParty));
    focusButton.setAttribute("aria-label", `Focus party ${item.display_name || item.party}`);
    focusButton.addEventListener("click", () => setFocusedParty(item.party));

    const avatar = createElement(
      "span",
      "party-avatar",
      (item.display_name || item.party).slice(0, 2).toUpperCase(),
    );
    avatar.setAttribute("aria-hidden", "true");

    const identity = createElement("span", "party-identity");
    identity.append(
      createElement("strong", "", item.display_name || abbreviatedParty(item.party)),
      createElement("code", "", compactPartyId(item.party)),
    );
    const badges = createElement("span", "party-badges");
    if (active) {
      badges.append(createPartyBadge("Active", "active"));
    }
    if (selected) {
      badges.append(createPartyBadge("Desired", "selected"));
    }
    if (!item.readable) {
      badges.append(createPartyBadge("Inaccessible", "inaccessible"));
    }
    if (!active && !selected && item.readable) {
      badges.append(createPartyBadge(item.is_local === false ? "Explicit" : "Readable"));
    }
    identity.append(badges);
    focusButton.append(avatar, identity);
    row.append(checkLabel, focusButton);
    return row;
  }

  function renderPartyList() {
    if (!elements.partyList) {
      return;
    }
    elements.partyList.replaceChildren();
    if (!state.parties.length) {
      const message = state.partyQuery
        ? `No authorized parties match “${state.partyQuery}”.`
        : "No parties are currently available in the cached catalog.";
      elements.partyList.append(createElement("p", "party-empty", message));
    } else {
      for (const item of state.parties) {
        elements.partyList.append(createPartyRow(item));
      }
    }
    elements.partyList.setAttribute("aria-busy", "false");

    const currentPage = Math.floor(state.partyOffset / state.partyLimit) + 1;
    const pageCount = Math.max(1, Math.ceil(state.partyTotal / state.partyLimit));
    elements.partyTotalCount.textContent = formatInteger(state.partyTotal);
    elements.partyTotalCount.setAttribute(
      "aria-label",
      `${formatInteger(state.partyTotal)} matching parties`,
    );
    elements.partyPageStatus.textContent = `Page ${currentPage} of ${pageCount}`;
    elements.partyPrevious.disabled = state.partyOffset === 0;
    elements.partyNext.disabled = state.partyOffset + state.partyLimit >= state.partyTotal;
    renderSelectionControls();
  }

  async function loadSelection() {
    try {
      const selection = await requestJson("/parties/selection");
      if (
        !selection
        || !Array.isArray(selection.desired_parties)
        || !Array.isArray(selection.active_parties)
      ) {
        throw new Error("Invalid selection response");
      }
      state.selection = selection;
      state.desiredParties = new Set(selection.desired_parties);
      state.activeParties = new Set(selection.active_parties);
      state.draftParties = new Set(selection.desired_parties);

      if (!state.focusedParty) {
        let requestedParty = null;
        if (window.location) {
          requestedParty = new URL(window.location.href).searchParams.get("party");
        }
        setFocusedParty(
          requestedParty
          || selection.active_parties[0]
          || selection.desired_parties[0]
          || null,
          { updateUrl: false },
        );
      }
      renderAdminState();
      renderSelectionControls();
    } catch (error) {
      showError(error);
      state.selection = {
        desired_parties: [],
        active_parties: [],
        max_parties: 50,
        selection_management_enabled: false,
      };
      renderAdminState();
      renderSelectionControls();
    }
  }

  async function loadParties() {
    if (state.partyController) {
      state.partyController.abort();
    }
    const controller = new AbortController();
    const requestVersion = ++state.partyRequestVersion;
    state.partyController = controller;
    elements.partyList.setAttribute("aria-busy", "true");

    const parameters = new URLSearchParams({
      limit: String(state.partyLimit),
      offset: String(state.partyOffset),
    });
    if (state.partyQuery) {
      parameters.set("q", state.partyQuery);
    }

    try {
      const response = await requestJson(`/parties?${parameters}`, {
        signal: controller.signal,
      });
      if (!response || !Array.isArray(response.items)) {
        throw new Error("Invalid party catalog response");
      }
      if (requestVersion !== state.partyRequestVersion) {
        return;
      }
      state.parties = response.items;
      state.partyTotal = response.total ?? 0;
      renderPartyList();
    } catch (error) {
      if (controller.signal.aborted || requestVersion !== state.partyRequestVersion) {
        return;
      }
      showError(error);
      if (!state.parties.length) {
        state.partyTotal = 0;
        renderPartyList();
      }
    } finally {
      if (requestVersion === state.partyRequestVersion) {
        state.partyController = null;
        elements.partySearch.disabled = false;
        elements.partyList.setAttribute("aria-busy", "false");
      }
    }
  }

  async function saveSelection() {
    if (
      !state.adminUnlocked
      || !state.adminToken
      || !selectionIsDirty()
      || state.selectionSaving
    ) {
      return;
    }
    const maximum = state.selection?.max_parties ?? 50;
    if (!state.draftParties.size || state.draftParties.size > maximum) {
      setSelectionMessage(`Select between 1 and ${maximum} parties.`, "error");
      return;
    }

    state.selectionSaving = true;
    renderSelectionControls();
    setSelectionMessage("Saving desired party selection…");
    try {
      const response = await requestJson("/parties/selection", {
        method: "PUT",
        headers: {
          Authorization: `Bearer ${state.adminToken}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ parties: [...state.draftParties].sort() }),
      });
      state.selection = {
        ...state.selection,
        ...response,
      };
      state.desiredParties = new Set(response.desired_parties ?? []);
      state.activeParties = new Set(response.active_parties ?? []);
      state.draftParties = new Set(response.desired_parties ?? []);
      setSelectionMessage(
        response.restart_required
          ? "Selection saved. Scanner reconciliation is now required."
          : "Selection saved and active.",
        "success",
      );
      await loadParties();
      refreshHealth({ force: true });
    } catch (error) {
      if (error instanceof ApiError && error.status === 403) {
        lockAdmin("That admin token was rejected. Try again.");
        openAdminDialog();
      }
      setSelectionMessage(friendlyError(error).message, "error");
      showError(error);
    } finally {
      state.selectionSaving = false;
      renderSelectionControls();
    }
  }

  function setupPartyExplorer() {
    elements.partySearch.addEventListener("input", () => {
      if (state.partySearchTimer !== null) {
        window.clearTimeout(state.partySearchTimer);
      }
      state.partySearchTimer = window.setTimeout(() => {
        state.partyQuery = elements.partySearch.value.trim();
        state.partyOffset = 0;
        loadParties();
      }, 250);
    });
    elements.partyPrevious.addEventListener("click", () => {
      state.partyOffset = Math.max(0, state.partyOffset - state.partyLimit);
      loadParties();
    });
    elements.partyNext.addEventListener("click", () => {
      if (state.partyOffset + state.partyLimit < state.partyTotal) {
        state.partyOffset += state.partyLimit;
        loadParties();
      }
    });
    elements.resetSelection.addEventListener("click", () => {
      state.draftParties = new Set(state.desiredParties);
      setSelectionMessage("Unsaved changes discarded.");
      renderPartyList();
    });
    elements.saveSelection.addEventListener("click", saveSelection);
    document.addEventListener("keydown", (event) => {
      if (
        (event.metaKey || event.ctrlKey)
        && event.key.toLowerCase() === "k"
        && !elements.partySearch.disabled
      ) {
        event.preventDefault();
        elements.partySearch.focus();
      }
    });
  }

  function setupDashboard() {
    elements.historyPrevious.addEventListener("click", () => {
      state.historyOffset = Math.max(0, state.historyOffset - state.historyLimit);
      loadHistory({ force: true });
    });
    elements.historyNext.addEventListener("click", () => {
      const total = Number(state.history?.total) || 0;
      if (state.historyOffset + state.historyLimit < total) {
        state.historyOffset += state.historyLimit;
        loadHistory({ force: true });
      }
    });
  }

  function setupAdminDialog() {
    elements.openAdminButton.addEventListener("click", openAdminDialog);
    elements.closeAdminButton.addEventListener("click", closeAdminDialog);
    elements.adminDialog.addEventListener("click", (event) => {
      if (event.target === elements.adminDialog) {
        closeAdminDialog();
      }
    });
    elements.adminForm.addEventListener("submit", (event) => {
      event.preventDefault();
      if (!selectionManagementEnabled()) {
        renderAdminState();
        return;
      }
      const token = elements.adminTokenInput.value;
      if (!token) {
        renderAdminState("Enter the scanner admin token.", "error");
        return;
      }
      state.adminToken = token;
      state.adminUnlocked = true;
      elements.adminTokenInput.value = "";
      renderAdminState("Selection controls are unlocked for this tab.", "success");
      renderSelectionControls();
      renderPartyList();
      closeAdminDialog();
    });
    renderAdminState();
  }

  async function initializePartyExplorer() {
    elements.partySearch.disabled = true;
    await loadSelection();
    await loadParties();
    await refreshFocusedData({ force: true });
    scheduleDashboardPoll();
  }

  elements.retryButton.addEventListener("click", () => {
    refreshHealth({ force: true });
    refreshFocusedData({ force: true });
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") {
      clearPollTimer();
      clearDashboardPoll();
      if (state.healthController) {
        state.healthController.abort();
      }
      if (state.balanceController) {
        state.balanceController.abort();
      }
      if (state.historyController) {
        state.historyController.abort();
      }
      return;
    }
    refreshHealth({ force: true });
    refreshFocusedData({ force: true });
    scheduleDashboardPoll();
  });
  window.addEventListener("online", () => {
    refreshHealth({ force: true });
    refreshFocusedData({ force: true });
  });
  window.addEventListener("offline", () => renderHealthFailure(new Error("offline")));
  window.addEventListener("beforeunload", () => {
    clearPollTimer();
    clearDashboardPoll();
    if (state.healthController) {
      state.healthController.abort();
    }
    if (state.balanceController) {
      state.balanceController.abort();
    }
    if (state.historyController) {
      state.historyController.abort();
    }
  });

  setupAdminDialog();
  setupPartyExplorer();
  setupDashboard();
  refreshHealth();
  initializePartyExplorer();
})();
