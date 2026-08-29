/* ==========================================================================
   Futures OrderFlow · 文档站自定义 JavaScript
   ========================================================================== */

// 文档加载完成后执行
document.addEventListener("DOMContentLoaded", function () {
  // 1. 外部链接自动在新标签页打开
  document.querySelectorAll(".md-content a[href^='http']").forEach(function (link) {
    if (!link.href.includes(window.location.hostname)) {
      link.setAttribute("target", "_blank");
      link.setAttribute("rel", "noopener noreferrer");
    }
  });

  // 2. 代码块语言标签显示优化
  document.querySelectorAll("pre > code").forEach(function (code) {
    const pre = code.parentElement;
    const lang = code.className.match(/language-(\w+)/);
    if (lang && !pre.querySelector(".code-lang-label")) {
      const label = document.createElement("span");
      label.className = "code-lang-label";
      label.textContent = lang[1].toUpperCase();
      pre.appendChild(label);
    }
  });

  // 3. 表格响应式包裹（移动端横向滚动）
  document.querySelectorAll(".md-typeset table:not([class])").forEach(function (table) {
    if (!table.parentElement.classList.contains("table-wrapper")) {
      const wrapper = document.createElement("div");
      wrapper.className = "table-wrapper";
      wrapper.style.overflowX = "auto";
      table.parentNode.insertBefore(wrapper, table);
      wrapper.appendChild(table);
    }
  });
});
