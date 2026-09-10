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

  // Preview file from student locker
  window.previewLockerFile = function(fileId) {
    const modal = document.getElementById("filePreviewModal");
    const titleEl = document.getElementById("previewModalTitle");
    const bodyEl = document.getElementById("previewModalBody");

    if (!modal || !titleEl || !bodyEl) return;

    titleEl.innerText = "Loading preview...";
    bodyEl.innerHTML = "<p style='text-align:center; padding: 2rem;'>Fetching file preview...</p>";
    modal.style.display = "flex";

    fetch(`/locker/preview/${fileId}`)
      .then(res => res.json())
      .then(data => {
        titleEl.innerText = data.filename + " (" + data.size + ")";
        if (data.type === "text") {
          bodyEl.innerHTML = `<pre style="background:#0f172a; color:#f8fafc; padding:1rem; border-radius:8px; max-height:450px; overflow:auto; font-family:monospace; font-size:0.88rem;"><code>${escapeHtml(data.content)}</code></pre>`;
        } else {
          bodyEl.innerHTML = `<div style="text-align:center; padding:2rem; color:#64748b;">
            <p style="font-size:1.1rem; margin-bottom:1rem;">📄 ${data.message}</p>
            <a href="/locker/download/${fileId}" class="btn btn-primary btn-sm">Download File</a>
          </div>`;
        }
      })
      .catch(err => {
        bodyEl.innerHTML = `<p style='color:#dc2626; padding:1rem;'>Failed to load preview: ${err}</p>`;
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
