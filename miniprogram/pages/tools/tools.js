const { api, uploadDepthJob, downloadAuthenticated } = require("../../utils/api");
const { ensurePrivacyAuthorized } = require("../../utils/privacy");
Page({
  data: { sourceMode: "upload", filePath: "", fileName: "", videoLink: "", preset: "standard_depth", computeCount: 0, job: null, submitting: false, nativeAdVisible: true }, timer: null, active: true,
  async onShow() { this.active = true; try { const user = await getApp().ensureLogin(); this.setData({ computeCount: user.compute_count ?? user.credits ?? 0 }); } catch (error) { wx.showToast({ title: error.message, icon: "none" }); } },
  onUnload() { this.active = false; if (this.timer) clearTimeout(this.timer); },
  chooseSource(event) { this.setData({ sourceMode: event.currentTarget.dataset.source, filePath: "", fileName: "", videoLink: "", job: null }); },
  choosePreset(event) { this.setData({ preset: event.currentTarget.dataset.preset }); },
  onLinkInput(event) { this.setData({ videoLink: event.detail.value }); },
  adLoad() { this.setData({ nativeAdVisible: true }); console.log("原生模板广告加载成功"); },
  adError(error) { this.setData({ nativeAdVisible: false }); console.error("原生模板广告加载失败", error); },
  adClose() { this.setData({ nativeAdVisible: false }); console.log("原生模板广告关闭"); },
  async chooseVideo() { if (!(await ensurePrivacyAuthorized())) return; wx.chooseMedia({ count: 1, mediaType: ["video"], sourceType: ["album", "camera"], maxDuration: 300, success: ({ tempFiles }) => { const file = tempFiles[0]; if (!file) return; if (file.size > 500 * 1024 * 1024) return wx.showToast({ title: "视频不能超过 500MB", icon: "none" }); this.setData({ filePath: file.tempFilePath, fileName: file.tempFilePath.split("/").pop() }); } }); },
  async submit() {
    if (this.data.computeCount < 1) return wx.showToast({ title: "算力次数不足", icon: "none" });
    if (this.data.sourceMode === "upload" && !this.data.filePath) return wx.showToast({ title: "请选择视频", icon: "none" });
    if (this.data.sourceMode === "link" && !this.data.videoLink.trim()) return wx.showToast({ title: "请粘贴视频链接", icon: "none" });
    const key = Date.now() + "-" + Math.random().toString(36).slice(2) + "-depth"; this.setData({ submitting: true });
    try { const job = this.data.sourceMode === "upload" ? await uploadDepthJob(this.data.filePath, this.data.preset, key) : await api("/api/v1/depth/jobs/remote", { method: "POST", data: { url: this.data.videoLink.trim(), preset: this.data.preset, idempotency_key: key } }); this.setData({ job, computeCount: this.data.computeCount - 1 }); this.poll(job.id); }
    catch (error) { wx.showModal({ title: "提交失败", content: error.message || "任务未提交，未消耗算力次数", showCancel: false }); } finally { this.setData({ submitting: false }); }
  },
  async poll(id) { try { const job = await api("/api/v1/depth/jobs/" + id); if (!this.active) return; this.setData({ job }); if (["completed", "failed"].includes(job.status)) return; this.timer = setTimeout(() => this.poll(id), 1500); } catch (error) { if (this.active) wx.showToast({ title: error.message, icon: "none" }); } },
  async download() { try { const path = await downloadAuthenticated("/api/v1/depth/jobs/" + this.data.job.id + "/download"); await new Promise((resolve, reject) => wx.saveVideoToPhotosAlbum({ filePath: path, success: resolve, fail: reject })); wx.showToast({ title: "已保存到相册" }); } catch (error) { wx.showToast({ title: error.errMsg || error.message || "保存失败", icon: "none" }); } }
});
