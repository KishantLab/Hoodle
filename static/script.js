document.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("submissionForm");
    const rollInput = document.getElementById("rollNumber");
    const rollStatusTip = document.getElementById("rollStatusTip");
    const dropZone = document.getElementById("dropZone");
    const fileInput = document.getElementById("examFile");
    const btnBrowse = document.getElementById("btnBrowse");
    const dropContent = document.getElementById("dropContent");
    const filePreview = document.getElementById("filePreview");
    const previewFileName = document.getElementById("previewFileName");
    const previewFileSize = document.getElementById("previewFileSize");
    const btnRemoveFile = document.getElementById("btnRemoveFile");
    const progressContainer = document.getElementById("progressContainer");
    const progressFill = document.getElementById("progressFill");
    const progressText = document.getElementById("progressText");
    const errorAlert = document.getElementById("errorAlert");
    const btnSubmit = document.getElementById("btnSubmit");
    const btnText = btnSubmit ? btnSubmit.querySelector(".btn-text") : null;
    const btnLoader = btnSubmit ? btnSubmit.querySelector(".btn-loader") : null;
    const submissionCard = document.getElementById("submissionCard");
    const receiptCard = document.getElementById("receiptCard");
    const btnResubmit = document.getElementById("btnResubmit");

    if (!form || !rollInput) return;

    const examConfig = window.EXAM_CONFIG || { slug: "", allowMultiple: true };
    const examSlug = examConfig.slug;

    // 1. Roll Number: Automatically convert to Uppercase Alphanumeric
    let debounceTimer = null;

    function sanitizeInput(val) {
        return val.toUpperCase().replace(/[^A-Z0-9_-]/g, "");
    }

    rollInput.addEventListener("input", (e) => {
        const cursorPosition = e.target.selectionStart;
        const originalVal = e.target.value;
        const cleaned = sanitizeInput(originalVal);
        
        if (originalVal !== cleaned) {
            e.target.value = cleaned;
            e.target.setSelectionRange(cursorPosition, cursorPosition);
        } else {
            e.target.value = cleaned;
        }

        clearError();

        // Debounced check for existing submission
        clearTimeout(debounceTimer);
        if (cleaned.length >= 3 && examSlug) {
            debounceTimer = setTimeout(() => {
                checkPriorSubmission(cleaned);
            }, 400);
        } else {
            rollStatusTip.textContent = "";
        }
    });

    rollInput.addEventListener("blur", (e) => {
        e.target.value = sanitizeInput(e.target.value);
    });

    async function checkPriorSubmission(roll) {
        try {
            const res = await fetch(`exam/${encodeURIComponent(examSlug)}/status/${encodeURIComponent(roll)}`);
            if (res.ok) {
                const data = await res.json();
                if (data.has_submitted) {
                    if (examConfig.allowMultiple) {
                        rollStatusTip.innerHTML = `ℹ️ Previous submission found (<strong>v${data.version}</strong> at ${data.submitted_at}). Uploading again will update your active latest file.`;
                        rollStatusTip.style.color = "#b45309";
                        btnSubmit.disabled = false;
                    } else {
                        rollStatusTip.innerHTML = `⛔ <strong>Already submitted</strong> at ${data.submitted_at}. Multiple submissions are not permitted for this exam.`;
                        rollStatusTip.style.color = "#b91c1c";
                        btnSubmit.disabled = true;
                    }
                } else {
                    rollStatusTip.textContent = "✓ Ready for submission";
                    rollStatusTip.style.color = "#15803d";
                    btnSubmit.disabled = false;
                }
            }
        } catch (err) {
            // Ignore background network glitch
        }
    }

    // 2. Drag and Drop File Upload
    if (btnBrowse) {
        btnBrowse.addEventListener("click", (e) => {
            e.preventDefault();
            e.stopPropagation();
            fileInput.click();
        });
    }

    if (dropZone) {
        dropZone.addEventListener("click", (e) => {
            if (e.target !== btnRemoveFile && !filePreview.contains(e.target)) {
                fileInput.click();
            }
        });

        ["dragenter", "dragover"].forEach((eventName) => {
            dropZone.addEventListener(eventName, (e) => {
                e.preventDefault();
                e.stopPropagation();
                dropZone.classList.add("dragover");
            });
        });

        ["dragleave", "drop"].forEach((eventName) => {
            dropZone.addEventListener(eventName, (e) => {
                e.preventDefault();
                e.stopPropagation();
                dropZone.classList.remove("dragover");
            });
        });

        dropZone.addEventListener("drop", (e) => {
            const files = e.dataTransfer.files;
            if (files.length > 0) {
                fileInput.files = files;
                displayFilePreview(files[0]);
            }
        });
    }

    if (fileInput) {
        fileInput.addEventListener("change", () => {
            if (fileInput.files.length > 0) {
                displayFilePreview(fileInput.files[0]);
            }
        });
    }

    if (btnRemoveFile) {
        btnRemoveFile.addEventListener("click", (e) => {
            e.preventDefault();
            e.stopPropagation();
            resetFileInput();
        });
    }

    function displayFilePreview(file) {
        clearError();
        previewFileName.textContent = file.name;
        previewFileSize.textContent = formatBytes(file.size);
        dropContent.style.display = "none";
        filePreview.style.display = "flex";
    }

    function resetFileInput() {
        fileInput.value = "";
        dropContent.style.display = "block";
        filePreview.style.display = "none";
    }

    function formatBytes(bytes) {
        if (bytes === 0) return "0 Bytes";
        const k = 1024;
        const sizes = ["Bytes", "KB", "MB", "GB"];
        const i = Math.floor(Math.log(bytes) / Math.log(k));
        return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + " " + sizes[i];
    }

    function showError(msg) {
        errorAlert.textContent = msg;
        errorAlert.style.display = "block";
    }

    function clearError() {
        errorAlert.textContent = "";
        errorAlert.style.display = "none";
    }

    // 3. Form Submission Handling (AJAX)
    form.addEventListener("submit", (e) => {
        e.preventDefault();
        clearError();

        const roll = sanitizeInput(rollInput.value);
        if (!roll) {
            showError("Please enter your Roll Number.");
            rollInput.focus();
            return;
        }

        if (!fileInput.files || fileInput.files.length === 0) {
            showError("Please select or drag your exam file.");
            return;
        }

        const file = fileInput.files[0];
        if (file.size === 0) {
            showError("The selected file is empty (0 bytes). Please upload a valid file.");
            return;
        }

        // Prepare FormData
        const formData = new FormData();
        formData.append("roll_number", roll);
        formData.append("exam_file", file);

        // UI State: Uploading
        btnSubmit.disabled = true;
        btnText.style.display = "none";
        btnLoader.style.display = "inline-block";
        progressContainer.style.display = "block";
        progressFill.style.width = "0%";
        progressText.textContent = "Uploading... 0%";

        const xhr = new XMLHttpRequest();
        xhr.open("POST", `exam/${encodeURIComponent(examSlug)}/submit`, true);

        xhr.upload.onprogress = (event) => {
            if (event.lengthComputable) {
                const percent = Math.round((event.loaded / event.total) * 100);
                progressFill.style.width = percent + "%";
                progressText.textContent = `Uploading... ${percent}%`;
            }
        };

        xhr.onload = () => {
            btnSubmit.disabled = false;
            btnText.style.display = "inline-block";
            btnLoader.style.display = "none";

            try {
                const res = JSON.parse(xhr.responseText);
                if (xhr.status === 200 && res.success) {
                    showSuccessReceipt(res.details);
                } else {
                    showError(res.error || "Upload failed. Please try again.");
                    progressContainer.style.display = "none";
                }
            } catch (err) {
                showError("Server returned an unexpected response. Please notify the invigilator.");
                progressContainer.style.display = "none";
            }
        };

        xhr.onerror = () => {
            btnSubmit.disabled = false;
            btnText.style.display = "inline-block";
            btnLoader.style.display = "none";
            progressContainer.style.display = "none";
            showError("Network connection error. Please verify your connection to the server.");
        };

        xhr.send(formData);
    });

    // 4. Show Success Receipt Card
    function showSuccessReceipt(details) {
        document.getElementById("receiptRoll").textContent = details.roll_number;
        document.getElementById("receiptFilename").textContent = details.filename;
        document.getElementById("receiptSize").textContent = details.file_size;
        document.getElementById("receiptTime").textContent = details.submitted_at;
        document.getElementById("receiptHash").textContent = details.sha256;

        const subtitle = document.getElementById("receiptSubtitle");
        if (details.is_update) {
            subtitle.innerHTML = `Your updated submission (Attempt #${details.version}) was received. Your active latest file has been updated.`;
        } else {
            subtitle.textContent = "Your exam file has been successfully uploaded and recorded.";
        }

        // Hide form card, reveal receipt card
        submissionCard.style.display = "none";
        receiptCard.style.display = "block";
        window.scrollTo({ top: 0, behavior: "smooth" });
    }

    // 5. Submit Another Version Button
    if (btnResubmit) {
        btnResubmit.addEventListener("click", () => {
            receiptCard.style.display = "none";
            submissionCard.style.display = "block";
            resetFileInput();
            progressContainer.style.display = "none";
            clearError();
            
            const roll = sanitizeInput(rollInput.value);
            if (roll) {
                checkPriorSubmission(roll);
            }
        });
    }
});
