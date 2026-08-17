const DEMO_DATA = {
  mode: "image",
  source: "demo",
  note: "当前为 Demo 分析。配置 OPENAI_API_KEY 后将调用真实 Vision API。",
  analysis: {
    subject: "一位穿深红色羊毛大衣的年轻女性，站在窗边望向城市，神情安静自然",
    scene: "现代公寓室内，蓝调时刻的城市天际线被雨水切割成柔和的光斑",
    composition: "4:5 竖幅，中景，人物位于左侧三分之一，窗框形成自然层次和引导线",
    camera: "50mm 定焦镜头，浅景深，主体清晰，背景城市灯光自然散景",
    lighting: "冷色窗光从侧面进入，室内暖色实用灯勾勒轮廓，低饱和电影感",
    color: "青蓝城市光、深红大衣、炭灰室内，冷暖对比克制",
    style: "高端电影感生活方式摄影，真实皮肤质感，轻微胶片颗粒",
    details: ["雨滴附着在玻璃表面", "羊毛大衣织物纹理清晰", "背景灯光有柔和光晕", "画面留有呼吸感"],
    negative_prompt: ["文字", "logo", "水印", "过度磨皮", "畸形手部", "过饱和", "杂乱背景"],
    confidence: 86,
  },
  prompts: {
    universal: "一位穿深红色羊毛大衣的年轻女性，站在窗边望向城市，神情安静自然。现代公寓室内，蓝调时刻的城市天际线被雨水切割成柔和的光斑。4:5 竖幅，中景，人物位于左侧三分之一，窗框形成自然层次和引导线。50mm 定焦镜头，浅景深，主体清晰，背景城市灯光自然散景。冷色窗光从侧面进入，室内暖色实用灯勾勒轮廓，低饱和电影感。青蓝城市光、深红大衣、炭灰室内，冷暖对比克制。高端电影感生活方式摄影，真实皮肤质感，轻微胶片颗粒。细节：雨滴附着在玻璃表面，羊毛大衣织物纹理清晰，背景灯光有柔和光晕，画面留有呼吸感。高真实度，画面干净，主体和环境关系自然。",
    midjourney: "一位穿深红色羊毛大衣的年轻女性，站在窗边望向城市，神情安静自然, 现代公寓室内，蓝调时刻的城市天际线被雨水切割成柔和的光斑, 4:5 竖幅，中景，人物位于左侧三分之一，窗框形成自然层次和引导线, 50mm 定焦镜头，浅景深，主体清晰，背景城市灯光自然散景, 冷色窗光从侧面进入，室内暖色实用灯勾勒轮廓，低饱和电影感, 高端电影感生活方式摄影, 雨滴附着在玻璃表面, 羊毛大衣织物纹理清晰, 背景灯光有柔和光晕, 画面留有呼吸感 --ar 4:5 --stylize 180 --no 文字, logo, 水印, 过度磨皮, 畸形手部, 过饱和, 杂乱背景",
    flux: "A cinematic editorial photograph of 一位穿深红色羊毛大衣的年轻女性，站在窗边望向城市，神情安静自然, set in 现代公寓室内，蓝调时刻的城市天际线被雨水切割成柔和的光斑. 4:5 竖幅，中景，人物位于左侧三分之一，窗框形成自然层次和引导线. 50mm 定焦镜头，浅景深，主体清晰，背景城市灯光自然散景. 冷色窗光从侧面进入，室内暖色实用灯勾勒轮廓，低饱和电影感. Color palette: 青蓝城市光、深红大衣、炭灰室内，冷暖对比克制. Style: 高端电影感生活方式摄影，真实皮肤质感，轻微胶片颗粒. Important details: 雨滴附着在玻璃表面, 羊毛大衣织物纹理清晰, 背景灯光有柔和光晕, 画面留有呼吸感. Avoid 文字, logo, 水印, 过度磨皮, 畸形手部, 过饱和, 杂乱背景.",
    video: "一位穿深红色羊毛大衣的年轻女性，站在窗边望向城市，神情安静自然，现代公寓室内，蓝调时刻的城市天际线被雨水切割成柔和的光斑。Start with 4:5 竖幅，中景，人物位于左侧三分之一，窗框形成自然层次和引导线；镜头极慢速向前推进，主体保持自然静止。保持冷色窗光从侧面进入、室内暖色实用灯勾勒轮廓、低饱和电影感，以及青蓝城市光、深红大衣、炭灰室内的色彩关系。玻璃上的雨水产生细微运动，真实运动模糊，24fps，电影感节奏。避免文字、logo、水印、过度磨皮、畸形手部、过饱和、杂乱背景。",
  },
};

const state = { mode: "image", file: null, data: DEMO_DATA, promptKey: "universal", previewUrl: "" };
const $ = (id) => document.getElementById(id);
const fieldMap = { subject: "subjectField", scene: "sceneField", composition: "compositionField", camera: "cameraField", lighting: "lightingField", color: "colorField", style: "styleField" };

function showToast(message) {
  const toast = $("toast");
  toast.textContent = message;
  toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("show"), 2600);
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

function renderTags(id, values) {
  $(id).innerHTML = values.map((value, index) => `<span class="tag">${escapeHtml(value)}<button type="button" data-remove="${id}" data-index="${index}" aria-label="移除 ${escapeHtml(value)}">×</button></span>`).join("");
}

function collectAnalysis() {
  const analysis = { ...state.data.analysis };
  Object.entries(fieldMap).forEach(([key, id]) => { analysis[key] = $(id).value.trim(); });
  analysis.details = [...document.querySelectorAll("#detailList .tag")].map((tag) => tag.firstChild.textContent.trim());
  analysis.negative_prompt = [...document.querySelectorAll("#negativeList .tag")].map((tag) => tag.firstChild.textContent.trim());
  return analysis;
}

function render(data) {
  state.data = data;
  Object.entries(fieldMap).forEach(([key, id]) => { $(id).value = data.analysis[key] || ""; });
  $("confidenceValue").textContent = data.analysis.confidence ?? 0;
  renderTags("detailList", data.analysis.details || []);
  renderTags("negativeList", data.analysis.negative_prompt || []);
  $("analysisNote").textContent = data.source === "live" ? "已完成实时视觉分析" : "Demo 分析已载入";
  $("serviceStatus").textContent = data.source === "live" ? "Live API" : "Demo 模式";
  refreshPrompt();
}

function buildPromptBundle(analysis) {
  const details = analysis.details.join(", ");
  const negative = analysis.negative_prompt.join(", ");
  return {
    universal: `${analysis.subject}。${analysis.scene}。${analysis.composition}。${analysis.camera}。${analysis.lighting}。${analysis.color}。${analysis.style}。细节：${details}。高真实度，画面干净，主体和环境关系自然。`,
    midjourney: `${analysis.subject}, ${analysis.scene}, ${analysis.composition}, ${analysis.camera}, ${analysis.lighting}, ${analysis.style}, ${details} --ar 4:5 --stylize 180 --no ${negative}`,
    flux: `A cinematic editorial photograph of ${analysis.subject}, set in ${analysis.scene}. ${analysis.composition}. ${analysis.camera}. ${analysis.lighting}. Color palette: ${analysis.color}. Style: ${analysis.style}. Important details: ${details}. Avoid ${negative}.`,
    video: `${analysis.subject}，${analysis.scene}。Start with ${analysis.composition}；镜头极慢速向前推进，主体保持自然静止。保持${analysis.lighting}和${analysis.color}，锁定人物、服装和背景连续性。玻璃上的雨水产生细微运动，真实运动模糊，24fps，电影感节奏。避免${negative}。`,
  };
}

function refreshPrompt() {
  const prompts = buildPromptBundle(collectAnalysis());
  state.data.prompts = prompts;
  $("promptBox").value = prompts[state.promptKey] || prompts.universal;
  updatePromptCount();
}

function updatePromptCount() {
  const value = $("promptBox").value.trim();
  const latinWords = value.match(/[A-Za-z0-9_]+/g)?.length || 0;
  const cjkChars = value.match(/[\u3400-\u9fff]/g)?.length || 0;
  $("promptWordCount").textContent = `${latinWords + cjkChars} tokens`;
}

function setFile(file) {
  if (!file) return;
  const isImage = file.type.startsWith("image/");
  const isVideo = file.type.startsWith("video/");
  if ((state.mode === "image" && !isImage) || (state.mode === "video" && !isVideo)) {
    showToast(state.mode === "image" ? "当前模式只接受图片文件" : "当前模式只接受视频文件");
    return;
  }
  if (file.size > 12 * 1024 * 1024) { showToast("文件不能超过 12MB"); return; }
  state.file = file;
  if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
  state.previewUrl = URL.createObjectURL(file);
  $("previewFrame").classList.add("has-media");
  $("previewEmpty").hidden = true;
  $("previewCaption").hidden = true;
  $("previewImage").hidden = !isImage;
  $("previewVideo").hidden = !isVideo;
  if (isImage) $("previewImage").src = state.previewUrl;
  if (isVideo) $("previewVideo").src = state.previewUrl;
  $("fileMeta").hidden = false;
  $("fileMeta").textContent = `${file.name} · ${(file.size / 1024 / 1024).toFixed(2)} MB`;
  $("dropzoneTitle").textContent = "素材已就绪，点击开始分析";
}

function clearFile() {
  state.file = null;
  if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
  state.previewUrl = "";
  $("previewFrame").classList.remove("has-media");
  $("previewEmpty").hidden = false;
  $("previewCaption").hidden = false;
  $("previewImage").hidden = true;
  $("previewVideo").hidden = true;
  $("previewImage").removeAttribute("src");
  $("previewVideo").removeAttribute("src");
  $("fileMeta").hidden = true;
  $("dropzoneTitle").textContent = state.mode === "image" ? "拖入图片，或点击选择" : "拖入视频，或点击选择";
  $("fileInput").value = "";
}

async function analyze() {
  if (!state.file) { render(DEMO_DATA); showToast("已载入 Demo 分析，可直接编辑字段"); return; }
  const button = $("analyzeButton");
  button.disabled = true;
  button.innerHTML = "<span>◌</span>正在分析…";
  const form = new FormData();
  form.append("file", state.file);
  form.append("mode", state.mode);
  form.append("model", $("modelSelect").value);
  try {
    const response = await fetch("/api/analyze", { method: "POST", body: form });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "分析失败");
    render(payload);
    showToast(payload.source === "live" ? "分析完成" : "Demo 分析完成");
  } catch (error) {
    showToast(error.message || "分析失败，请稍后重试");
  } finally {
    button.disabled = false;
    button.innerHTML = "<span>✦</span>开始反推提示词";
  }
}

function addTag(listId, label) {
  const value = window.prompt(label);
  if (!value || !value.trim()) return;
  const values = [...document.querySelectorAll(`#${listId} .tag`)].map((tag) => tag.firstChild.textContent.trim());
  values.push(value.trim());
  renderTags(listId, values);
  refreshPrompt();
}

function saveVersion() {
  const versions = JSON.parse(localStorage.getItem("prompt-lens-versions") || "[]");
  versions.unshift({ createdAt: new Date().toISOString(), analysis: collectAnalysis(), prompts: state.data.prompts });
  localStorage.setItem("prompt-lens-versions", JSON.stringify(versions.slice(0, 20)));
  $("historyCount").textContent = versions.length;
  showToast("已保存当前版本");
}

function exportJson() {
  const blob = new Blob([JSON.stringify({ analysis: collectAnalysis(), prompts: state.data.prompts }, null, 2)], { type: "application/json" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `prompt-lens-${new Date().toISOString().slice(0, 10)}.json`;
  link.click();
  URL.revokeObjectURL(link.href);
}

document.addEventListener("DOMContentLoaded", () => {
  render(DEMO_DATA);
  const versions = JSON.parse(localStorage.getItem("prompt-lens-versions") || "[]");
  $("historyCount").textContent = versions.length;
  $("fileInput").addEventListener("change", (event) => setFile(event.target.files[0]));
  $("dropzone").addEventListener("dragover", (event) => { event.preventDefault(); $("dropzone").classList.add("dragging"); });
  $("dropzone").addEventListener("dragleave", () => $("dropzone").classList.remove("dragging"));
  $("dropzone").addEventListener("drop", (event) => { event.preventDefault(); $("dropzone").classList.remove("dragging"); setFile(event.dataTransfer.files[0]); });
  $("clearButton").addEventListener("click", clearFile);
  $("analyzeButton").addEventListener("click", analyze);
  $("demoButton").addEventListener("click", () => { clearFile(); render(DEMO_DATA); showToast("Demo 分析已载入"); });
  $("copyButton").addEventListener("click", async () => { await navigator.clipboard.writeText($("promptBox").value); showToast("提示词已复制"); });
  $("promptBox").addEventListener("input", () => { state.data.prompts[state.promptKey] = $("promptBox").value; updatePromptCount(); });
  $("saveButton").addEventListener("click", saveVersion);
  $("exportButton").addEventListener("click", exportJson);
  $("addDetailButton").addEventListener("click", () => addTag("detailList", "新增关键细节"));
  $("addNegativeButton").addEventListener("click", () => addTag("negativeList", "新增 Negative Prompt"));
  document.addEventListener("click", (event) => {
    const remove = event.target.closest("[data-remove]");
    if (remove) {
      const list = $(remove.dataset.remove);
      const values = [...list.querySelectorAll(".tag")].map((tag) => tag.firstChild.textContent.trim());
      values.splice(Number(remove.dataset.index), 1);
      renderTags(remove.dataset.remove, values);
      refreshPrompt();
    }
    const modeButton = event.target.closest("[data-mode]");
    if (modeButton) {
      state.mode = modeButton.dataset.mode;
      document.querySelectorAll(".mode-button").forEach((button) => { button.classList.toggle("active", button === modeButton); button.setAttribute("aria-selected", button === modeButton ? "true" : "false"); });
      $("fileInput").accept = state.mode === "image" ? "image/jpeg,image/png,image/webp,image/gif" : "video/mp4,video/webm,video/quicktime";
      clearFile();
      if (state.mode === "video") showToast("视频分镜分析将在下一版本开放");
    }
    const promptTab = event.target.closest("[data-prompt]");
    if (promptTab) {
      state.promptKey = promptTab.dataset.prompt;
      document.querySelectorAll(".prompt-tab").forEach((button) => button.classList.toggle("active", button === promptTab));
      refreshPrompt();
    }
  });
  Object.values(fieldMap).forEach((id) => $(id).addEventListener("input", refreshPrompt));
});
