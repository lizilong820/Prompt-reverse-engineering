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

function uploadJob(filePath, mode, idempotencyKey) {
  return new Promise((resolve, reject) => {
    const task = wx.uploadFile({
      url: API_BASE_URL + "/api/v1/jobs",
      filePath,
      name: "file",
      formData: { mode, idempotency_key: idempotencyKey },
      header: { Authorization: "Bearer " + wx.getStorageSync("token") },
      success(response) {
        let data;
        try { data = JSON.parse(response.data); } catch (_) { return reject(new Error("服务器响应异常")); }
        if (response.statusCode >= 200 && response.statusCode < 300) return resolve(data);
        const error = new Error(data.detail || "提交失败"); error.statusCode = response.statusCode; reject(error);
      },
      fail: reject
    });
    task.onProgressUpdate((event) => optionsProgress(event.progress));
  });
}

let optionsProgress = () => {};
function onUploadProgress(callback) { optionsProgress = callback || (() => {}); }

module.exports = { api, uploadJob, onUploadProgress };
