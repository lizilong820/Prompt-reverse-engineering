const { api, downloadAuthenticated } = require("../../utils/api");
const { API_BASE_URL } = require("../../config");

Page({
  data: { projects: [], active: null, loading: true },
  onShow() { this.load(); },
  async load() {
    try { await getApp().ensureLogin(); this.setData({ projects: await api("/api/v1/projects"), loading: false }); }
    catch (error) { this.setData({ loading: false }); wx.showToast({ title: error.message || "项目读取失败", icon: "none" }); }
  },
  async openProject(event) {
    try { this.setData({ active: await api("/api/v1/projects/" + event.currentTarget.dataset.id) }); }
    catch (error) { wx.showToast({ title: error.message, icon: "none" }); }
  },
  closeProject() { this.setData({ active: null }); },
  async toggleFavorite() {
    const project = this.data.active; if (!project) return;
    try { const active = await api(`/api/v1/projects/${project.id}`, { method: "PATCH", data: { is_favorite: !project.is_favorite } }); this.setData({ active }); await this.load(); this.setData({ active }); }
    catch (error) { wx.showToast({ title: error.message, icon: "none" }); }
  },
  async editProject() {
    const project = this.data.active; if (!project) return;
    const title = await new Promise((resolve) => wx.showModal({ title: "修改项目标题", editable: true, content: project.title, confirmText: "保存", success: (r) => resolve(r.confirm ? r.content : ""), fail: () => resolve("") }));
    if (!title) return;
    try { this.setData({ active: await api(`/api/v1/projects/${project.id}`, { method: "PATCH", data: { title } }) }); await this.load(); }
    catch (error) { wx.showToast({ title: error.message, icon: "none" }); }
  },
  async editNote() {
    const project = this.data.active; if (!project) return;
    const note = await new Promise((resolve) => wx.showModal({ title: "修改项目备注", editable: true, content: project.note || "", confirmText: "保存", success: (r) => resolve(r.confirm ? r.content : ""), fail: () => resolve("") }));
    if (note === "") return;
    try { this.setData({ active: await api(`/api/v1/projects/${project.id}`, { method: "PATCH", data: { note } }) }); await this.load(); }
    catch (error) { wx.showToast({ title: error.message, icon: "none" }); }
  },
  async copy(event) { const item = this.data.active?.versions?.find((v) => String(v.id) === String(event.currentTarget.dataset.id)); if (item) wx.setClipboardData({ data: event.currentTarget.dataset.lang === "en" ? item.prompt_en : item.prompt_zh }); },
  async exportProject(event) {
    const project = this.data.active; if (!project) return;
    const format = event.currentTarget.dataset.format;
    try { const path = await downloadAuthenticated(`/api/v1/projects/${project.id}/export?format=${format}`, format); wx.openDocument({ filePath: path, fileType: format, showMenu: true }); }
    catch (error) { wx.showToast({ title: error.message || "导出失败", icon: "none" }); }
  },
  async deleteProject() {
    const project = this.data.active; if (!project) return;
    const confirmed = await new Promise((resolve) => wx.showModal({ title: "删除项目", content: "项目及其版本将被删除，不能恢复", confirmColor: "#ff6b57", success: (r) => resolve(r.confirm), fail: () => resolve(false) }));
    if (!confirmed) return;
    try { await api(`/api/v1/projects/${project.id}`, { method: "DELETE" }); this.setData({ active: null }); await this.load(); }
    catch (error) { wx.showToast({ title: error.message, icon: "none" }); }
  }
});
