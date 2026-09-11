/** 轻量 Markdown 渲染（面向对话回复，无外部依赖） */
(function (global) {
  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function inline(text) {
    let s = escapeHtml(text);
    s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
    s = s.replace(
      /\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noreferrer">$1</a>',
    );
    s = s.replace(/(https?:\/\/[^\s<]+)/g, (url) => {
      if (s.includes(`href="${url}`)) return url;
      return `<a href="${url}" target="_blank" rel="noreferrer">${url}</a>`;
    });
    return s;
  }

  function renderMarkdown(src) {
    const text = String(src || "").replace(/\r\n/g, "\n");
    const lines = text.split("\n");
    const html = [];
    let i = 0;
    let inUl = false;
    let inOl = false;
    let inTable = false;

    function closeLists() {
      if (inUl) {
        html.push("</ul>");
        inUl = false;
      }
      if (inOl) {
        html.push("</ol>");
        inOl = false;
      }
    }
    function closeTable() {
      if (inTable) {
        html.push("</tbody></table>");
        inTable = false;
      }
    }

    while (i < lines.length) {
      const line = lines[i];

      if (line.startsWith("```")) {
        closeLists();
        closeTable();
        const lang = escapeHtml(line.slice(3).trim());
        const buf = [];
        i += 1;
        while (i < lines.length && !lines[i].startsWith("```")) {
          buf.push(lines[i]);
          i += 1;
        }
        html.push(
          `<pre class="md-code"><code class="lang-${lang}">${escapeHtml(buf.join("\n"))}</code></pre>`,
        );
        i += 1;
        continue;
      }

      if (/^\s*\|(.+)\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-+:?\s*\|/.test(lines[i + 1])) {
        closeLists();
        const headers = line.split("|").slice(1, -1).map((c) => c.trim());
        i += 2;
        if (!inTable) {
          html.push("<table class='md-table'><thead><tr>");
          headers.forEach((h) => html.push(`<th>${inline(h)}</th>`));
          html.push("</tr></thead><tbody>");
          inTable = true;
        }
        while (i < lines.length && /^\s*\|(.+)\|\s*$/.test(lines[i])) {
          const cells = lines[i].split("|").slice(1, -1).map((c) => c.trim());
          html.push("<tr>");
          cells.forEach((c) => html.push(`<td>${inline(c)}</td>`));
          html.push("</tr>");
          i += 1;
        }
        closeTable();
        continue;
      }

      if (/^---+$/.test(line.trim())) {
        closeLists();
        closeTable();
        html.push("<hr />");
        i += 1;
        continue;
      }

      const heading = /^(#{1,4})\s+(.*)$/.exec(line);
      if (heading) {
        closeLists();
        closeTable();
        const level = heading[1].length;
        html.push(`<h${level}>${inline(heading[2])}</h${level}>`);
        i += 1;
        continue;
      }

      const ul = /^\s*[-*]\s+(.*)$/.exec(line);
      if (ul) {
        closeTable();
        if (inOl) {
          html.push("</ol>");
          inOl = false;
        }
        if (!inUl) {
          html.push("<ul>");
          inUl = true;
        }
        html.push(`<li>${inline(ul[1])}</li>`);
        i += 1;
        continue;
      }

      const ol = /^\s*(\d+)[\.\)]\s+(.*)$/.exec(line);
      if (ol) {
        closeTable();
        if (inUl) {
          html.push("</ul>");
          inUl = false;
        }
        if (!inOl) {
          html.push("<ol>");
          inOl = true;
        }
        html.push(`<li>${inline(ol[2])}</li>`);
        i += 1;
        continue;
      }

      if (!line.trim()) {
        closeLists();
        closeTable();
        i += 1;
        continue;
      }

      closeLists();
      closeTable();
      html.push(`<p>${inline(line)}</p>`);
      i += 1;
    }
    closeLists();
    closeTable();
    return html.join("\n") || "<p></p>";
  }

  global.renderMarkdown = renderMarkdown;
  global.escapeHtml = escapeHtml;
})(window);
