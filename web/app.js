/**
 * 数据集主页：图片按 outfit 分组显示，每张图片自带 outfit/scene 元数据。
 */

const POLL_MS = 2000;

/** @type {ReturnType<typeof setInterval> | null} */
let pollTimer = null;

/** 上一次 poll 获取到的 server hash，避免无意义刷新 */
let serverHash = "";

/**
 * @typedef {{ fid: string, outfit: string, scene: string }} ImageEntry
 * @type {ImageEntry[]}
 */
let images = [];

/** 大图预览/编辑是否打开 */
let editOpen = false;
/** 当前正在编辑的图片 index（在 images 中的下标） */
let editIndex = -1;

const imageRev = new Map();

let saveTimer = null;
let stateLoaded = false;

function setToolbarStatus(msg) {
  const st = document.getElementById("toolbarStatus");
  if (st) st.textContent = msg || "";
}

function bumpImageRev(fileId) {
  if (!fileId || typeof fileId !== "string") return;
  imageRev.set(fileId, Date.now());
}

function imageUrl(fileId) {
  const rev = imageRev.get(fileId) ?? 0;
  return `${apiRoot()}/bridge/dataset/image/${fileId}?v=${rev}`;
}

function applyStatePayload(data) {
  const raw = data.images || [];
  images = raw
    .filter((e) => e && typeof e === "object" && e.fid && typeof e.fid === "string")
    .map((e) => ({
      fid: e.fid,
      outfit: typeof e.outfit === "string" ? e.outfit : "",
      scene: typeof e.scene === "string" ? e.scene : "",
    }));
}

async function loadStateFromServer() {
  const data = await fetchJson(`${apiRoot()}/bridge/dataset/state`);
  serverHash = data.hash || "";
  applyStatePayload(data);
  stateLoaded = true;
}

async function pushStateToServer() {
  if (!stateLoaded) return;
  const data = await fetchJson(`${apiRoot()}/bridge/dataset/state`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ images }),
  });
  if (data && data.hash) serverHash = data.hash;
}

function scheduleSaveState() {
  if (!stateLoaded) return;
  if (saveTimer) clearTimeout(saveTimer);
  saveTimer = setTimeout(flushSaveState, 450);
}

async function flushSaveState() {
  saveTimer = null;
  if (!stateLoaded) return;
  try {
    await pushStateToServer();
  } catch (e) {
    setToolbarStatus(e.message || "save");
  }
}

// ==================== 网格渲染 ====================

function arraysEqualStr(a, b) {
  if (a.length !== b.length) return false;
  return a.every((x, i) => x === b[i]);
}

/** 按 outfit 分组，返回 { outfit, entries: ImageEntry[] }[] */
function groupByOutfit() {
  const map = new Map();
  for (const entry of images) {
    const key = entry.outfit || "";
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(entry);
  }
  const result = [];
  for (const [outfit, entries] of map) {
    result.push({ outfit, entries });
  }
  return result;
}

/** 增量比对两个 ImageEntry 数组，返回需要执行的操作 */
function diffImages(oldArr, newArr) {
  const oldMap = new Map(oldArr.map((e) => [e.fid, e]));
  const newMap = new Map(newArr.map((e) => [e.fid, e]));

  /** @type {{ fid: string, entry: ImageEntry }[]} */
  const added = [];
  /** @type {{ fid: string, entry: ImageEntry }[]} */
  const removed = [];
  /** @type {{ fid: string, entry: ImageEntry }[]} */
  const changed = [];

  // 新增 + 修改
  for (const [fid, entry] of newMap) {
    if (!oldMap.has(fid)) {
      added.push({ fid, entry });
    } else {
      const old = oldMap.get(fid);
      if (old.outfit !== entry.outfit || old.scene !== entry.scene) {
        changed.push({ fid, entry });
      }
    }
  }

  // 删除
  for (const [fid] of oldMap) {
    if (!newMap.has(fid)) {
      removed.push({ fid, entry: oldMap.get(fid) });
    }
  }

  return { added, removed, changed };
}

/**
 * 根据 diff 结果增量更新 DOM，不重建整体 grid。
 * 注意：outfit 分组变化可能涉及行 id 变化（outfit 值变了），
 * 但绝大多数时候 outfit 变动会触发 save -> 增量场景主要是 Blender 新增图片。
 * 我们用简单策略：如果 added/removed/changed 总操作数 < images.length 的一半，
 * 就走 DOM 级增量；否则 fallback 到全量 renderGrid。
 */
function applyIncrementalDiff(diff) {
  const totalOps = diff.added.length + diff.removed.length + diff.changed.length;
  if (totalOps === 0) return;

  const grid = document.getElementById("homeGrid");
  if (!grid) return;

  // 如果操作数 >= 当前图片数的一半，直接全量渲染更简单
  if (totalOps >= images.length * 0.5) {
    renderGrid();
    return;
  }

  // --- 删除卡片 ---
  for (const { fid } of diff.removed) {
    const card = grid.querySelector(`.image-card[data-fid="${CSS.escape(fid)}"]`);
    if (card) {
      const row = card.closest(".image-row");
      card.remove();
      // 如果行内没有图片了，移除整行
      if (row) {
        const imgContainer = row.querySelector(".image-row-images");
        if (imgContainer && imgContainer.children.length === 0) {
          row.remove();
        }
      }
    }
  }

  // --- 新增卡片（可能需要新建行）---
  const groups = groupByOutfit();
  const rowMap = buildRowMap(grid, groups);

  for (const { entry, fid } of diff.added) {
    bumpImageRev(fid);
    const outfit = entry.outfit || "";
    let rowEl = rowMap.get(outfit);
    if (!rowEl) {
      rowEl = createRowElement(outfit, []);
      grid.appendChild(rowEl);
      rowMap.set(outfit, rowEl);
    }
    const imgContainer = rowEl.querySelector(".image-row-images");
    if (imgContainer) {
      const card = createCardElement(entry, images.findIndex((e) => e.fid === entry.fid));
      imgContainer.appendChild(card);
    }
  }

  // --- 修改 outfit / scene（重新创建卡片）---
  for (const { entry, fid } of diff.changed) {
    const oldCard = grid.querySelector(`.image-card[data-fid="${CSS.escape(fid)}"]`);
    if (!oldCard) continue;
    const row = oldCard.closest(".image-row");
    const oldImgContainer = row && row.querySelector(".image-row-images");
    if (!oldImgContainer) continue;

    const idx = images.findIndex((e) => e.fid === entry.fid);
    const newCard = createCardElement(entry, idx);

    // 如果 outfit 变了，需要移动到正确行
    const oldOutfit = oldCard.dataset.outfit || "";
    const newOutfit = entry.outfit || "";
    if (oldOutfit !== newOutfit) {
      // 从旧行删除
      oldCard.remove();
      // 如果旧行空了，删除旧行
      if (oldImgContainer.children.length === 0) {
        row.remove();
      }
      // 找到或创建新行
      const newGroups = groupByOutfit();
      const newRowMap = buildRowMap(grid, newGroups);
      let targetRow = newRowMap.get(newOutfit);
      if (!targetRow) {
        targetRow = createRowElement(newOutfit, []);
        // 按 outfit 顺序插入
        const rows = Array.from(grid.querySelectorAll(".image-row"));
        const insertIdx = newGroups.findIndex((g) => g.outfit === newOutfit);
        if (insertIdx >= 0 && insertIdx < rows.length) {
          grid.insertBefore(targetRow, rows[insertIdx]);
        } else {
          grid.appendChild(targetRow);
        }
        newRowMap.set(newOutfit, targetRow);
      }
      const newImgContainer = targetRow.querySelector(".image-row-images");
      if (newImgContainer) newImgContainer.appendChild(newCard);
    } else {
      oldCard.replaceWith(newCard);
    }
  }
}

/** 根据当前 groups 重建 <fid -> row> 映射，返回 Map<outfit, rowElement> */
function buildRowMap(grid, groups) {
  const map = new Map();
  const existingRows = grid.querySelectorAll(".image-row");
  // 先把已有行按 outfit 映射
  const rowByOutfit = new Map();
  for (const row of existingRows) {
    const input = row.querySelector(".image-row-outfit-input");
    if (input) {
      rowByOutfit.set(input.value, row);
    }
  }
  for (const group of groups) {
    const row = rowByOutfit.get(group.outfit);
    if (row) map.set(group.outfit, row);
  }
  return map;
}

function onVisibilityChange() {
  if (document.visibilityState === "visible") void pollFromServer();
}

async function pollFromServer() {
  if (!stateLoaded || document.visibilityState !== "visible") return;
  try {
    const data = await fetchJson(`${apiRoot()}/bridge/dataset/state`);
    const newHash = data.hash || "";
    if (newHash === serverHash) return; // hash 未变，跳过
    serverHash = newHash;

    const newImages = (data.images || []).filter((e) => e && typeof e === "object" && e.fid);
    const oldImages = images;
    const diff = diffImages(oldImages, newImages);
    applyStatePayload(data);

    if (editOpen) {
      // 编辑中不操作 DOM，只更新数据
    } else {
      applyIncrementalDiff(diff);
    }
  } catch {
    /* silent */
  }
}

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(() => void pollFromServer(), POLL_MS);
  document.addEventListener("visibilitychange", onVisibilityChange);
}

// ==================== 渲染图片网格 ====================

function mountGrid() {
  const grid = document.getElementById("homeGrid");
  if (!grid) return;
  grid.innerHTML = "";
  renderGrid();
}

/** 创建单张图片卡片 DOM */
function createCardElement(entry, idx) {
  const card = document.createElement("div");
  card.className = "image-card";
  card.dataset.fid = entry.fid;
  card.dataset.outfit = entry.outfit || "";

  const img = document.createElement("img");
  img.alt = "";
  img.src = imageUrl(entry.fid);
  card.appendChild(img);

  if (entry.scene) {
    const sceneLabel = document.createElement("div");
    sceneLabel.className = "image-card-scene";
    sceneLabel.textContent = entry.scene;
    card.appendChild(sceneLabel);
  }

  // 删除按钮
  const delBtn = document.createElement("button");
  delBtn.type = "button";
  delBtn.className = "image-card-delete";
  delBtn.textContent = "×";
  delBtn.title = "删除此图片";
  delBtn.addEventListener("click", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    void deleteImage(entry.fid);
  });
  card.appendChild(delBtn);

  // 点击打开编辑
  card.addEventListener("click", (ev) => {
    if (ev.target === delBtn) return;
    if (idx >= 0) openEdit(idx);
  });

  return card;
}

/** 创建一行 DOM，outfit 已固定 */
function createRowElement(outfit, entries) {
  const row = document.createElement("div");
  row.className = "image-row";

  // outfit 列
  const outfitCell = document.createElement("div");
  outfitCell.className = "image-row-outfit";

  const label = document.createElement("div");
  label.className = "image-row-outfit-label";
  label.textContent = "outfit";
  outfitCell.appendChild(label);

  const input = document.createElement("textarea");
  input.className = "image-row-outfit-input";
  input.rows = 1;
  input.spellcheck = false;
  input.value = outfit;
  input.addEventListener("input", () => {
    const newOutfit = input.value;
    // 找到同一行中所有属于这个 outfit 的 entry
    const rowEl = input.closest(".image-row");
    if (rowEl) {
      const cards = rowEl.querySelectorAll(".image-card");
      for (const card of cards) {
        const fid = card.dataset.fid;
        const entry = images.find((e) => e.fid === fid);
        if (entry) entry.outfit = newOutfit;
      }
    }
    scheduleSaveState();
  });
  outfitCell.appendChild(input);
  row.appendChild(outfitCell);

  // 图片列
  const imagesCell = document.createElement("div");
  imagesCell.className = "image-row-images";

  for (const entry of entries) {
    const idx = images.indexOf(entry);
    const card = createCardElement(entry, idx);
    imagesCell.appendChild(card);
  }

  row.appendChild(imagesCell);
  return row;
}

function renderGrid() {
  const grid = document.getElementById("homeGrid");
  if (!grid) return;

  // 清空
  grid.innerHTML = "";

  if (images.length === 0) {
    const empty = document.createElement("p");
    empty.className = "image-row-empty";
    empty.textContent = "暂无图片，请导入图片或从 Blender 渲染上传。";
    Object.assign(empty.style, {
      padding: "48px 20px",
      textAlign: "center",
      fontSize: "0.875rem",
      color: "rgba(255,255,255,0.32)",
      border: "1px dashed rgba(255,255,255,0.08)",
      borderRadius: "var(--zlh-radius)",
      background: "rgba(255,255,255,0.02)",
    });
    grid.appendChild(empty);
    return;
  }

  const groups = groupByOutfit();

  for (const group of groups) {
    const row = createRowElement(group.outfit, group.entries);
    grid.appendChild(row);
  }
}

// ==================== 图片操作 ====================

async function deleteImage(fid) {
  if (!fid) return;
  try {
    await fetchJson(`${apiRoot()}/bridge/dataset/image/${fid}`, { method: "DELETE" });
  } catch (e) {
    setToolbarStatus(e.message || "delete");
    return;
  }
  if (editOpen && editIndex >= 0 && images[editIndex] && images[editIndex].fid === fid) {
    closeEdit();
  }
  await loadStateFromServer();
  renderGrid();
  setToolbarStatus("");
}

async function deleteAllImages() {
  if (!confirm("清空全部图片？")) return;
  if (!confirm("再次确认：将删除所有图片，不可撤销。")) return;
  try {
    await fetchJson(`${apiRoot()}/bridge/dataset/clear-images`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  } catch (e) {
    setToolbarStatus(e.message || "clear");
    return;
  }
  closeEdit();
  await loadStateFromServer();
  renderGrid();
  setToolbarStatus("");
}

async function uploadImagesFromFiles(files) {
  const list = Array.from(files || []).filter((f) => f.type.startsWith("image/"));
  if (!list.length) return;
  try {
    for (const file of list) {
      const fr = new FileReader();
      const b64 = await new Promise((resolve, reject) => {
        fr.onload = () => {
          const s = /** @type {string} */ (fr.result);
          const m = /^data:[^;]+;base64,(.+)$/.exec(s);
          resolve(m ? m[1] : "");
        };
        fr.onerror = () => reject(new Error("read"));
        fr.readAsDataURL(file);
      });
      if (!b64) continue;
      const data = await fetchJson(`${apiRoot()}/bridge/dataset/image`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ image_base64: b64, outfit: "", scene: "" }),
      });
      if (data && data.id) bumpImageRev(String(data.id));
      applyStatePayload(data);
    }
    renderGrid();
    setToolbarStatus("");
  } catch (e) {
    await loadStateFromServer();
    renderGrid();
    setToolbarStatus(e.message || "upload");
  }
}

// ==================== 编辑面板 ====================

function fillEditPanel() {
  const imgEl = document.getElementById("imageEditImg");
  const emptyEl = document.getElementById("imageEditEmpty");
  const outfitInput = document.getElementById("imageEditOutfit");
  const sceneInput = document.getElementById("imageEditScene");
  if (!imgEl || !emptyEl || !outfitInput || !sceneInput) return;

  if (editIndex < 0 || editIndex >= images.length) {
    imgEl.classList.add("hidden");
    emptyEl.classList.remove("hidden");
    outfitInput.value = "";
    sceneInput.value = "";
    return;
  }

  const entry = images[editIndex];
  emptyEl.classList.add("hidden");
  imgEl.classList.remove("hidden");
  imgEl.src = imageUrl(entry.fid);
  outfitInput.value = entry.outfit;
  sceneInput.value = entry.scene;
}

function openEdit(index) {
  closeEdit(); // 先关闭并保存之前的

  editOpen = true;
  editIndex = index;
  fillEditPanel();

  const backdrop = document.getElementById("imageEditBackdrop");
  if (backdrop) backdrop.classList.remove("hidden");
}

function closeEdit() {
  if (editIndex >= 0 && editIndex < images.length) {
    const outfitInput = /** @type {HTMLInputElement | null} */ (document.getElementById("imageEditOutfit"));
    const sceneInput = /** @type {HTMLTextAreaElement | null} */ (document.getElementById("imageEditScene"));
    if (outfitInput && sceneInput) {
      const entry = images[editIndex];
      if (entry) {
        entry.outfit = outfitInput.value;
        entry.scene = sceneInput.value;
        scheduleSaveState();
      }
    }
  }

  editOpen = false;
  editIndex = -1;
  const backdrop = document.getElementById("imageEditBackdrop");
  if (backdrop) backdrop.classList.add("hidden");
  renderGrid();
}

async function saveEdit() {
  if (editIndex < 0 || editIndex >= images.length) return;
  const outfitInput = /** @type {HTMLInputElement | null} */ (document.getElementById("imageEditOutfit"));
  const sceneInput = /** @type {HTMLTextAreaElement | null} */ (document.getElementById("imageEditScene"));
  if (!outfitInput || !sceneInput) return;

  const entry = images[editIndex];
  entry.outfit = outfitInput.value;
  entry.scene = sceneInput.value;

  try {
    if (saveTimer) {
      clearTimeout(saveTimer);
      saveTimer = null;
    }
    await pushStateToServer();
    setToolbarStatus("");
  } catch (e) {
    setToolbarStatus(e.message || "save");
  }
  closeEdit();
}

async function deleteFromEdit() {
  if (editIndex < 0 || editIndex >= images.length) return;
  const entry = images[editIndex];
  await deleteImage(entry.fid);
}

// ==================== 导出 ====================

async function exportToDatasets() {
  if (!stateLoaded) {
    setToolbarStatus("未加载");
    return;
  }
  try {
    if (saveTimer) {
      clearTimeout(saveTimer);
      saveTimer = null;
    }
    await pushStateToServer();
    await postNdjsonDatasetJob(`${apiRoot()}/bridge/dataset/save`, { allowEmptyStream: true });
    setToolbarStatus("");
  } catch (e) {
    setToolbarStatus(e.message || "↑");
  }
}

async function postNdjsonDatasetJob(url, opts = {}) {
  const allowEmptyStream = !!opts.allowEmptyStream;
  const wrap = document.getElementById("jobProgress");
  const fill = document.getElementById("jobProgressFill");
  const track = fill && fill.parentElement;
  const btnExp = document.getElementById("btnExport");
  if (!wrap || !fill || !track) throw new Error("ui");

  setToolbarStatus("");
  wrap.classList.remove("hidden");
  wrap.setAttribute("aria-hidden", "false");
  fill.style.width = "0%";
  track.setAttribute("aria-valuenow", "0");
  if (btnExp) btnExp.disabled = true;

  /** @type {Record<string, unknown>|null} */
  let last = null;
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (!res.ok) {
      const text = await res.text();
      let msg = res.statusText || String(res.status);
      try {
        const j = JSON.parse(text);
        if (j && j.message) msg = String(j.message);
        else if (j && j.error != null) msg = typeof j.error === "string" ? j.error : msg;
      } catch {
        if (text) msg = text.slice(0, 240);
      }
      throw new Error(msg);
    }
    const reader = res.body && res.body.getReader();
    if (!reader) throw new Error("body");
    const dec = new TextDecoder();
    let buf = "";
    let acc = "";
    for (;;) {
      const chunk = await reader.read();
      if (chunk.done) break;
      const piece = dec.decode(chunk.value, { stream: true });
      buf += piece;
      acc += piece;
      const lines = buf.split("\n");
      buf = lines.pop() || "";
      for (const line of lines) {
        const ln = line.trim();
        if (!ln) continue;
        let obj;
        try {
          obj = JSON.parse(ln);
        } catch {
          continue;
        }
        if (obj.type === "progress" && typeof obj.total === "number" && obj.total > 0) {
          const cur = typeof obj.current === "number" ? obj.current : 0;
          const pct = Math.min(100, (cur / obj.total) * 100);
          fill.style.width = `${pct}%`;
          track.setAttribute("aria-valuenow", String(Math.round(pct)));
        }
        if (obj.type === "done") last = obj;
      }
    }
    const tail = buf.trim();
    if (tail) {
      try {
        const obj = JSON.parse(tail);
        if (obj.type === "done") last = obj;
      } catch {
        /* ignore */
      }
    }
    if (!last && acc.trim()) {
      const allLines = acc.split("\n");
      for (let i = allLines.length - 1; i >= 0; i--) {
        const ln = allLines[i].trim();
        if (!ln) continue;
        try {
          const obj = JSON.parse(ln);
          if (obj.type === "done") {
            last = obj;
            break;
          }
        } catch {
          /* continue */
        }
      }
    }
    if (!last) {
      if (allowEmptyStream) last = { ok: true, saved: [] };
      else throw new Error("no result");
    }
    if (last.ok !== true) {
      const err = last.error != null ? String(last.error) : "fail";
      const extra = last.message != null ? ` ${last.message}` : "";
      throw new Error(`${err}${extra}`.trim());
    }
    fill.style.width = "100%";
    track.setAttribute("aria-valuenow", "100");
    return last;
  } finally {
    if (btnExp) btnExp.disabled = false;
    wrap.classList.add("hidden");
    wrap.setAttribute("aria-hidden", "true");
    fill.style.width = "0%";
    track.setAttribute("aria-valuenow", "0");
  }
}

// ==================== 事件绑定 ====================

function wireImportButton() {
  const fi = document.getElementById("fileInput");
  const btn = document.getElementById("btnPickLocal");
  if (!fi || !btn) return;
  btn.addEventListener("click", () => fi.click());
  fi.addEventListener("change", () => {
    const fs = fi.files;
    if (fs && fs.length) void uploadImagesFromFiles(fs);
    fi.value = "";
  });
}

function wireEditPanel() {
  const closeBtn = document.getElementById("imageEditClose");
  const saveBtn = document.getElementById("imageEditSave");
  const deleteBtn = document.getElementById("imageEditDelete");
  const backdrop = document.getElementById("imageEditBackdrop");
  const panel = document.getElementById("imageEditPanel");
  if (!closeBtn || !saveBtn || !deleteBtn || !backdrop || !panel) return;

  closeBtn.addEventListener("click", () => closeEdit());
  saveBtn.addEventListener("click", () => void saveEdit());
  deleteBtn.addEventListener("click", () => void deleteFromEdit());

  backdrop.addEventListener("click", (e) => {
    if (e.target === backdrop) closeEdit();
  });
  panel.addEventListener("click", (e) => e.stopPropagation());
}

document.addEventListener("keydown", (e) => {
  const editBd = document.getElementById("imageEditBackdrop");
  if (editBd && !editBd.classList.contains("hidden")) {
    if (e.key === "Escape") {
      closeEdit();
      e.preventDefault();
      return;
    }
  }
});

function wireToolbar() {
  const bind = (id, handler) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener("click", handler);
  };
  bind("btnExport", () => void exportToDatasets());
  bind("btnClear", () => void deleteAllImages());
}

// ==================== 启动 ====================

document.addEventListener("DOMContentLoaded", () => {
  wireToolbar();
  wireImportButton();
  wireEditPanel();

  void (async () => {
    try {
      await loadStateFromServer();
    } catch (e) {
      setToolbarStatus(e.message || "load");
      images = [];
    }
    mountGrid();
    if (stateLoaded) startPolling();
  })();
});
