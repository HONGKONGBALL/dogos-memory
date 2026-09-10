"use strict";

const form = document.querySelector("#identity-form");
const button = document.querySelector("#preview-button");
const result = document.querySelector("#result");
const serviceStatus = document.querySelector("#service-status");

function showResult(kind, title, detail, files = []) {
  result.replaceChildren();
  result.hidden = false;
  result.dataset.kind = kind;

  const label = document.createElement("div");
  label.className = "result-label";
  label.textContent = kind === "success" ? "DRY-RUN VALIDATED" : "PREVIEW BLOCKED";

  const heading = document.createElement("h2");
  heading.textContent = title;
  const copy = document.createElement("p");
  copy.className = "result-detail";
  copy.textContent = detail;
  result.append(label, heading, copy);

  if (files.length > 0) {
    const list = document.createElement("ul");
    list.className = "file-list";
    files.forEach((file) => {
      const item = document.createElement("li");
      const name = document.createElement("span");
      name.textContent = file.filename;
      const size = document.createElement("span");
      size.textContent = `${file.size_bytes.toLocaleString()} B · ${file.sha256.slice(0, 10)}`;
      item.append(name, size);
      list.append(item);
    });
    result.append(list);
  }
  result.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function checkHealth() {
  try {
    const response = await fetch("/api/health", { cache: "no-store" });
    const data = await response.json();
    if (!response.ok || data.status !== "ok") throw new Error("unhealthy");
    serviceStatus.textContent = "本地服务已就绪";
  } catch (_error) {
    serviceStatus.textContent = "本地服务暂不可用";
    document.body.dataset.offline = "true";
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!form.reportValidity()) return;

  const files = { "soul.md": document.querySelector("#soul").value };
  const user = document.querySelector("#user").value;
  const memory = document.querySelector("#memory").value;
  if (user.length > 0) files["user.md"] = user;
  if (memory.length > 0) files["memory.md"] = memory;

  button.disabled = true;
  button.querySelector("span").textContent = "正在验证…";
  try {
    const response = await fetch("/api/identity/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json; charset=utf-8" },
      body: JSON.stringify({
        dog_id: document.querySelector("#dog-id").value,
        files,
      }),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error?.detail || "请求未通过验证");
    }
    showResult(
      "success",
      data.revision,
      `${data.dog_id} 的身份包已在本地生成并验证；机械狗没有发生任何改变。`,
      data.files,
    );
  } catch (error) {
    showResult("error", "无法生成预览", error.message || "未知错误");
  } finally {
    button.disabled = false;
    button.querySelector("span").textContent = "生成本地预览";
  }
});

checkHealth();
