const { api, uploadDepthJob, downloadAuthenticated } = require("../../utils/api");
const { AD_UNIT_ID, API_BASE_URL } = require("../../config");
const { ensurePrivacyAuthorized } = require("../../utils/privacy");
Page({
  data: { sourceMode: "upload", filePath: "", fileName: "", videoLink: "", preset: "standard_depth", computeCount: 0, adReward: 1, adRemaining: 20, job: null, resultVideoPath: "", previewLoading: false, submitting: false, adLoading: false }, timer: null, active: true, rewardAd: null, currentJobId: null, adFlowActive: false,
  onLoad() {
    this.active = true;
    if (AD_UNIT_ID && wx.createRewardedVideoAd) {
      this.rewardAd = wx.createRewardedVideoAd({ adUnitId: AD_UNIT_ID });
    }
  },
  async onShow() {
    this.active = true;
    try {
      const user = await getApp().ensureLogin();
      this.setData({ computeCount: user.compute_count ?? user.credits ?? 0, adReward: user.ad?.reward || 1, adRemaining: user.ad?.remaining_today ?? user.ad?.daily_limit ?? 20 });
      const requestedJobId = wx.getStorageSync("openDepthJobId");
      if (requestedJobId) {
        wx.removeStorageSync("openDepthJobId");
        this.openDepthJob(requestedJobId);
      }
    } catch (error) { wx.showToast({ title: error.message, icon: "none" }); }
  },
  onUnload() {
    this.active = false;
    if (this.timer) clearTimeout(this.timer);
  },
  openDiagnostic() { wx.navigateTo({ url: "/pages/diagnostic/diagnostic" }); },
  chooseSource(event) {
    this.stopPolling();
    this.setData({ sourceMode: event.currentTarget.dataset.source, filePath: "", fileName: "", videoLink: "", job: null, resultVideoPath: "", previewLoading: false });
  },
  choosePreset(event) {
    const preset = event.currentTarget.dataset.preset;
    if (preset === this.data.preset) return;
    if (this.data.job && !["completed", "failed", "expired"].includes(this.data.job.status)) {
      return wx.showToast({ title: "当前任务处理中，请等待完成", icon: "none" });
    }
    this.setData({ preset, job: null, resultVideoPath: "", previewLoading: false });
  },
  onLinkInput(event) { this.setData({ videoLink: event.detail.value }); },
  async watchRewardAd() {
    if (this.adFlowActive) return;
    if (!this.rewardAd) return wx.showToast({ title: "激励广告尚未配置", icon: "none" });
    if (this.data.adRemaining <= 0) return wx.showToast({ title: "今日奖励次数已用完", icon: "none" });
    this.adFlowActive = true;
    this.setData({ adLoading: true });
    try {
      const prepared = await api("/api/v1/rewards/ad/prepare", { method: "POST" });
      const ended = await new Promise(async (resolve, reject) => {
        const handler = (event) => { this.rewardAd.offClose(handler); resolve(event?.isEnded); };
        this.rewardAd.onClose(handler);
        try { await this.rewardAd.show(); } catch (_) {
          try { await this.rewardAd.load(); await this.rewardAd.show(); } catch (error) { this.rewardAd.offClose(handler); reject(error); }
        }
      });
      if (!ended) return wx.showToast({ title: "完整观看后才能获得次数", icon: "none" });
      const result = await api("/api/v1/rewards/ad/complete", { method: "POST", data: { claim_token: prepared.claim_token } });
      if (this.active) this.setData({ computeCount: result.compute_count ?? result.credits, adRemaining: Math.max(0, this.data.adRemaining - 1) });
      wx.showToast({ title: "+" + result.rewarded + " 次算力" });
    } catch (error) {
      wx.showToast({ title: error.message || "领取失败", icon: "none" });
    } finally {
      this.adFlowActive = false;
      if (this.active) this.setData({ adLoading: false });
    }
  },
  async chooseVideo() { if (!(await ensurePrivacyAuthorized())) return; wx.chooseMedia({ count: 1, mediaType: ["video"], sourceType: ["album", "camera"], maxDuration: 300, success: ({ tempFiles }) => { const file = tempFiles[0]; if (!file) return; if (file.size > 500 * 1024 * 1024) return wx.showToast({ title: "视频不能超过 500MB", icon: "none" }); this.setData({ filePath: file.tempFilePath, fileName: file.tempFilePath.split("/").pop() }); } }); },
  async submit() {
    if (this.data.computeCount < 1) return wx.showToast({ title: "算力次数不足", icon: "none" });
    if (this.data.sourceMode === "upload" && !this.data.filePath) return wx.showToast({ title: "请选择视频", icon: "none" });
    if (this.data.sourceMode === "link" && !this.data.videoLink.trim()) return wx.showToast({ title: "请粘贴视频链接", icon: "none" });
    const key = Date.now() + "-" + Math.random().toString(36).slice(2) + "-depth"; this.stopPolling(); this.setData({ submitting: true, job: null, resultVideoPath: "", previewLoading: false });
    try { const job = this.data.sourceMode === "upload" ? await uploadDepthJob(this.data.filePath, this.data.preset, key) : await api("/api/v1/depth/jobs/remote", { method: "POST", data: { url: this.data.videoLink.trim(), preset: this.data.preset, idempotency_key: key } }); this.setData({ job, computeCount: this.data.computeCount - 1 }); this.poll(job.id); }
    catch (error) { wx.showModal({ title: "提交失败", content: error.message || "任务未提交，未消耗算力次数", showCancel: false }); } finally { this.setData({ submitting: false }); }
  },
  stopPolling() { if (this.timer) clearTimeout(this.timer); this.timer = null; this.currentJobId = null; },
  openDepthJob(id) {
    this.stopPolling();
    this.setData({ job: null, resultVideoPath: "", previewLoading: false });
    this.poll(id);
  },
  decorateJob(job) {
    const metadata = job.metadata || {};
    const resultMeta = metadata.width && metadata.height
      ? `${metadata.width} × ${metadata.height} · ${metadata.fps || "-"} FPS · ${metadata.frames || "-"} 帧`
      : "";
    return { ...job, resultMeta, expiresText: job.available_until ? new Date(job.available_until).toLocaleString() : "" };
  },
  async loadPreview(job) {
    if (!job.preview_url || this.data.resultVideoPath) return;
    this.setData({ previewLoading: true });
    try {
      const path = API_BASE_URL + job.preview_url;
      if (this.active && this.currentJobId === job.id) this.setData({ resultVideoPath: path });
    } catch (error) {
      if (this.active) wx.showToast({ title: error.message || "结果视频加载失败", icon: "none" });
    } finally {
      if (this.active && this.currentJobId === job.id) this.setData({ previewLoading: false });
    }
  },
  async poll(id) {
    this.currentJobId = Number(id);
    try {
      const job = this.decorateJob(await api("/api/v1/depth/jobs/" + id));
      if (!this.active || this.currentJobId !== Number(id)) return;
      this.setData({ job });
      if (job.status === "completed") { await this.loadPreview(job); return; }
      if (["failed", "expired"].includes(job.status)) return;
      this.timer = setTimeout(() => this.poll(id), 1500);
    } catch (error) {
      if (!this.active || this.currentJobId !== Number(id)) return;
      wx.showToast({ title: error.message || "任务状态读取失败", icon: "none" });
      this.timer = setTimeout(() => this.poll(id), 3000);
    }
  },
  noop() {},
  async download() {
    if (!this.data.job?.download_url || !(await ensurePrivacyAuthorized())) return;
    try { const path = await downloadAuthenticated(this.data.job.download_url); await new Promise((resolve, reject) => wx.saveVideoToPhotosAlbum({ filePath: path, success: resolve, fail: reject })); wx.showToast({ title: "已保存到相册" }); } catch (error) { wx.showToast({ title: error.errMsg || error.message || "保存失败", icon: "none" }); }
  }
});
