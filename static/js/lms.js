// ACCLLMS - Frontend Interaction Utilities
document.addEventListener("DOMContentLoaded", () => {
  // Modal handlers
  window.openModal = function(modalId) {
    const el = document.getElementById(modalId);
    if (el) el.style.display = "flex";
  };

  window.closeModal = function(modalId) {
    const el = document.getElementById(modalId);
    if (el) el.style.display = "none";
  };

  // Close modals when clicking backdrop
  document.querySelectorAll(".modal-backdrop").forEach(backdrop => {
    backdrop.addEventListener("click", (e) => {
      if (e.target === backdrop) {
        backdrop.style.display = "none";
      }
    });
  });

  // Copy to clipboard helper
  window.copyToClipboard = function(text, btnElement, successMsg = "Copied!") {
    navigator.clipboard.writeText(text).then(() => {
      const origText = btnElement.innerText;
      btnElement.innerText = successMsg;
      btnElement.style.pointerEvents = "none";
      setTimeout(() => {
        btnElement.innerText = origText;
        btnElement.style.pointerEvents = "auto";
      }, 2000);
    }).catch(err => {
      alert("Failed to copy: " + err);
    });
  };

  // In-App PDF Viewer powered by Mozilla PDF.js
  window.openPdfViewer = function(url, title, downloadUrl) {
    const modal = document.getElementById("pdfViewerModal");
    const frame = document.getElementById("pdfViewerFrame");
    const titleEl = document.getElementById("pdfViewerTitle");
    const newTabBtn = document.getElementById("pdfViewerNewTab");
    const downloadBtn = document.getElementById("pdfViewerDownload");

    if (!modal || !frame) return;

    const basePrefix = window.location.pathname.startsWith("/lms") ? "/lms" : "";
    const targetDownload = downloadUrl || url;
    const viewerPageUrl = `${basePrefix}/pdf/viewer?file=${encodeURIComponent(url)}&title=${encodeURIComponent(title || "Document")}&download=${encodeURIComponent(targetDownload)}`;

    if (titleEl) titleEl.innerText = title || "Document Viewer";
    if (newTabBtn) newTabBtn.href = viewerPageUrl;
    if (downloadBtn) {
      downloadBtn.href = targetDownload;
      downloadBtn.setAttribute("download", title || "document.pdf");
      downloadBtn.onclick = function(e) {
        e.preventDefault();
        const docName = (title && title.endsWith('.pdf')) ? title : ((title || 'document') + '.pdf');
        if (window.Android && typeof Android.downloadUrl === 'function') {
          Android.downloadUrl(targetDownload, docName);
          return;
        }
        let dl = targetDownload;
        if (dl.indexOf('?') === -1) dl += '?download=1';
        else if (dl.indexOf('download=') === -1) dl += '&download=1';
        window.location.href = dl;
      };
    }

    frame.src = viewerPageUrl;
    modal.style.display = "flex";
    document.body.style.overflow = "hidden";
  };

  window.closePdfViewer = function() {
    const modal = document.getElementById("pdfViewerModal");
    const frame = document.getElementById("pdfViewerFrame");
    if (modal) modal.style.display = "none";
    if (frame) frame.src = "about:blank";
    document.body.style.overflow = "";
    const modalContent = modal ? modal.querySelector(".pdf-modal-content") : null;
    if (modalContent) modalContent.classList.remove("pdf-fullscreen");
    const fsBtn = document.getElementById("pdfViewerFullscreenBtn");
    if (fsBtn) fsBtn.innerText = "⛶ Fullscreen";
  };

  window.togglePdfFullscreen = function() {
    const modal = document.getElementById("pdfViewerModal");
    if (!modal) return;
    const modalContent = modal.querySelector(".pdf-modal-content");
    const fsBtn = document.getElementById("pdfViewerFullscreenBtn");
    if (modalContent) {
      modalContent.classList.toggle("pdf-fullscreen");
      if (fsBtn) {
        fsBtn.innerText = modalContent.classList.contains("pdf-fullscreen") ? "⛶ Exit Fullscreen" : "⛶ Fullscreen";
      }
    }
  };

  // Close PDF on Escape key
  document.addEventListener("keydown", function(e) {
    if (e.key === "Escape") {
      const modal = document.getElementById("pdfViewerModal");
      if (modal && modal.style.display === "flex") {
        closePdfViewer();
      }
    }
  });

  // Preview file from student locker
  window.previewLockerFile = function(fileId) {
    const modal = document.getElementById("filePreviewModal");
    const titleEl = document.getElementById("previewModalTitle");
    const bodyEl = document.getElementById("previewModalBody");

    const basePrefix = window.location.pathname.startsWith("/lms") ? "/lms" : "";

    fetch(`${basePrefix}/locker/preview/${fileId}`)
      .then(res => res.json())
      .then(data => {
        if (data.type === "pdf") {
          window.openPdfViewer(data.url, data.filename, data.download_url);
          return;
        }

        if (!modal || !titleEl || !bodyEl) return;
        titleEl.innerText = data.filename + " (" + data.size + ")";
        modal.style.display = "flex";

        if (data.type === "text") {
          bodyEl.innerHTML = `<pre style="background:#0f172a; color:#f8fafc; padding:1rem; border-radius:8px; max-height:450px; overflow:auto; font-family:monospace; font-size:0.88rem;"><code>${escapeHtml(data.content)}</code></pre>`;
        } else {
          bodyEl.innerHTML = `<div style="text-align:center; padding:2rem; color:#64748b;">
            <p style="font-size:1.1rem; margin-bottom:1rem;">📄 ${data.message}</p>
            <a href="${basePrefix}/locker/download/${fileId}" class="btn btn-primary btn-sm">Download File</a>
          </div>`;
        }
      })
      .catch(err => {
        if (bodyEl && modal) {
          modal.style.display = "flex";
          bodyEl.innerHTML = `<p style='color:#dc2626; padding:1rem;'>Failed to load preview: ${err}</p>`;
        }
      });
  };

  function escapeHtml(string) {
    const entityMap = {
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;',
      '/': '&#x2F;'
    };
    return String(string).replace(/[&<>"'\/]/g, s => entityMap[s]);
  }

  // Exam Countdown Timer
  const timerContainer = document.getElementById("examTimer");
  if (timerContainer) {
    const endIso = timerContainer.getAttribute("data-end");
    if (endIso) {
      const endTime = new Date(endIso.replace(" ", "T")).getTime();
      const updateTimer = () => {
        const now = new Date().getTime();
        const distance = endTime - now;

        if (distance < 0) {
          timerContainer.innerHTML = "<span style='color:#dc2626; font-weight:700;'>EXAM TIME EXPIRED</span>";
          const submitBtn = document.querySelector("#examSubmitForm button[type='submit']");
          if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.textContent = "🔒 Exam Concluded - Submissions Closed";
            submitBtn.style.background = "#64748b";
            submitBtn.style.cursor = "not-allowed";
          }
          return;
        }

        const hours = Math.floor(distance / (1000 * 60 * 60));
        const minutes = Math.floor((distance % (1000 * 60 * 60)) / (1000 * 60));
        const seconds = Math.floor((distance % (1000 * 60)) / 1000);

        timerContainer.innerHTML = `⏱ Time Remaining: <strong>${hours}h ${minutes}m ${seconds}s</strong>`;
      };

      updateTimer();
      setInterval(updateTimer, 1000);
    }
  }

  // Scheduled Exam Countdown & Auto-Unlock
  const startBox = document.getElementById("startCountdownBox");
  if (startBox) {
    const startIso = startBox.getAttribute("data-start");
    if (startIso) {
      const startTime = new Date(startIso.replace(" ", "T")).getTime();
      let hasReloaded = false;
      const updateStartTimer = () => {
        const now = new Date().getTime();
        const distance = startTime - now;

        if (distance <= 0) {
          startBox.innerHTML = "🔓 <strong style='color:#16a34a;'>EXAM IS LIVE! Unlocking...</strong>";
          if (!hasReloaded) {
            hasReloaded = true;
            setTimeout(() => { window.location.reload(); }, 800);
          }
          return;
        }

        const hours = Math.floor(distance / (1000 * 60 * 60));
        const minutes = Math.floor((distance % (1000 * 60 * 60)) / (1000 * 60));
        const seconds = Math.floor((distance % (1000 * 60)) / 1000);

        startBox.innerHTML = `⏳ Unlocks in: <strong>${hours}h ${minutes}m ${seconds}s</strong>`;
      };

      updateStartTimer();
      setInterval(updateStartTimer, 1000);
    }
  }

  // Strict Client-Side ZIP Check for Exam Submission
  const examForm = document.getElementById("examSubmitForm");
  if (examForm) {
    examForm.addEventListener("submit", (e) => {
      const fileInput = document.getElementById("exam_file");
      if (fileInput && fileInput.files.length > 0) {
        const file = fileInput.files[0];
        if (!file.name.toLowerCase().endsWith(".zip")) {
          e.preventDefault();
          alert("STRICT POLICY: Only .zip files are allowed for exam submission. Please zip your submission files and try again.");
          return false;
        }
      }
    });
  }
});

// Reusable Markdown & Rich Text Formatting Toolbar Helper
function applyFormat(textareaId, formatType) {
  const textarea = document.getElementById(textareaId);
  if (!textarea) return;

  const start = textarea.selectionStart;
  const end = textarea.selectionEnd;
  const selectedText = textarea.value.substring(start, end);
  let replacement = "";

  switch (formatType) {
    case 'bold':
      if (selectedText.trim()) {
        const leading = selectedText.match(/^\s*/)[0];
        const trailing = selectedText.match(/\s*$/)[0];
        replacement = `${leading}**${selectedText.trim()}**${trailing}`;
      } else {
        replacement = `**bold text**`;
      }
      break;
    case 'italic':
      if (selectedText.trim()) {
        const leading = selectedText.match(/^\s*/)[0];
        const trailing = selectedText.match(/\s*$/)[0];
        replacement = `${leading}*${selectedText.trim()}*${trailing}`;
      } else {
        replacement = `*italic text*`;
      }
      break;
    case 'heading':
      replacement = `\n### ${selectedText || 'Heading'}\n`;
      break;
    case 'ul':
      replacement = selectedText
        ? selectedText.split('\n').map(l => `- ${l}`).join('\n')
        : `\n- Item 1\n- Item 2\n`;
      break;
    case 'ol':
      replacement = selectedText
        ? selectedText.split('\n').map((l, i) => `${i + 1}. ${l}`).join('\n')
        : `\n1. First item\n2. Second item\n`;
      break;
    case 'code':
      if (selectedText.includes('\n')) {
        replacement = `\n\`\`\`\n${selectedText || '// code here'}\n\`\`\`\n`;
      } else {
        replacement = `\`${selectedText || 'code'}\``;
      }
      break;
    case 'link':
      const url = prompt('Enter URL (e.g. https://example.com):', 'https://');
      if (!url) return;
      replacement = `[${selectedText || 'link'}](${url})`;
      break;
  }

  textarea.setRangeText(replacement, start, end, 'end');
  textarea.focus();
}
