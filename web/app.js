/**
 * 数据集主页：图册（Album）模式。
 * 每张卡片 = 一个图册，点击进详情查看/管理子图片。
 */

const POLL_MS = 2000;

/** @type {ReturnType<typeof setInterval> | null} */
let pollTimer = null;
let serverHash = "";

/**
 * @typedef {{ aid: string, tags: string, images: Array<{fid: string}> }} Album
 * @type {Album[]}
 */
let albums = [];

/** 左键选中的标签（多选/交集），空集 = 不限 */
let selectedTags = new Set();
/** 右键排除的标签（多选/并集），空集 = 不限 */
let excludedTags = new Set();

/** 详情弹窗状态 */
let detailOpen = false;
/** 当前打开详情弹窗的 album index（在 albums 中的下标） */
let detailAlbumIndex = -1;
/** 当前画廊显示第几张（0-based） */
let galleryIndex = 0;

const imageRev = new Map();
let saveTimer = null;
let stateLoaded = false;

/**
 * 回收站：刷新即清空，存储被删除的 album 和 image 快照。
 * @type {Array<{type: 'album'|'image', album: (Object|null), fid: (string|null), tags: string}>}
 */
let trashBin = [];

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
  const raw = data.albums || [];
  albums = raw
    .filter((a) => a && typeof a === "object" && a.aid && typeof a.aid === "string")
    .map((a) => ({
      aid: a.aid,
      tags: typeof a.tags === "string" ? a.tags : "",
      images: Array.isArray(a.images) ? a.images.filter((i) => i && typeof i.fid === "string") : [],
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
    body: JSON.stringify({ albums }),
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

// ==================== 标签筛选 ====================

/**
 * 从所有 album 的 tags 字段中解析逗号分隔的标签，统计出现次数。
 * @returns {Map<string, number>} tag → 出现次数
 */
function parseTagCounts() {
  const counts = new Map();
  for (const album of albums) {
    if (!album.tags) continue;
    const parts = album.tags.split(",").map((s) => s.trim()).filter(Boolean);
    const seen = new Set();
    for (const tag of parts) {
      // 同一个 album 内同名 tag 只计一次
      if (!seen.has(tag)) {
        seen.add(tag);
        counts.set(tag, (counts.get(tag) || 0) + 1);
      }
    }
  }
  return counts;
}

/**
 * 渲染标签筛选栏
 */
function renderTagFilter() {
  const bar = document.getElementById("tagFilterBar");
  if (!bar) return;

  const counts = parseTagCounts();
  const sorted = Array.from(counts.entries()).sort((a, b) => b[1] - a[1]);

  bar.innerHTML = "";

  // "全部"按钮
  const allBtn = document.createElement("button");
  allBtn.type = "button";
  allBtn.className = "tag-filter-btn";
  if (selectedTags.size === 0 && excludedTags.size === 0) allBtn.classList.add("is-active");
  allBtn.textContent = "全部";
  allBtn.addEventListener("click", () => {
    selectedTags.clear();
    excludedTags.clear();
    renderTagFilter();
    renderGrid();
  });
  bar.appendChild(allBtn);

  // 每个 tag
  for (const [tag, count] of sorted) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "tag-filter-btn";

    const isSelected = selectedTags.has(tag);
    const isExcluded = excludedTags.has(tag);
    if (isSelected) btn.classList.add("is-active");
    if (isExcluded) btn.classList.add("is-excluded");

    const label = document.createElement("span");
    label.textContent = tag;
    btn.appendChild(label);

    const countSpan = document.createElement("span");
    countSpan.className = "tag-filter-count";
    countSpan.textContent = String(count);
    btn.appendChild(countSpan);

    // 左键 → 选中（同一标签上清除排除状态）
    btn.addEventListener("click", (ev) => {
      ev.preventDefault();
      excludedTags.delete(tag); // 同一标签不能同时排除
      if (selectedTags.has(tag)) {
        selectedTags.delete(tag);
      } else {
        selectedTags.add(tag);
      }
      renderTagFilter();
      renderGrid();
    });

    // 右键 → 排除（同一标签上清除选中状态）
    btn.addEventListener("contextmenu", (ev) => {
      ev.preventDefault();
      selectedTags.delete(tag); // 同一标签不能同时选中
      if (excludedTags.has(tag)) {
        excludedTags.delete(tag);
      } else {
        excludedTags.add(tag);
      }
      renderTagFilter();
      renderGrid();
    });

    bar.appendChild(btn);
  }
}

/**
 * @returns {Album[]} 算法：左键选中标签的并集 − 右键排除标签
 */
function getFilteredAlbums() {
  const hasSelected = selectedTags.size > 0;
  const hasExcluded = excludedTags.size > 0;

  // 无筛选 → 全部
  if (!hasSelected && !hasExcluded) return albums;

  // 预计算每个 album 的 tag set
  const albumTagSets = new Map();
  for (const album of albums) {
    albumTagSets.set(album.aid, new Set(
      (album.tags || "").split(",").map((s) => s.trim()).filter(Boolean)
    ));
  }

  // 排除：剔除包含任一排除标签的 album
  if (hasExcluded) {
    const excludedAids = new Set();
    for (const album of albums) {
      const tags = albumTagSets.get(album.aid);
      if (!tags) { excludedAids.add(album.aid); continue; }
      for (const tag of excludedTags) {
        if (tags.has(tag)) { excludedAids.add(album.aid); break; }
      }
    }
    // 如果只有排除（没有选中），返回 albums 中没被排除的
    if (!hasSelected) {
      return albums.filter((a) => !excludedAids.has(a.aid));
    }
    // 同时有选中+排除：取选中并集，再剔除排除
    const selectedAids = new Set();
    for (const album of albums) {
      const tags = albumTagSets.get(album.aid);
      if (!tags) continue;
      for (const tag of selectedTags) {
        if (tags.has(tag)) { selectedAids.add(album.aid); break; }
      }
    }
    return albums.filter((a) => selectedAids.has(a.aid) && !excludedAids.has(a.aid));
  }

  // 只有选中：取并集
  const selectedAids = new Set();
  for (const album of albums) {
    const tags = albumTagSets.get(album.aid);
    if (!tags) continue;
    for (const tag of selectedTags) {
      if (tags.has(tag)) { selectedAids.add(album.aid); break; }
    }
  }
  return albums.filter((a) => selectedAids.has(a.aid));
}

// ==================== 网格渲染 ====================

function mountGrid() {
  const grid = document.getElementById("homeGrid");
  if (!grid) return;
  grid.innerHTML = "";
  renderTagFilter();
  renderGrid();
}

function createAlbumCard(album, idx) {
  const card = document.createElement("div");
  card.className = "image-card album-card";
  card.dataset.aid = album.aid;

  const coverFid = album.images.length > 0 ? album.images[0].fid : null;
  const img = document.createElement("img");
  img.alt = "";
  if (coverFid) {
    bumpImageRev(coverFid);
    img.src = imageUrl(coverFid);
  } else {
    // 无图片的占位（理论上 reconcile 会过滤掉）
  }
  card.appendChild(img);

  // 图片数量角标
  const countBadge = document.createElement("div");
  countBadge.className = "album-card-count";
  countBadge.textContent = `${album.images.length} 图`;
  card.appendChild(countBadge);

  // 删除按钮
  const delBtn = document.createElement("button");
  delBtn.type = "button";
  delBtn.className = "image-card-delete";
  delBtn.textContent = "×";
  delBtn.title = "删除此图册";
  delBtn.addEventListener("click", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    void deleteAlbum(album.aid);
  });
  card.appendChild(delBtn);

  card.addEventListener("click", (ev) => {
    if (ev.target === delBtn) return;
    if (idx >= 0) openAlbumDetail(idx);
  });

  return card;
}

function renderGrid() {
  const grid = document.getElementById("homeGrid");
  if (!grid) return;
  grid.innerHTML = "";

  // 每次渲染网格时同步更新标签栏（tag 频次可能变了）
  renderTagFilter();

  const filtered = getFilteredAlbums();

  if (filtered.length === 0) {
    const p = document.createElement("p");
    p.className = "image-row-empty";
    p.textContent = albums.length === 0
      ? "暂无图册，请导入图片或从 Blender 渲染上传。"
      : "没有匹配选中标签的图册。";
    Object.assign(p.style, {
      padding: "48px 20px",
      textAlign: "center",
      fontSize: "0.875rem",
      color: "rgba(255,255,255,0.32)",
      border: "1px dashed rgba(255,255,255,0.08)",
      borderRadius: "var(--zlh-radius)",
      background: "rgba(255,255,255,0.02)",
    });
    grid.appendChild(p);
    return;
  }

  // 使用 flex-wrap 展示所有 album card
  const wrap = document.createElement("div");
  wrap.className = "album-grid-wrap";
  for (let i = 0; i < filtered.length; i++) {
    // createAlbumCard 需要全局 index 用于打开详情弹窗
    const globalIdx = albums.indexOf(filtered[i]);
    const card = createAlbumCard(filtered[i], globalIdx);
    wrap.appendChild(card);
  }
  grid.appendChild(wrap);
}

// ==================== 增量更新 ====================

function diffAlbums(oldArr, newArr) {
  const oldMap = new Map(oldArr.map((a) => [a.aid, a]));
  const newMap = new Map(newArr.map((a) => [a.aid, a]));
  const added = [];
  const removed = [];
  const changed = [];

  for (const [aid, album] of newMap) {
    if (!oldMap.has(aid)) {
      added.push({ aid, album });
    } else {
      const old = oldMap.get(aid);
      if (old.tags !== album.tags || JSON.stringify(old.images) !== JSON.stringify(album.images)) {
        changed.push({ aid, album });
      }
    }
  }
  for (const [aid, album] of oldMap) {
    if (!newMap.has(aid)) {
      removed.push({ aid, album });
    }
  }
  return { added, removed, changed };
}

function applyIncrementalDiff(diff) {
  const totalOps = diff.added.length + diff.removed.length + diff.changed.length;
  if (totalOps === 0) return;

  // 有标签筛选时全量渲染更简单
  if (totalOps >= albums.length * 0.5 || selectedTags.size > 0 || excludedTags.size > 0) {
    renderGrid();
    return;
  }

  const grid = document.getElementById("homeGrid");
  if (!grid) return;
  const wrap = grid.querySelector(".album-grid-wrap");
  if (!wrap) {
    renderGrid();
    return;
  }

  // 删除
  for (const { aid } of diff.removed) {
    const card = wrap.querySelector(`.album-card[data-aid="${CSS.escape(aid)}"]`);
    if (card) card.remove();
  }

  // 新增
  for (const { album, aid } of diff.added) {
    const idx = albums.findIndex((a) => a.aid === aid);
    if (idx < 0) continue;
    const card = createAlbumCard(album, idx);
    wrap.appendChild(card);
  }

  // 修改
  for (const { album, aid } of diff.changed) {
    const oldCard = wrap.querySelector(`.album-card[data-aid="${CSS.escape(aid)}"]`);
    if (!oldCard) continue;
    const idx = albums.findIndex((a) => a.aid === aid);
    if (idx < 0) continue;
    const newCard = createAlbumCard(album, idx);
    oldCard.replaceWith(newCard);
  }

  if (wrap.children.length === 0) renderGrid();
}

// ==================== Poll ====================

function onVisibilityChange() {
  if (document.visibilityState === "visible") void pollFromServer();
}

async function pollFromServer() {
  if (!stateLoaded || document.visibilityState !== "visible") return;
  try {
    const data = await fetchJson(`${apiRoot()}/bridge/dataset/state`);
    const newHash = data.hash || "";
    if (newHash === serverHash) return;
    serverHash = newHash;
    const newAlbums = (data.albums || []).filter((a) => a && typeof a === "object" && a.aid);
    const oldAlbums = albums;
    const diff = diffAlbums(oldAlbums, newAlbums);
    applyStatePayload(data);

    if (detailOpen) {
      // 如果当前详情页的 album 被外部修改了，刷新画廊
      if (detailAlbumIndex >= 0 && detailAlbumIndex < albums.length) {
        renderGallery();
      }
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

// ==================== 图片操作 ====================

async function deleteImage(fid) {
  if (!fid) return;
  // 记住当前打开的 album aid，用于 reload 后重定位
  const prevAid = detailOpen && detailAlbumIndex >= 0 && detailAlbumIndex < albums.length
    ? albums[detailAlbumIndex].aid
    : null;

  // 存回收站快照（保存当前 album 快照和 fid）
  if (prevAid) {
    const album = albums.find((a) => a.aid === prevAid);
    if (album) {
      trashBin.push({
        type: "image",
        album: JSON.parse(JSON.stringify(album)),
        fid: fid,
        tags: album.tags || "",
      });
      updateTrashButtonHint();
    }
  }

  try {
    await fetchJson(`${apiRoot()}/bridge/dataset/image/${fid}`, { method: "DELETE" });
  } catch (e) {
    setToolbarStatus(e.message || "delete");
    return;
  }
  await loadStateFromServer();
  renderGrid();
  if (prevAid) {
    const newIdx = albums.findIndex((a) => a.aid === prevAid);
    if (newIdx >= 0) {
      detailAlbumIndex = newIdx;
      renderGallery();
    } else {
      closeAlbumDetail();
    }
  }
  setToolbarStatus("");
}

async function deleteAlbum(aid) {
  if (!confirm(`确定删除此图册及其所有图片？`)) return;

  // 存回收站快照
  const album = albums.find((a) => a.aid === aid);
  if (album) {
    trashBin.push({
      type: "album",
      album: JSON.parse(JSON.stringify(album)),
      fid: null,
      tags: album.tags || "",
    });
    updateTrashButtonHint();
  }

  try {
    await fetchJson(`${apiRoot()}/bridge/dataset/album/${aid}`, { method: "DELETE" });
  } catch (e) {
    setToolbarStatus(e.message || "delete album");
    return;
  }
  if (detailOpen && detailAlbumIndex >= 0 && albums[detailAlbumIndex] && albums[detailAlbumIndex].aid === aid) {
    closeAlbumDetail();
  }
  await loadStateFromServer();
  renderGrid();
  setToolbarStatus("");
}

async function deleteAllAlbums() {
  if (!confirm("清空全部图册？")) return;
  if (!confirm("再次确认：将删除所有图册及图片，不可撤销。")) return;

  // 存回收站快照
  for (const album of albums) {
    trashBin.push({
      type: "album",
      album: JSON.parse(JSON.stringify(album)),
      fid: null,
      tags: album.tags || "",
    });
  }
  updateTrashButtonHint();

  try {
    await fetchJson(`${apiRoot()}/bridge/dataset/clear-images`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  } catch (e) {
    setToolbarStatus(e.message || "clear");
    return;
  }
  closeAlbumDetail();
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
      // 上传创建新 album
      const data = await fetchJson(`${apiRoot()}/bridge/dataset/image`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ image_base64: b64, tags: "" }),
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

// ==================== 图册详情弹窗 ====================

function openAlbumDetail(index) {
  closeAlbumDetail();
  detailOpen = true;
  detailAlbumIndex = index;
  galleryIndex = 0;

  const backdrop = document.getElementById("albumDetailBackdrop");
  if (backdrop) backdrop.classList.remove("hidden");

  fillAlbumDetail();
}

function fillAlbumDetail() {
  if (detailAlbumIndex < 0 || detailAlbumIndex >= albums.length) {
    closeAlbumDetail();
    return;
  }

  const album = albums[detailAlbumIndex];
  const titleEl = document.querySelector(".album-detail-title");
  if (titleEl) titleEl.textContent = `图册 · ${album.images.length} 张图`;

  // 标签
  const tagsInput = document.getElementById("albumTagsInput");
  if (tagsInput) tagsInput.value = album.tags;

  renderGallery();
  refreshWorkflowList();
}

function getCurrentAlbum() {
  if (detailAlbumIndex < 0 || detailAlbumIndex >= albums.length) return null;
  return albums[detailAlbumIndex];
}

function renderGallery() {
  const track = document.getElementById("galleryTrack");
  const counter = document.getElementById("galleryCounter");
  const leftArrow = document.getElementById("galleryArrowLeft");
  const rightArrow = document.getElementById("galleryArrowRight");
  if (!track || !counter) return;

  const album = getCurrentAlbum();
  if (!album) return;

  const images = album.images || [];

  // 清除旧内容
  track.innerHTML = "";

  if (images.length === 0) {
    const emptyMsg = document.createElement("div");
    emptyMsg.className = "album-detail-empty-gallery";
    emptyMsg.textContent = "此图册暂无图片";
    track.appendChild(emptyMsg);
    counter.textContent = "0 / 0";
    if (leftArrow) leftArrow.style.display = "none";
    if (rightArrow) rightArrow.style.display = "none";
    return;
  }

  if (leftArrow) leftArrow.style.display = "";
  if (rightArrow) rightArrow.style.display = "";

  // 创建每张图的容器
  for (let i = 0; i < images.length; i++) {
    const fid = images[i].fid;
    bumpImageRev(fid);
    const slide = document.createElement("div");
    slide.className = "album-detail-slide";
    if (i === 0) slide.classList.add("is-active");

    const img = document.createElement("img");
    img.alt = `image-${i}`;
    img.src = imageUrl(fid);
    slide.appendChild(img);

    // 单独删除按钮（悬浮在图片上）
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "album-detail-slide-delete";
    delBtn.textContent = "×";
    delBtn.title = "从此图册移除";
    delBtn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      void deleteImage(fid);
    });
    slide.appendChild(delBtn);

    track.appendChild(slide);
  }

  // 限制 galleryIndex 范围
  galleryIndex = Math.max(0, Math.min(galleryIndex, images.length - 1));
  scrollToGalleryIndex();
  updateCounter();
}

function scrollToGalleryIndex() {
  const track = document.getElementById("galleryTrack");
  if (!track) return;
  const slides = track.querySelectorAll(".album-detail-slide");
  slides.forEach((s, i) => {
    s.classList.toggle("is-active", i === galleryIndex);
  });
  // 水平滚动到对应位置
  const active = slides[galleryIndex];
  if (active) {
    active.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
  }
  updateCounter();
}

function updateCounter() {
  const counter = document.getElementById("galleryCounter");
  const album = getCurrentAlbum();
  if (!counter || !album) return;
  const total = album.images.length;
  counter.textContent = `${total > 0 ? galleryIndex + 1 : 0} / ${total}`;
}

function galleryPrev() {
  const album = getCurrentAlbum();
  if (!album) return;
  if (galleryIndex > 0) {
    galleryIndex--;
    scrollToGalleryIndex();
  }
}

function galleryNext() {
  const album = getCurrentAlbum();
  if (!album) return;
  if (galleryIndex < album.images.length - 1) {
    galleryIndex++;
    scrollToGalleryIndex();
  }
}

function closeAlbumDetail() {
  // 保存标签
  if (detailAlbumIndex >= 0 && detailAlbumIndex < albums.length) {
    const tagsInput = document.getElementById("albumTagsInput");
    if (tagsInput) {
      const album = albums[detailAlbumIndex];
      if (album) {
        album.tags = tagsInput.value;
        scheduleSaveState();
      }
    }
  }

  detailOpen = false;
  detailAlbumIndex = -1;
  galleryIndex = 0;

  const backdrop = document.getElementById("albumDetailBackdrop");
  if (backdrop) backdrop.classList.add("hidden");
  renderGrid();
}

// ==================== 上传子图到当前 album ====================

async function uploadSubImagesToAlbum(files) {
  const list = Array.from(files || []).filter((f) => f.type.startsWith("image/"));
  if (!list.length) return;
  const album = getCurrentAlbum();
  if (!album) return;

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
      const data = await fetchJson(`${apiRoot()}/bridge/dataset/album/${album.aid}/image`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ image_base64: b64 }),
      });
      if (data && data.id) bumpImageRev(String(data.id));
      applyStatePayload(data);
    }
    renderGallery();
    setToolbarStatus("");
  } catch (e) {
    await loadStateFromServer();
    renderGallery();
    setToolbarStatus(e.message || "upload sub");
  }
}

// ==================== Web Bridge 跑图 ====================

async function refreshWorkflowList() {
  const sel = document.getElementById("albumWorkflowSelect");
  const st = document.getElementById("albumBridgeStatus");
  if (!sel) return;
  try {
    const data = await fetchJson(`${apiRoot()}/bridge/workflows`);
    sel.innerHTML = "";
    const names = data.workflows || [];
    if (names.length === 0) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "（无含 Bridge 的工作流）";
      sel.appendChild(opt);
      return;
    }
    for (const n of names) {
      const opt = document.createElement("option");
      opt.value = n;
      opt.textContent = n;
      sel.appendChild(opt);
    }
  } catch (e) {
    if (st) st.textContent = "加载工作流失败: " + e.message;
  }
}

async function runBridgeForAlbum() {
  const album = getCurrentAlbum();
  if (!album) {
    setToolbarStatus("请先打开一个图册");
    return;
  }

  const sel = document.getElementById("albumWorkflowSelect");
  const st = document.getElementById("albumBridgeStatus");
  const btn = document.getElementById("albumBtnRunBridge");
  if (!sel || !st || !btn) return;

  const wf = sel.value;
  if (!wf) {
    st.textContent = "请选择一个工作流";
    return;
  }

  // 构建 input：传入 source 图（转 base64）+ tags
  const sourceFid = album.images.length > 0 ? album.images[0].fid : "";
  let sourceBase64 = "";
  if (sourceFid) {
    try {
      const resp = await fetch(imageUrl(sourceFid));
      const blob = await resp.blob();
      sourceBase64 = await new Promise((resolve) => {
        const r = new FileReader();
        r.onload = () => {
          const s = /** @type {string} */ (r.result);
          const m = /^data:([^;]+);base64,(.+)$/.exec(s);
          resolve(m ? m[2] : "");
        };
        r.readAsDataURL(blob);
      });
    } catch {
      st.textContent = "读取 source 图片失败";
      return;
    }
  }

  const payload = {
    input: album.tags || "",
    images: sourceBase64
      ? [{ data_base64: sourceBase64 }]
      : [],
  };

  btn.disabled = true;
  st.textContent = "排队运行…";

  try {
    const body = {
      workflow_name: wf,
      input: payload,
    };
    const queued = await fetchJson(`${apiRoot()}/bridge/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const sessionKey = queued.session_key;
    st.textContent = `已排队，等待图像…`;

    // 轮询输出
    const out = await pollBridgeOutput(sessionKey);
    const outImages = out.images || [];
    if (outImages.length === 0) {
      st.textContent = "完成，但无输出图片";
      return;
    }

    // 把每张输出图作为子图添加到当前 album
    for (const img of outImages) {
      const b64 = img.data_base64;
      if (!b64) continue;
      const data = await fetchJson(`${apiRoot()}/bridge/dataset/album/${album.aid}/image`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ image_base64: b64 }),
      });
      if (data && data.id) bumpImageRev(String(data.id));
      applyStatePayload(data);
    }

    renderGallery();
    st.textContent = `完成，添加了 ${outImages.length} 张图`;
  } catch (e) {
    st.textContent = "错误: " + e.message;
  } finally {
    btn.disabled = false;
  }
}

async function pollBridgeOutput(sessionKey, { timeoutMs = 900000, intervalMs = 400 } = {}) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const data = await fetchJson(`${apiRoot()}/bridge/output/${encodeURIComponent(sessionKey)}`);
    const imgs = data.images || [];
    if (data.ready && imgs.length > 0) {
      return { images: imgs };
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  throw new Error("等待输出超时");
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

function wireAlbumDetailPanel() {
  const closeBtn = document.getElementById("albumDetailClose");
  const backdrop = document.getElementById("albumDetailBackdrop");
  const panel = document.getElementById("albumDetailPanel");
  const leftArrow = document.getElementById("galleryArrowLeft");
  const rightArrow = document.getElementById("galleryArrowRight");
  const uploadBtn = document.getElementById("albumBtnUpload");
  const fileInput = document.getElementById("albumFileInput");
  const runBtn = document.getElementById("albumBtnRunBridge");
  const refreshWfBtn = document.getElementById("albumBtnRefreshWorkflows");
  const deleteAlbumBtn = document.getElementById("albumBtnDeleteAlbum");

  if (closeBtn) closeBtn.addEventListener("click", () => closeAlbumDetail());
  if (backdrop)
    backdrop.addEventListener("click", (e) => {
      if (e.target === backdrop) closeAlbumDetail();
    });
  if (panel) panel.addEventListener("click", (e) => e.stopPropagation());

  if (leftArrow) leftArrow.addEventListener("click", () => galleryPrev());
  if (rightArrow) rightArrow.addEventListener("click", () => galleryNext());

  if (uploadBtn && fileInput) {
    uploadBtn.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => {
      const fs = fileInput.files;
      if (fs && fs.length) void uploadSubImagesToAlbum(fs);
      fileInput.value = "";
    });
  }

  if (runBtn) runBtn.addEventListener("click", () => void runBridgeForAlbum());
  if (refreshWfBtn)
    refreshWfBtn.addEventListener("click", () => void refreshWorkflowList());
  if (deleteAlbumBtn && deleteAlbumBtn !== panel) {
    deleteAlbumBtn.addEventListener("click", () => {
      const album = getCurrentAlbum();
      if (album) void deleteAlbum(album.aid);
    });
  }
}

function wireToolbar() {
  const bind = (id, handler) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener("click", handler);
  };
  bind("btnExport", () => void exportToDatasets());
  bind("btnTrash", () => void openTrashModal());
  bind("btnClear", () => void deleteAllAlbums());
}

// ==================== 回收站 ====================

function updateTrashButtonHint() {
  const btn = document.getElementById("btnTrash");
  if (!btn) return;
  if (trashBin.length > 0) {
    btn.textContent = `回收站 (${trashBin.length})`;
  } else {
    btn.textContent = "回收站";
  }
}

function openTrashModal() {
  const backdrop = document.getElementById("trashBackdrop");
  if (backdrop) backdrop.classList.remove("hidden");
  renderTrashList();
}

function closeTrashModal() {
  const backdrop = document.getElementById("trashBackdrop");
  if (backdrop) backdrop.classList.add("hidden");
}

function renderTrashList() {
  const list = document.getElementById("trashList");
  const empty = document.getElementById("trashEmpty");
  if (!list || !empty) return;

  list.innerHTML = "";

  if (trashBin.length === 0) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");

  for (let i = 0; i < trashBin.length; i++) {
    const item = trashBin[i];
    const card = document.createElement("div");
    card.className = "trash-item";

    // 信息区域
    const info = document.createElement("div");
    info.className = "trash-item-info";

    const label = document.createElement("span");
    label.className = "trash-item-label";
    if (item.type === "album") {
      label.textContent = item.tags ? `📁 图册: ${item.tags.slice(0, 50)}` : `📁 图册 (${item.album.images.length}张图)`;
    } else {
      label.textContent = `🖼️ 单张图片 (来自: ${item.tags ? item.tags.slice(0, 40) : "无标签"})`;
    }
    info.appendChild(label);

    const preview = document.createElement("span");
    preview.className = "trash-item-preview";
    const imgCount = item.type === "album" ? item.album.images.length : 1;
    preview.textContent = `${imgCount} 张图`;
    info.appendChild(preview);

    card.appendChild(info);

    // 还原按钮
    const restoreBtn = document.createElement("button");
    restoreBtn.type = "button";
    restoreBtn.className = "btn-inline trash-restore-btn";
    restoreBtn.textContent = "还原";
    restoreBtn.addEventListener("click", () => void restoreTrashItem(i));

    // 删除按钮（彻底丢弃）
    const discardBtn = document.createElement("button");
    discardBtn.type = "button";
    discardBtn.className = "btn-ghost btn-ghost--risk btn-ghost--text trash-discard-btn";
    discardBtn.textContent = "丢弃";
    discardBtn.addEventListener("click", () => {
      trashBin.splice(i, 1);
      renderTrashList();
      updateTrashButtonHint();
    });

    const actions = document.createElement("div");
    actions.className = "trash-item-actions";
    actions.appendChild(restoreBtn);
    actions.appendChild(discardBtn);
    card.appendChild(actions);

    list.appendChild(card);
  }
}

async function restoreTrashItem(index) {
  if (index < 0 || index >= trashBin.length) return;
  const item = trashBin[index];

  if (item.type === "album") {
    // 还原整个图册：把 album 的数据逐一上传图片
    const album = item.album;
    if (!album || !album.images || album.images.length === 0) {
      setToolbarStatus("恢复失败：没有图片数据");
      return;
    }
    try {
      for (const img of album.images) {
        const fid = img.fid;
        if (!fid) continue;
        // 从服务端重新请求原始图片
        const resp = await fetch(imageUrl(fid));
        if (!resp.ok) {
          setToolbarStatus(`图片 ${fid} 已从服务端删除，无法恢复`);
          continue;
        }
        const blob = await resp.blob();
        const b64 = await new Promise((resolve) => {
          const r = new FileReader();
          r.onload = () => {
            const s = /** @type {string} */ (r.result);
            const m = /^data:[^;]+;base64,(.+)$/.exec(s);
            resolve(m ? m[2] : "");
          };
          r.readAsDataURL(blob);
        });
        if (!b64) continue;
        const data = await fetchJson(`${apiRoot()}/bridge/dataset/image`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ image_base64: b64, tags: album.tags || "" }),
        });
        if (data && data.id) bumpImageRev(String(data.id));
        applyStatePayload(data);
      }
      // 从回收站移除
      trashBin.splice(index, 1);
      renderTrashList();
      updateTrashButtonHint();
      renderGrid();
      if (!detailOpen) {
        setToolbarStatus(`已还原图册「${album.tags ? album.tags.slice(0, 30) : "无标签"}」`);
      }
    } catch (e) {
      setToolbarStatus("还原图册失败: " + e.message);
    }
  } else if (item.type === "image") {
    // 还原单张图片：找到原 album，重新上传图片
    const album = item.album;
    const fid = item.fid;
    if (!album || !fid) {
      setToolbarStatus("恢复失败：数据不完整");
      return;
    }
    try {
      const resp = await fetch(imageUrl(fid));
      if (!resp.ok) {
        setToolbarStatus("图片已从服务端删除，无法恢复");
        return;
      }
      const blob = await resp.blob();
      const b64 = await new Promise((resolve) => {
        const r = new FileReader();
        r.onload = () => {
          const s = /** @type {string} */ (r.result);
          const m = /^data:[^;]+;base64,(.+)$/.exec(s);
          resolve(m ? m[2] : "");
        };
        r.readAsDataURL(blob);
      });
      if (!b64) return;

      // 检查原 album 是否还存在
      const existingAlbum = albums.find((a) => a.aid === album.aid);
      if (existingAlbum) {
        // album 还存在，直接加到 album 中
        const data = await fetchJson(`${apiRoot()}/bridge/dataset/album/${album.aid}/image`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ image_base64: b64 }),
        });
        if (data && data.id) bumpImageRev(String(data.id));
        applyStatePayload(data);
      } else {
        // album 已不存在，创建新 album
        const data = await fetchJson(`${apiRoot()}/bridge/dataset/image`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ image_base64: b64, tags: album.tags || "" }),
        });
        if (data && data.id) bumpImageRev(String(data.id));
        applyStatePayload(data);
      }

      trashBin.splice(index, 1);
      renderTrashList();
      updateTrashButtonHint();
      renderGrid();
      if (detailOpen) renderGallery();
      setToolbarStatus("已还原图片");
    } catch (e) {
      setToolbarStatus("还原图片失败: " + e.message);
    }
  }
}

function wireTrash() {
  const bind = (id, handler) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("click", handler);
  };

  bind("trashClose", () => void closeTrashModal());

  const backdrop = document.getElementById("trashBackdrop");
  if (backdrop) {
    backdrop.addEventListener("click", (e) => {
      if (e.target === backdrop) closeTrashModal();
    });
  }
  const panel = document.getElementById("trashPanel");
  if (panel) panel.addEventListener("click", (e) => e.stopPropagation());
}

// ==================== 键盘导航 ====================

document.addEventListener("keydown", (e) => {
  const backdrop = document.getElementById("albumDetailBackdrop");
  if (backdrop && !backdrop.classList.contains("hidden")) {
    if (e.key === "Escape") {
      closeAlbumDetail();
      e.preventDefault();
      return;
    }
    if (e.key === "ArrowLeft") {
      galleryPrev();
      e.preventDefault();
      return;
    }
    if (e.key === "ArrowRight") {
      galleryNext();
      e.preventDefault();
      return;
    }
  }
});

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

// ==================== 批量跑图 ====================

/** @type {boolean} */
let batchRunActive = false;

function openBatchRunModal() {
  const backdrop = document.getElementById("batchRunBackdrop");
  if (backdrop) backdrop.classList.remove("hidden");
  batchRunActive = true;
  updateBatchRunAlbumCount();
  refreshBatchRunWorkflowList();
}

function closeBatchRunModal() {
  batchRunActive = false;
  const backdrop = document.getElementById("batchRunBackdrop");
  if (backdrop) backdrop.classList.add("hidden");
}

function updateBatchRunAlbumCount() {
  const countEl = document.getElementById("batchRunAlbumCount");
  if (!countEl) return;
  const filtered = getFilteredAlbums();
  countEl.textContent = `当前筛选：${filtered.length} 个图集`;
}

async function refreshBatchRunWorkflowList() {
  const sel = document.getElementById("batchRunWorkflowSelect");
  if (!sel) return;
  try {
    const data = await fetchJson(`${apiRoot()}/bridge/workflows`);
    sel.innerHTML = "";
    const names = data.workflows || [];
    if (names.length === 0) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "（无含 Bridge 的工作流）";
      sel.appendChild(opt);
      return;
    }
    for (const n of names) {
      const opt = document.createElement("option");
      opt.value = n;
      opt.textContent = n;
      sel.appendChild(opt);
    }
  } catch (e) {
    const st = document.getElementById("batchRunStatus");
    if (st) st.textContent = "加载工作流失败: " + e.message;
  }
}

async function startBatchRun() {
  const btn = document.getElementById("batchBtnRun");
  const statusEl = document.getElementById("batchRunStatus");
  const fill = document.getElementById("batchRunProgressFill");
  const selWf = document.getElementById("batchRunWorkflowSelect");
  if (!btn || !statusEl || !fill || !selWf) return;

  const wf = selWf.value;
  if (!wf) {
    statusEl.textContent = "请选择一个工作流";
    return;
  }

  // 直接用当前筛选结果
  const targetAlbums = getFilteredAlbums();
  if (targetAlbums.length === 0) {
    statusEl.textContent = "当前筛选结果为空";
    return;
  }

  btn.disabled = true;
  statusEl.textContent = "准备中…";
  fill.style.width = "0%";

  let completed = 0;
  const total = targetAlbums.length;

  try {
    // 先保存当前状态
    if (saveTimer) {
      clearTimeout(saveTimer);
      saveTimer = null;
    }
    await pushStateToServer();

    for (const album of targetAlbums) {
      statusEl.textContent = `正在处理 [${completed + 1}/${total}] ${album.tags ? album.tags.slice(0, 30) : "(无标签)"}`;

      // 构建 input：传入 source 图（转 base64）+ tags
      const sourceFid = album.images.length > 0 ? album.images[0].fid : "";
      let sourceBase64 = "";
      if (sourceFid) {
        try {
          const resp = await fetch(imageUrl(sourceFid));
          const blob = await resp.blob();
          sourceBase64 = await new Promise((resolve) => {
            const r = new FileReader();
            r.onload = () => {
              const s = /** @type {string} */ (r.result);
              const m = /^data:([^;]+);base64,(.+)$/.exec(s);
              resolve(m ? m[2] : "");
            };
            r.readAsDataURL(blob);
          });
        } catch {
          statusEl.textContent = `[${completed + 1}/${total}] 读取图片失败，跳过`;
          completed++;
          const pct = Math.min(100, (completed / total) * 100);
          fill.style.width = `${pct}%`;
          continue;
        }
      }

      const payload = {
        input: album.tags || "",
        images: sourceBase64 ? [{ data_base64: sourceBase64 }] : [],
      };

      try {
        const body = {
          workflow_name: wf,
          input: payload,
        };
        const queued = await fetchJson(`${apiRoot()}/bridge/run`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        const sessionKey = queued.session_key;

        // 轮询输出
        const out = await pollBridgeOutput(sessionKey);
        const outImages = out.images || [];

        // 把每张输出图作为子图添加到当前 album
        for (const img of outImages) {
          const b64 = img.data_base64;
          if (!b64) continue;
          const data = await fetchJson(`${apiRoot()}/bridge/dataset/album/${album.aid}/image`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ image_base64: b64 }),
          });
          if (data && data.id) bumpImageRev(String(data.id));
          applyStatePayload(data);
        }

        statusEl.textContent = `[${completed + 1}/${total}] 完成，添加 ${outImages.length} 张图`;
      } catch (e) {
        statusEl.textContent = `[${completed + 1}/${total}] 错误: ${e.message}`;
      }

      completed++;
      const pct = Math.min(100, (completed / total) * 100);
      fill.style.width = `${pct}%`;

      // 每个图集之间稍微延迟，避免队列涌塞
      if (completed < total) {
        await new Promise((r) => setTimeout(r, 500));
      }
    }

    // 完成后刷新
    if (detailOpen) {
      renderGallery();
    }
    renderGrid();
    fill.style.width = "100%";
    statusEl.textContent = `批量跑图完成，处理了 ${total} 个图集`;
  } catch (e) {
    statusEl.textContent = "批量跑图异常: " + e.message;
  } finally {
    btn.disabled = false;
  }
}

function wireBatchRun() {
  const bind = (id, handler) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("click", handler);
  };

  bind("btnBatchRun", () => void openBatchRunModal());
  bind("batchRunClose", () => void closeBatchRunModal());

  const backdrop = document.getElementById("batchRunBackdrop");
  if (backdrop) {
    backdrop.addEventListener("click", (e) => {
      if (e.target === backdrop) closeBatchRunModal();
    });
  }
  const panel = document.getElementById("batchRunPanel");
  if (panel) panel.addEventListener("click", (e) => e.stopPropagation());

  bind("batchBtnRun", () => void startBatchRun());
  bind("batchRunRefreshWorkflows", () => void refreshBatchRunWorkflowList());
}

// ==================== 启动 ====================

document.addEventListener("DOMContentLoaded", () => {
  wireToolbar();
  wireImportButton();
  wireAlbumDetailPanel();
  wireBatchRun();
  wireTrash();

  void (async () => {
    try {
      await loadStateFromServer();
    } catch (e) {
      setToolbarStatus(e.message || "load");
      albums = [];
    }
    mountGrid();
    if (stateLoaded) startPolling();
  })();
});
