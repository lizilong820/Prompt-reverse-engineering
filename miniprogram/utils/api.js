const { API_BASE_URL } = require("../config");

function api(path, options = {}) {
  return new Promise((resolve, reject) => {
    wx.request({
      url: API_BASE_URL + path,
      method: options.method || "GET",
      data: options.data,
      header: {
        "content-type": "application/json",
        ...(options.auth === false ? {} : { Authorization: "Bearer " + wx.getStorageSync("token") })
      },
      success(response) {
        if (response.statusCode >= 200 && response.statusCode < 300) return resolve(response.data);
        const error = new Error(response.data?.detail || "请求失败");
        error.statusCode = response.statusCode;
        reject(error);
      },
      fail: reject
    });
  });
}

function uploadFileTask(path, filePath, formData) {
  return new Promise((resolve, reject) => {
    const task = wx.uploadFile({
      url: API_BASE_URL + path,
      filePath,
      name: "file",
      formData,
      header: { Authorization: "Bearer " + wx.getStorageSync("token") },
      success(response) {
        let data;
        try { data = JSON.parse(response.data); } catch (_) { return reject(new Error("服务器响应异常")); }
        if (response.statusCode >= 200 && response.statusCode < 300) return resolve(data);
        const error = new Error(data.detail || "提交失败"); error.statusCode = response.statusCode; error.stage = "upload"; reject(error);
      },
      fail(error) {
        const failure = new Error(error?.errMsg || "文件上传失败，请检查 uploadFile 合法域名");
        failure.stage = "upload";
        failure.original = error;
        reject(failure);
      }
    });
    task.onProgressUpdate((event) => optionsProgress(event.progress));
  });
}

function uploadJob(filePath, mode, idempotencyKey, analysisDepth = "detailed", analysisTask = "reconstruct") {
  return uploadFileTask("/api/v1/jobs", filePath, { mode, analysis_depth: analysisDepth, analysis_task: analysisTask, idempotency_key: idempotencyKey });
}

function uploadDepthJob(filePath, options, idempotencyKey) {
  return uploadFileTask("/api/v1/depth/jobs", filePath, { ...options, idempotency_key: idempotencyKey });
}

function uploadDiagnosticVideo(diagnosticId, role, filePath) {
  return uploadFileTask(`/api/v1/replication-diagnostics/${diagnosticId}/${role}`, filePath, {});
}

function downloadAuthenticated(path, extension = "") {
  return new Promise((resolve, reject) => {
    wx.downloadFile({
      url: API_BASE_URL + path,
      header: { Authorization: "Bearer " + wx.getStorageSync("token") },
      success(response) {
        if (response.statusCode >= 200 && response.statusCode < 300) {
          if (!extension) return resolve(response.tempFilePath);
          const target = `${wx.env.USER_DATA_PATH}/prompt-lens-${Date.now()}${extension.startsWith(".") ? extension : `.${extension}`}`;
          wx.getFileSystemManager().copyFile({
            srcPath: response.tempFilePath,
            destPath: target,
            success: () => resolve(target),
            fail: reject,
          });
          return;
        }
        reject(new Error("视频下载失败"));
      },
      fail: reject
    });
  });
}

let optionsProgress = () => {};
function onUploadProgress(callback) { optionsProgress = callback || (() => {}); }

module.exports = { api, uploadJob, uploadDepthJob, uploadDiagnosticVideo, downloadAuthenticated, onUploadProgress };
