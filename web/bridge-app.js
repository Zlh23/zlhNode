/** Web Bridge：4×3 跑图页；出图写入服务端 stash 供主页导入。 */
const SLOT_COLS = 4;
const SLOT_ROWS = 3;
const SLOT_COUNT = SLOT_COLS * SLOT_ROWS;

/** @type {{ payloadText: string, apiBase: string, workflowName: string, images: Array<{ mime?: string, data_base64: string }>, selectedIndex: number }[]} */
let slots = [];
let activeRunSlot = 0;
let modalOpenSlot = -1;

function bridgeApiRoot() {
  const el = document.getElementById("modalBaseUrl");
  if (el) {
    const raw = el.value.trim();
    if (raw) return raw.replace(/\/$/, "");
  }
  return defaultApiBase();
}

async function refreshWorkflows(preferredWorkflowName) {
  const sel = document.getElementById("modalWorkflowSelect");
  const st = document.getElementById("modalStatus");
  const data = await fetchJson(`${bridgeApiRoot()}/bridge/workflows`);
  sel.innerHTML = "";
  const names = data.workflows || [];
  if (names.length === 0) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "（无）请在 Comfy 保存工作流";
    sel.appendChild(opt);
    if (st && !st.textContent.includes("排队") && !st.textContent.includes("等待图像")) {
      st.textContent = "工作流列表为空";
    }
    return;
  }
  for (const n of names) {
    const opt = document.createElement("option");
    opt.value = n;
    opt.textContent = n;
    sel.appendChild(opt);
  }
  if (preferredWorkflowName && names.includes(preferredWorkflowName)) {
    sel.value = preferredWorkflowName;
  }
}

function persistModalToSlot(si) {
  if (si < 0 || si >= SLOT_COUNT) return;
  const s = slots[si];
  const ta = document.getElementById("modalPayload");
  const apiEl = document.getElementById("modalBaseUrl");
  const wfEl = document.getElementById("modalWorkflowSelect");
  if (ta) s.payloadText = ta.value;
  if (apiEl) s.apiBase = apiEl.value.trim();
  if (wfEl) s.workflowName = wfEl.value;
}

async function pollOutput(sessionKey, { timeoutMs = 900000, intervalMs = 400 } = {}) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const data = await fetchJson(`${bridgeApiRoot()}/bridge/output/${encodeURIComponent(sessionKey)}`);
    const imgs = data.images || [];
    const tx = typeof data.text === "string" ? data.text : "";
    if (data.ready && (imgs.length > 0 || tx.length > 0)) {
      return { images: imgs, text: tx };
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  throw new Error("等待输出超时");
}

function ensureSlots() {
  if (slots.length === SLOT_COUNT) return;
  slots = Array.from({ length: SLOT_COUNT }, () => ({
    payloadText: "",
    apiBase: "",
    workflowName: "",
    images: [],
    outputText: "",
    selectedIndex: 0,
  }));
}

function renderSlotGrid() {
  ensureSlots();
  const grid = document.getElementById("slotGrid");
  grid.innerHTML = "";
  for (let i = 0; i < SLOT_COUNT; i++) {
    const cell = document.createElement("button");
    cell.type = "button";
    cell.className = "slot-cell";
    if (i === activeRunSlot) cell.classList.add("is-active");
    cell.dataset.slotIndex = String(i);
    cell.setAttribute("aria-label", `格子 ${i + 1}`);

    const ph = document.createElement("div");
    ph.className = "slot-placeholder";
    ph.textContent = `#${i + 1}`;
    cell.appendChild(ph);

    const s = slots[i];
    const imgs = s.images || [];
    const idx = Math.min(s.selectedIndex || 0, Math.max(0, imgs.length - 1));
    if (imgs.length > 0) {
      cell.classList.add("has-image");
      const img = document.createElement("img");
      img.alt = `slot-${i + 1}`;
      img.src = `data:${imgs[idx].mime || "image/png"};base64,${imgs[idx].data_base64}`;
      cell.appendChild(img);
    }

    cell.addEventListener("click", (e) => {
      e.preventDefault();
      if (modalOpenSlot >= 0) {
        persistModalToSlot(modalOpenSlot);
      }
      activeRunSlot = i;
      renderSlotGrid();
      void openModal(i).catch((e) => {
        const st = document.getElementById("modalStatus");
        if (st) st.textContent = "打开设置失败: " + e.message;
      });
    });

    grid.appendChild(cell);
  }
}

async function openModal(slotIndex) {
  if (modalOpenSlot >= 0 && modalOpenSlot !== slotIndex) {
    persistModalToSlot(modalOpenSlot);
  }
  modalOpenSlot = slotIndex;
  const modal = document.getElementById("modalBackdrop");
  const title = document.getElementById("modalTitle");
  const apiEl = document.getElementById("modalBaseUrl");
  const ta = document.getElementById("modalPayload");
  const thumbs = document.getElementById("modalThumbs");
  const noImg = document.getElementById("modalNoImages");
  const st = document.getElementById("modalStatus");

  title.textContent = `格子 ${slotIndex + 1}`;
  const s = slots[slotIndex];
  apiEl.value = s.apiBase || (defaultApiBase() !== window.location.origin ? defaultApiBase() : "");
  ta.value = s.payloadText;

  try {
    await refreshWorkflows(s.workflowName || null);
  } catch (e) {
    if (st) st.textContent = "加载工作流失败: " + e.message;
  }
  const wfSel = document.getElementById("modalWorkflowSelect");
  if (s.workflowName && wfSel && Array.from(wfSel.options).some((o) => o.value === s.workflowName)) {
    wfSel.value = s.workflowName;
  }

  thumbs.innerHTML = "";
  const imgs = s.images || [];
  if (imgs.length === 0) {
    noImg.classList.remove("hidden");
  } else {
    noImg.classList.add("hidden");
    const sel = Math.min(s.selectedIndex || 0, imgs.length - 1);
    imgs.forEach((item, idx) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "modal-thumb";
      if (idx === sel) b.classList.add("is-selected");
      const img = document.createElement("img");
      img.alt = `out-${idx}`;
      img.src = `data:${item.mime || "image/png"};base64,${item.data_base64}`;
      b.appendChild(img);
      b.addEventListener("click", (ev) => {
        ev.stopPropagation();
        persistModalToSlot(slotIndex);
        slots[slotIndex].selectedIndex = idx;
        renderSlotGrid();
        void openModal(slotIndex).catch(() => {});
      });
      thumbs.appendChild(b);
    });
  }

  modal.classList.remove("hidden");
}

function closeModal() {
  if (modalOpenSlot >= 0) {
    persistModalToSlot(modalOpenSlot);
  }
  modalOpenSlot = -1;
  document.getElementById("modalBackdrop").classList.add("hidden");
}

async function run() {
  if (modalOpenSlot < 0) return;
  persistModalToSlot(modalOpenSlot);
  const si = modalOpenSlot;
  const status = document.getElementById("modalStatus");
  const btn = document.getElementById("modalBtnRun");
  const wf = document.getElementById("modalWorkflowSelect").value;
  if (!wf) {
    status.textContent = "请选择工作流";
    return;
  }
  slots[si].workflowName = wf;
  btn.disabled = true;
  status.textContent = "排队运行…";
  try {
    const body = {
      workflow_name: wf,
      input: slots[si].payloadText,
    };
    const queued = await fetchJson(`${bridgeApiRoot()}/bridge/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const sessionKey = queued.session_key;
    status.textContent = `已排队 prompt_id=${queued.prompt_id}，等待图像…`;
    const out = await pollOutput(sessionKey);
    slots[si].images = out.images || [];
    slots[si].outputText = out.text || "";
    slots[si].selectedIndex = 0;
    persistModalToSlot(si);
    slots[si].payloadText = document.getElementById("modalPayload").value;
    renderSlotGrid();
    await openModal(si);
    status.textContent = "完成";
  } catch (e) {
    status.textContent = "错误: " + e.message;
  } finally {
    btn.disabled = false;
  }
}

document.getElementById("modalBtnRefresh").addEventListener("click", () => {
  const st = document.getElementById("modalStatus");
  const cur =
    modalOpenSlot >= 0 ? slots[modalOpenSlot].workflowName : "";
  refreshWorkflows(cur || null).catch((e) => {
    if (st) st.textContent = "刷新失败: " + e.message;
  });
});

document.getElementById("modalBtnRun").addEventListener("click", () => {
  run();
});

document.getElementById("modalClose").addEventListener("click", () => {
  closeModal();
});

document.getElementById("modalBackdrop").addEventListener("click", (e) => {
  if (e.target.id === "modalBackdrop") closeModal();
});

document.getElementById("modalPanel").addEventListener("click", (e) => {
  e.stopPropagation();
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !document.getElementById("modalBackdrop").classList.contains("hidden")) {
    closeModal();
  }
});

document.addEventListener("DOMContentLoaded", () => {
  ensureSlots();
  renderSlotGrid();
});
